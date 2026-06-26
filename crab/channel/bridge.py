"""Unix-socket bridge between the Crab Textual TUI (parent) and the MCP channel server (child).

The parent CrabApp owns a Unix socket; the MCP server (spawned by Claude Code as
its stdio MCP child) connects back to it. All traffic is line-delimited JSON.

  CrabApp (parent)  ←Unix socket→  crab.channel.server  ←stdio (MCP)→  Claude

Wire format (one JSON object per line, UTF-8, terminated with \\n):

  parent → child:
    {"type": "push_prompt",        "content": str, "meta": {str: str}}
    {"type": "permission_verdict", "request_id": str, "behavior": "allow" | "deny"}
    {"type": "menu_response",      "request_id": str, "selected": int}
    {"type": "shutdown"}
  child → parent:
    {"type": "ready"}                                                    handshake
    {"type": "reply",              "text": str, "kind": str | None}      kind ∈ {assistant, narrate, tool_use, None}
    {"type": "permission_request", "request_id": str, "tool_name": str,
                                   "description": str, "input_preview": str}
    {"type": "menu_request",       "request_id": str, "question": str,
                                   "options": [str]}
    {"type": "notify_action",      "action_type": str, "target": str,
                                   "summary": str}
    {"type": "status_update",      "label": str}
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

SOCKET_PATH = Path("/tmp/crab-bot.sock")

# Message type constants (use these instead of string literals)
PUSH_PROMPT = "push_prompt"
PERMISSION_VERDICT = "permission_verdict"
MENU_RESPONSE = "menu_response"
SHUTDOWN = "shutdown"
READY = "ready"
REPLY = "reply"
PERMISSION_REQUEST = "permission_request"
MENU_REQUEST = "menu_request"
NOTIFY_ACTION = "notify_action"
STATUS_UPDATE = "status_update"


async def send_message(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    """Write one JSON object followed by newline."""
    writer.write((json.dumps(msg) + "\n").encode("utf-8"))
    await writer.drain()


async def recv_message(reader: asyncio.StreamReader) -> Optional[dict[str, Any]]:
    """Read one JSON line. Returns None on clean EOF."""
    line = await reader.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


async def connect_to_parent(
    path: Path = SOCKET_PATH,
    timeout: float = 5.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Child-side: connect to the parent's Unix socket and send a READY handshake.

    Retries every 100 ms until `timeout` elapses (parent may still be starting up
    when Claude spawns us).
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_err: Optional[Exception] = None
    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as e:
            last_err = e
            await asyncio.sleep(0.1)
            continue
        await send_message(writer, {"type": READY})
        return reader, writer
    raise RuntimeError(
        f"could not connect to crab parent socket at {path} within {timeout}s: {last_err}"
    )


ConnectionHandler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


async def serve_parent(
    handler: ConnectionHandler,
    path: Path = SOCKET_PATH,
) -> asyncio.AbstractServer:
    """Parent-side: start a Unix socket server.

    `handler(reader, writer)` is awaited once per inbound connection. Removes
    stale socket files; if a *live* listener is detected on the path, raises
    rather than silently shadowing it (which would leave the user wondering
    why the MCP child never connected).
    """
    if path.exists():
        # Probe — is anyone actually listening on this socket?
        try:
            _r, w = await asyncio.open_unix_connection(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            # Stale file from a previous crash — safe to remove.
            pass
        else:
            try:
                w.close()
                await w.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"another process is already listening on {path}. Kill the "
                f"stale voice_controller (or its MCP child) before retrying. "
                f"Try: lsof {path}  (and `kill -9 <pid>`)"
            )
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return await asyncio.start_unix_server(handler, path=str(path))
