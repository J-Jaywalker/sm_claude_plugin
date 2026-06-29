"""MCP stdio channel server for Crab — hand-rolled JSON-RPC.

Spawned by Claude Code via .mcp.json when Claude starts with
`--dangerously-load-development-channels server:crab`. Bridges Claude's MCP
channel protocol to the parent CrabApp over a Unix socket.

  Claude  ←stdio (MCP)→  this process  ←Unix socket→  CrabApp (Textual)

Capabilities declared:
  experimental.claude/channel             — push prompts in, reply tool out
  experimental.claude/channel/permission  — relay tool-permission prompts

Run directly via `python -m crab.channel.server`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import secrets
import string
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

from crab.channel import bridge
from crab.config import dlog as _shared_dlog

# Stdout is reserved for JSON-RPC; all diagnostics go to stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [crab-channel] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("crab.channel.server")

def _dlog(msg: str) -> None:
    _shared_dlog("server", msg)


_notif_counter = itertools.count(1)
_tool_counter = itertools.count(1)

_REPLY_TOOL: dict[str, Any] = {
    "name": "reply",
    "description": (
        "Send a reply to the user over the voice channel. Use kind='narrate' for "
        "short progress updates that should be spoken immediately; kind='tool_use' "
        "for a brief header like '[EDIT] foo.py'; kind='assistant' (or omit) for "
        "regular replies that appear in the chat bubble and are spoken at end of turn."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to display and speak"},
            "kind": {
                "type": "string",
                "enum": ["assistant", "narrate", "tool_use"],
                "description": "Routing hint for the TUI (default: assistant)",
            },
        },
        "required": ["text"],
    },
}

_ASK_MENU_TOOL: dict[str, Any] = {
    "name": "ask_menu",
    "description": (
        "Ask the user to pick one option from a short list. Use this ONLY when "
        "the choice genuinely can't be phrased as yes/no — yes/no can be "
        "answered by voice, but a multi-choice menu requires the user to click "
        "in the TUI. Returns the selected option's index (0-based) and label, "
        "or 'cancelled' if dismissed. Keep options to 4 or fewer."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Short question shown at the top of the menu modal",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 6,
                "description": "Labels for each selectable option",
            },
        },
        "required": ["question", "options"],
    },
}

_NOTIFY_ACTION_TOOL: dict[str, Any] = {
    "name": "notify_action",
    "description": (
        "Announce a meaningful tool action BEFORE performing it. Renders as a "
        "structured tool-use segment in the chat bubble (typed by action_type "
        "for cleaner display than reply(kind='tool_use')). Use this for Edit, "
        "Write, Bash, and other approval-required tools — skip for cheap reads "
        "(Read/Glob/Grep). Examples: "
        "notify_action(action_type='edit', target='auth.py', summary='use bcrypt'); "
        "notify_action(action_type='bash', target='pytest tests/test_voice.py')."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action_type": {
                "type": "string",
                "enum": ["edit", "write", "read", "bash", "search", "delete", "other"],
                "description": "Category of the action",
            },
            "target": {
                "type": "string",
                "description": "What's being acted on (file path, command, search term)",
            },
            "summary": {
                "type": "string",
                "description": "Optional one-phrase intent (omit for terse output)",
            },
        },
        "required": ["action_type", "target"],
    },
}

_SET_STATUS_TOOL: dict[str, Any] = {
    "name": "set_status",
    "description": (
        "Update the crab visualiser's status label to reflect what you're "
        "currently doing — replaces the default random crab puns ('Snipping...', "
        "'Pondering...') with a short descriptive phrase. Useful during longer "
        "operations so the user knows what's happening. Pass label='' to revert "
        "to the default puns. The state machine (idle/listening/thinking) is "
        "system-controlled; this only flavours the label below the crab."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "description": "Short status phrase, or empty string to revert",
            },
        },
        "required": ["label"],
    },
}

# Pending ask_menu calls: request_id → Future that resolves with the selected int.
_pending_menus: dict[str, asyncio.Future[int]] = {}


def _new_request_id() -> str:
    """5-char lowercase id, matching the format Claude uses for permission ids."""
    alphabet = string.ascii_lowercase.replace("l", "")  # avoid 1/l confusion
    return "".join(secrets.choice(alphabet) for _ in range(5))


# ---------------------------------------------------------------------------
# stdin / stdout plumbing — single writer, no interleaving
# ---------------------------------------------------------------------------

_stdout_lock = asyncio.Lock()


async def _stdin_lines() -> AsyncIterator[str]:
    """Async iterator over Claude's JSON-RPC lines on our stdin."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    while True:
        line = await reader.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        if text:
            yield text


async def _send_jsonrpc(msg: dict[str, Any]) -> None:
    async with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Inbound from Claude (stdin) — dispatch
# ---------------------------------------------------------------------------

async def _claude_inbound_loop(sock_writer: asyncio.StreamWriter) -> None:
    async for raw in _stdin_lines():
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("bad JSON from claude: %s", e)
            continue

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            await _send_jsonrpc({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2025-11-25"),
                    "capabilities": {
                        "tools": {},
                        "experimental": {
                            "claude/channel": {},
                            "claude/channel/permission": {},
                        },
                    },
                    "serverInfo": {"name": "crab", "version": "0.1.0"},
                },
            })
        elif method == "notifications/initialized":
            log.info("claude initialized")
        elif method == "tools/list":
            await _send_jsonrpc({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        _REPLY_TOOL,
                        _ASK_MENU_TOOL,
                        _NOTIFY_ACTION_TOOL,
                        _SET_STATUS_TOOL,
                    ],
                },
            })
        elif method == "tools/call":
            await _handle_tool_call(msg_id, params, sock_writer)
        elif method == "notifications/claude/channel/permission_request":
            await bridge.send_message(sock_writer, {
                "type": bridge.PERMISSION_REQUEST,
                "request_id": params.get("request_id", ""),
                "tool_name": params.get("tool_name", ""),
                "description": params.get("description", ""),
                "input_preview": params.get("input_preview", ""),
            })
        elif msg_id is not None:
            await _send_jsonrpc({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"unhandled method: {method}"},
            })
        # else: notification we don't recognise — silently drop

    log.info("claude closed stdin")


async def _handle_tool_call(
    msg_id: Any,
    params: dict[str, Any],
    sock_writer: asyncio.StreamWriter,
) -> None:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "reply":
        n = next(_tool_counter)
        _dlog(f"tool_call #{n} reply kind={args.get('kind')!r} text={args.get('text', '')!r}")
        await bridge.send_message(sock_writer, {
            "type": bridge.REPLY,
            "text": args.get("text", ""),
            "kind": args.get("kind"),
        })
        await _send_jsonrpc({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": "ok"}]},
        })
    elif name == "ask_menu":
        await _handle_ask_menu(msg_id, args, sock_writer)
    elif name == "notify_action":
        n = next(_tool_counter)
        _dlog(f"tool_call #{n} notify_action {args!r}")
        await bridge.send_message(sock_writer, {
            "type": bridge.NOTIFY_ACTION,
            "action_type": (args.get("action_type") or "other").lower(),
            "target": args.get("target") or "",
            "summary": args.get("summary") or "",
        })
        await _send_jsonrpc({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": "ok"}]},
        })
    elif name == "set_status":
        n = next(_tool_counter)
        _dlog(f"tool_call #{n} set_status label={args.get('label')!r}")
        await bridge.send_message(sock_writer, {
            "type": bridge.STATUS_UPDATE,
            "label": args.get("label") or "",
        })
        await _send_jsonrpc({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": "ok"}]},
        })
    else:
        await _send_jsonrpc({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"unknown tool: {name}"},
        })


async def _handle_ask_menu(
    msg_id: Any,
    args: dict[str, Any],
    sock_writer: asyncio.StreamWriter,
) -> None:
    """Forward an ask_menu tool call to the parent and await the user's pick."""
    question = (args.get("question") or "").strip()
    options = args.get("options") or []
    if not question or len(options) < 2:
        await _send_jsonrpc({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32602, "message": "ask_menu needs a question and ≥2 options"},
        })
        return

    request_id = _new_request_id()
    future: asyncio.Future[int] = asyncio.get_event_loop().create_future()
    _pending_menus[request_id] = future
    n = next(_tool_counter)
    _dlog(f"tool_call #{n} ask_menu id={request_id} q={question!r} opts={options!r}")

    await bridge.send_message(sock_writer, {
        "type": bridge.MENU_REQUEST,
        "request_id": request_id,
        "question": question,
        "options": options,
    })

    try:
        selected = await future
    except asyncio.CancelledError:
        _pending_menus.pop(request_id, None)
        raise

    if selected < 0 or selected >= len(options):
        result_text = "cancelled"
    else:
        result_text = f"selected index {selected}: {options[selected]!r}"

    _dlog(f"ask_menu {request_id} resolved: {result_text}")
    await _send_jsonrpc({
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {"content": [{"type": "text", "text": result_text}]},
    })


# ---------------------------------------------------------------------------
# Inbound from parent CrabApp (Unix socket) — forward as MCP notifications
# ---------------------------------------------------------------------------

async def _parent_inbound_loop(sock_reader: asyncio.StreamReader) -> None:
    while True:
        msg = await bridge.recv_message(sock_reader)
        if msg is None:
            log.info("parent socket closed")
            return

        t = msg.get("type")
        if t == bridge.PUSH_PROMPT:
            n = next(_notif_counter)
            _dlog(f"push→notif #{n} content={msg.get('content', '')!r} meta={msg.get('meta')}")
            await _send_jsonrpc({
                "jsonrpc": "2.0",
                "method": "notifications/claude/channel",
                "params": {
                    "content": msg.get("content", ""),
                    "meta": msg.get("meta") or {},
                },
            })
        elif t == bridge.PERMISSION_VERDICT:
            await _send_jsonrpc({
                "jsonrpc": "2.0",
                "method": "notifications/claude/channel/permission",
                "params": {
                    "request_id": msg["request_id"],
                    "behavior": msg["behavior"],
                },
            })
        elif t == bridge.MENU_RESPONSE:
            request_id = msg.get("request_id", "")
            selected = int(msg.get("selected", -1))
            future = _pending_menus.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(selected)
            else:
                log.warning("menu_response with no pending future: %s", request_id)
        elif t == bridge.SHUTDOWN:
            log.info("parent requested shutdown")
            return
        else:
            log.warning("unknown bridge message type: %s", t)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    log.info("starting; connecting to parent socket %s", bridge.SOCKET_PATH)
    try:
        sock_reader, sock_writer = await bridge.connect_to_parent()
    except Exception as e:
        log.error("could not connect to parent: %s", e)
        sys.exit(1)
    log.info("connected to parent")

    claude_task = asyncio.create_task(_claude_inbound_loop(sock_writer), name="claude-in")
    parent_task = asyncio.create_task(_parent_inbound_loop(sock_reader), name="parent-in")

    _, pending = await asyncio.wait(
        {claude_task, parent_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()

    try:
        sock_writer.close()
        await sock_writer.wait_closed()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
