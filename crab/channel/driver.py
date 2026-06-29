"""Channels-mode driver: hidden-PTY Claude + Unix socket bridge.

Starts a single long-running Claude session under a hidden PTY, owns the Unix
socket the MCP child connects to, and pumps messages in both directions:
voice prompts → channel notification, permission requests → voice yes/no →
verdict, reply tool calls → chat bubble + TTS, ask_menu → click modal.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import pty
import signal
import time
import uuid
from pathlib import Path
from typing import Optional

from crab.asr.controller import VoiceController
from crab.asr.parsers.menu_select import llm_interpret_menu
from crab.asr.parsers.yes_no import parse_yes_no
from crab.channel import bridge
from crab.config import dlog
from crab.ui.protocol import _UI

log = logging.getLogger("crab.channel.driver")

# Maps notify_action's typed action_type to the "[PREFIX]" label the bubble
# renderer displays as a tool-use segment.
_ACTION_PREFIXES: dict[str, str] = {
    "edit": "EDIT",
    "write": "WRITE",
    "read": "READ",
    "bash": "BASH",
    "search": "SEARCH",
    "delete": "DELETE",
    "other": "ACTION",
}

# Where the hidden PTY's stdout is captured for debugging.
_PTY_LOG = Path("/tmp/crab-claude.pty.log")
_MCP_CONFIG = Path(__file__).parent / "mcp.json"
# crab/channel/driver.py → crab/channel/ → crab/ → project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _dlog(msg: str) -> None:
    dlog("driver", msg)


async def channel_driver(
    controller: VoiceController,
    ui: _UI,
    stop_event: asyncio.Event,
) -> None:
    """Run Claude in a hidden PTY and bridge it to the UI over a Unix socket."""
    loop = asyncio.get_event_loop()

    # ── Parent socket server ────────────────────────────────────────────────
    connection_future: asyncio.Future[
        tuple[asyncio.StreamReader, asyncio.StreamWriter]
    ] = loop.create_future()

    async def _on_connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        hello = await bridge.recv_message(reader)
        if hello != {"type": bridge.READY}:
            ui.add_error_message(f"[CHANNEL] bad handshake: {hello!r}")
            writer.close()
            return
        if connection_future.done():
            # A second MCP child shouldn't normally appear; drop it.
            writer.close()
            return
        connection_future.set_result((reader, writer))
        # Hold the connection open; the main loop owns the streams.
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    sock_server = await bridge.serve_parent(_on_connection)

    # ── Spawn Claude under a PTY ────────────────────────────────────────────
    _PTY_LOG.write_bytes(b"")
    master_fd, slave_fd = pty.openpty()
    env = {**os.environ, "TERM": "xterm-256color"}
    cmd = [
        "claude",
        "--mcp-config", str(_MCP_CONFIG),
        "--strict-mcp-config",
        # Pre-allow all of our own channel-infrastructure tools so they don't
        # round-trip through the voice permission relay. Edit/Write/Bash and
        # other Claude built-ins still go through the relay.
        "--allowedTools",
        "mcp__crab__reply,mcp__crab__ask_menu,mcp__crab__notify_action,mcp__crab__set_status",
        "--dangerously-load-development-channels", "server:crab",
    ]
    log.info("spawning: %s", " ".join(cmd))
    _dlog(f"spawning claude cwd={_PROJECT_ROOT}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        cwd=str(_PROJECT_ROOT),
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    pty_log_file = _PTY_LOG.open("ab")
    tasks: list[asyncio.Task] = []

    # Drain the PTY into the log file so it doesn't fill the kernel buffer.
    def _on_pty_readable() -> None:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            loop.remove_reader(master_fd)
            return
        if not data:
            loop.remove_reader(master_fd)
            return
        pty_log_file.write(data)
        pty_log_file.flush()

    loop.add_reader(master_fd, _on_pty_readable)

    # Dismiss the one-time dev-channels confirmation dialog with Enter.
    async def _confirm_dev_channels() -> None:
        await asyncio.sleep(2.5)
        try:
            os.write(master_fd, b"\r")
        except OSError:
            pass

    tasks.append(asyncio.create_task(_confirm_dev_channels(), name="dev-confirm"))

    # ── Wait for the MCP child to connect ───────────────────────────────────
    try:
        sock_reader, sock_writer = await asyncio.wait_for(
            connection_future, timeout=60.0
        )
    except asyncio.TimeoutError:
        # Claude either failed to start at all, never spawned our MCP child,
        # or the child crashed before sending its handshake.
        if proc.returncode is not None:
            cause = f"Claude exited (rc={proc.returncode}) before the MCP child connected"
        else:
            cause = "Claude is still running but never spawned the MCP child"
        ui.add_error_message(
            f"[CHANNEL] timed out after 60s — {cause}. "
            f"Check /tmp/crab-claude.pty.log for Claude's UI output (and "
            f"/tmp/crab-channel-debug.log if DEBUG=1)."
        )
        await _teardown(proc, master_fd, sock_server, pty_log_file, tasks, None)
        return

    log.info("MCP child connected; pumps online")

    # ── Pump prompts (parent → channel) ─────────────────────────────────────
    push_counter = itertools.count(1)

    async def _prompts_to_channel() -> None:
        while not stop_event.is_set():
            try:
                await controller.prompt_ready.wait()
            except asyncio.CancelledError:
                return
            controller.prompt_ready.clear()
            text = controller.last_prompt
            if not text:
                continue
            controller.listening.clear()
            n = next(push_counter)
            message_id = uuid.uuid4().hex[:12]
            _dlog(f"push_prompt #{n} id={message_id} text={text!r}")
            try:
                await bridge.send_message(sock_writer, {
                    "type": bridge.PUSH_PROMPT,
                    "content": text,
                    "meta": {
                        "user": "crab",
                        "chat_id": "crab",
                        "message_id": message_id,
                        "ts": str(time.time()),
                    },
                })
            except Exception as e:  # noqa: BLE001
                ui.add_error_message(f"[CHANNEL] failed to push prompt: {e}")
                return

    # ── Pump replies + permission requests (channel → parent) ──────────────
    async def _channel_to_ui() -> None:
        while not stop_event.is_set():
            msg = await bridge.recv_message(sock_reader)
            if msg is None:
                log.info("MCP child socket closed")
                return
            t = msg.get("type")
            if t == bridge.REPLY:
                kind = msg.get("kind") or "assistant"
                text = msg.get("text", "")
                if kind == "tool_use":
                    ui.add_tool_use(text)
                elif kind == "narrate":
                    # Mid-task progress: speak immediately AND show in bubble.
                    # The text comes plain (no <narrate> tags), so we drive
                    # TTS directly rather than relying on tag-stream parsing.
                    ui.add_assistant_text(text)
                    ui.speak(text)
                else:  # assistant
                    ui.add_assistant_text(text)
                    # Await the <tts> clip (which queues behind any narrate
                    # still playing) so the mic doesn't reopen while we're
                    # still speaking.
                    await ui.finalise_assistant_turn()
                    ui.set_status("idle")
                    controller.listening.set()
                    controller.response_done.set()
            elif t == bridge.PERMISSION_REQUEST:
                await _relay_permission(ui, controller, sock_writer, msg)
            elif t == bridge.MENU_REQUEST:
                # Fire-and-forget: the modal can sit open while other
                # messages flow. Server-side correlates by request_id.
                asyncio.create_task(
                    _relay_menu(ui, controller, sock_writer, msg),
                    name=f"menu-{msg.get('request_id', '?')}",
                )
            elif t == bridge.NOTIFY_ACTION:
                action_type = (msg.get("action_type") or "other").lower()
                target = msg.get("target") or ""
                summary = msg.get("summary") or ""
                prefix = _ACTION_PREFIXES.get(action_type, "ACTION")
                label = f"[{prefix}] {target}"
                if summary:
                    label = f"{label} — {summary}"
                ui.add_tool_use(label)
            elif t == bridge.STATUS_UPDATE:
                ui.set_label(msg.get("label") or "")

    tasks.append(asyncio.create_task(_prompts_to_channel(), name="prompts→channel"))
    tasks.append(asyncio.create_task(_channel_to_ui(), name="channel→ui"))
    stop_task = asyncio.create_task(stop_event.wait(), name="channel-stop")
    tasks.append(stop_task)

    try:
        done, _ = await asyncio.wait(
            {tasks[-3], tasks[-2], stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for d in done:
            if (exc := d.exception()) is not None:
                ui.add_error_message(f"[CHANNEL] task crashed: {exc}")
    except asyncio.CancelledError:
        raise
    finally:
        await _teardown(proc, master_fd, sock_server, pty_log_file, tasks, sock_writer)


_PERMISSION_TIMEOUT_S = 20.0


async def _relay_permission(
    ui: _UI,
    controller: VoiceController,
    sock_writer: asyncio.StreamWriter,
    msg: dict,
) -> None:
    """Forward a permission_request to the user via TTS+ASR, send verdict back."""
    request_id = msg.get("request_id", "")
    tool_name = msg.get("tool_name", "")
    description = msg.get("description", "")
    input_preview = msg.get("input_preview", "")

    _dlog(f"permission_request id={request_id} tool={tool_name}")

    # 1. Visual: show the ask in the UI
    ui.add_tool_use(f"[ASK] {tool_name} — {description}")
    if input_preview:
        truncated = input_preview if len(input_preview) <= 160 else input_preview[:160] + "…"
        ui.add_tool_use(f"  ↳ {truncated}")

    # 2. Speak the question and BLOCK until TTS finishes. Mic stays disabled
    #    during playback so Speechmatics doesn't transcribe our own audio.
    question = f"Allow {tool_name}? {description}".strip()
    await ui.speak_and_wait(question)

    # 3. Cue the user — chime + visualizer flip, then open the mic.
    ui.set_status("listening")
    controller.listening.set()
    controller.begin_permission_listen()

    timed_out = False
    try:
        await asyncio.wait_for(
            controller.permission_received.wait(),
            timeout=_PERMISSION_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        timed_out = True

    answer = controller.end_permission_listen()
    controller.listening.clear()
    ui.set_status("thinking")

    if timed_out:
        ui.add_error_message(
            f"[CHANNEL] timed out waiting for approval of {tool_name} — denying"
        )
        verdict = "deny"
    else:
        verdict = parse_yes_no(answer) or ""
        if not verdict:
            ui.add_error_message(
                f"[CHANNEL] couldn't parse {answer!r} as yes/no — denying {tool_name}"
            )
            verdict = "deny"
        else:
            ui.add_tool_use(f"[VERDICT] {verdict} ({answer!r})")

    _dlog(f"sending verdict id={request_id} behavior={verdict}")
    await bridge.send_message(sock_writer, {
        "type": bridge.PERMISSION_VERDICT,
        "request_id": request_id,
        "behavior": verdict,
    })


async def _relay_menu(
    ui: _UI,
    controller: VoiceController,
    sock_writer: asyncio.StreamWriter,
    msg: dict,
) -> None:
    """Race click-modal vs voice answer; send back the chosen index.

    Flow:
      1. Show the question + options visually in the chat
      2. Speak the question via TTS, blocking the mic
      3. Chime + flip visualiser to listening + open the mic
      4. Race the modal click and the controller's menu_received event;
         first to resolve wins (modal dismisses either way)
      5. Close the mic, pop chime, send the verdict back
    """
    request_id = msg.get("request_id", "")
    question = msg.get("question", "")
    options = list(msg.get("options") or [])
    _dlog(f"menu_request id={request_id} q={question!r} opts={options!r}")

    ui.add_tool_use(f"[MENU] {question}")
    for i, opt in enumerate(options):
        ui.add_tool_use(f"  {i + 1}. {opt}")

    # Block while TTS plays so we don't transcribe our own audio.
    await ui.speak_and_wait(question)

    # Open the listening window. set_status("listening") fires the Ping chime
    # and flips the visualiser to green; begin_menu_listen routes voice into
    # the menu-select parser instead of the wake-word state machine.
    controller.begin_menu_listen(options)
    controller.listening.set()
    ui.set_status("listening")

    voice_future: asyncio.Future[int] = asyncio.get_event_loop().create_future()

    async def _watch_voice() -> None:
        """React to each EoU until we resolve voice_future or the modal closes.

        Loop iterations:
          - Rule-based parser matched (>=0 or CANCEL) → resolve, done.
          - Rule-based returned -2 → close the mic, ask the LLM, then either
            resolve or reopen the listening window for the user to retry.
        """
        while not voice_future.done():
            await controller.menu_received.wait()
            controller.menu_received.clear()
            ans = controller.menu_answer
            spoken = controller.menu_spoken_text

            if ans != -2:
                # Clean rule-based decision (real index, or CANCEL).
                if not voice_future.done():
                    voice_future.set_result(ans)
                return

            # Rule-based couldn't match — pause listening while we ask the LLM.
            controller.menu_listening = False
            controller.listening.clear()
            ui.set_status("thinking")
            ui.add_tool_use(f"[MENU] interpreting {spoken!r}...")

            llm_idx = await llm_interpret_menu(spoken, options)

            if llm_idx is not None:
                ui.add_tool_use(f"[MENU] LLM picked option {llm_idx}")
                if not voice_future.done():
                    voice_future.set_result(llm_idx)
                return

            # LLM also couldn't decide. Show an error, reopen the listening
            # window, and let the user try again or click.
            ui.add_error_message(
                f"[MENU] couldn't tell from {spoken!r} — try again or click."
            )
            controller.begin_menu_listen(options)
            controller.listening.set()
            ui.set_status("listening")

    voice_task = asyncio.create_task(_watch_voice(), name=f"menu-voice-{request_id}")

    try:
        selected = await ui.show_menu(question, options, external_result=voice_future)
    except Exception as e:  # noqa: BLE001
        ui.add_error_message(f"[CHANNEL] menu modal failed: {e}")
        selected = -1
    finally:
        voice_task.cancel()
        controller.end_menu_listen()
        controller.listening.clear()
        # set_status("thinking") fires the Pop chime so the user knows the
        # listening window has closed and Claude is now processing.
        ui.set_status("thinking")

    if 0 <= selected < len(options):
        ui.add_tool_use(f"[CHOICE] {options[selected]}")
    else:
        ui.add_tool_use("[CHOICE] (cancelled)")

    _dlog(f"sending menu_response id={request_id} selected={selected}")
    await bridge.send_message(sock_writer, {
        "type": bridge.MENU_RESPONSE,
        "request_id": request_id,
        "selected": selected,
    })


async def _teardown(
    proc: asyncio.subprocess.Process,
    master_fd: int,
    sock_server: asyncio.AbstractServer,
    pty_log_file,
    tasks: list[asyncio.Task],
    sock_writer: Optional[asyncio.StreamWriter],
) -> None:
    loop = asyncio.get_event_loop()

    if sock_writer is not None:
        try:
            await bridge.send_message(sock_writer, {"type": bridge.SHUTDOWN})
        except Exception:  # noqa: BLE001
            pass
        try:
            sock_writer.close()
            await sock_writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    for t in tasks:
        if not t.done():
            t.cancel()

    try:
        loop.remove_reader(master_fd)
    except Exception:  # noqa: BLE001
        pass

    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    try:
        os.close(master_fd)
    except OSError:
        pass

    try:
        pty_log_file.close()
    except Exception:  # noqa: BLE001
        pass

    sock_server.close()
    try:
        await sock_server.wait_closed()
    except Exception:  # noqa: BLE001
        pass
