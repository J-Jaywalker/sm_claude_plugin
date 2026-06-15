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
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

from crab.channel import bridge

# Stdout is reserved for JSON-RPC; all diagnostics go to stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [crab-channel] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("crab.channel.server")

_DEBUG_LOG = Path("/tmp/crab-channel-debug.log")


def _dlog(msg: str) -> None:
    try:
        with _DEBUG_LOG.open("a") as f:
            f.write(f"{time.time():.3f}  server  {msg}\n")
    except OSError:
        pass


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
                "result": {"tools": [_REPLY_TOOL]},
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
    else:
        await _send_jsonrpc({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"unknown tool: {name}"},
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
