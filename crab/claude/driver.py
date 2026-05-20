"""Async driver that spawns `claude -p` and forwards stream events."""

from __future__ import annotations

import asyncio
import json
import os

from crab.asr.controller import VoiceController
from crab.claude.stream import _handle_stream_event
from crab.config import _ANSI_ESCAPE, _DEBUG, _load_system_prompt
from crab.ui.protocol import _UI


async def claude_driver(
    controller: VoiceController,
    ui: _UI,
    stop_event: asyncio.Event,
) -> None:
    """Run `claude -p` for each assembled prompt, streaming output to the UI.

    Clears controller.listening while Claude runs so the audio pump drops
    frames, then restores it when done.
    """
    child_env = {k: v for k, v in os.environ.items() if k != "DEBUG"}
    cwd = os.getcwd()
    base_prompt = _load_system_prompt()
    system_prompt = f"{base_prompt}\n\nWorking directory: {cwd}".strip()
    first_prompt = True

    while not stop_event.is_set():
        await controller.prompt_ready.wait()
        controller.prompt_ready.clear()
        prompt_text = controller.last_prompt
        if not prompt_text:
            continue

        controller.listening.clear()

        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--verbose",
            "--output-format", "stream-json",
        ]
        if system_prompt:
            cmd += ["--system-prompt", system_prompt]
        if not first_prompt:
            cmd.append("--continue")
        cmd += ["-p", prompt_text]
        first_prompt = False

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=child_env,
                cwd=os.getcwd(),
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    clean = _ANSI_ESCAPE.sub("", line)
                    if clean:
                        ui.add_tool_use(clean)
                    continue
                _handle_stream_event(event, ui)
            await proc.wait()
        except asyncio.CancelledError:
            if proc is not None:
                proc.terminate()
            raise
        except Exception as exc:
            ui.add_error_message(f"[CLAUDE] Error: {exc}")

        ui.set_status("idle")
        controller.listening.set()
        controller.response_done.set()
