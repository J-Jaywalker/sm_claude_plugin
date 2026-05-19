"""Voice controller: wake word + accumulate + end-of-utterance + Claude integration.

Streams microphone audio to Speechmatics RT ASR. On first run, enrolls the
speaker by capturing 30 seconds of audio and saving identifiers to speakers.txt.
On subsequent runs, loads enrolled speakers and ignores transcripts from
unrecognised voices. Detects wake phrase "CRAB-BOT" in finals, accumulates
until EndOfUtterance, then submits the prompt to Claude Code via `claude -p`.

Usage:
    SPEECHMATICS_API_KEY=... python voice_controller.py
    DEBUG=1 SPEECHMATICS_API_KEY=... python voice_controller.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from collections import deque
from enum import Enum
from typing import Any

from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    AuthenticationError,
    ClientMessageType,
    ConversationConfig,
    Microphone,
    OperatingPoint,
    ServerMessageType,
    SpeakerDiarizationConfig,
    SpeakerIdentifier,
    TranscriptionConfig,
)


_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_WAKE_WORD_PATTERN = re.compile(r"\bcrab[\s\-]+bot\b")
_DEBUG = bool(os.environ.get("DEBUG"))
_IDLE_BUFFER_MAX = 120

_SPEAKERS_FILE = os.path.join(os.path.dirname(__file__), "speakers.txt")
_ENROLLMENT_SECONDS = 30

_RT_URL = "ws://127.0.0.1:9002/v2" if os.environ.get("SM_LOCAL_CLAUDE_TRANSCRIPTION") else None

_SYSTEM_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "system_prompt.txt")

def _load_system_prompt() -> str:
    try:
        with open(_SYSTEM_PROMPT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

_DOT_INTERVAL = 0.2

_CRAB_ART_FILE = os.path.join(os.path.dirname(__file__), "crab_art.txt")


def _load_crab_art() -> dict[str, Any]:
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    try:
        with open(_CRAB_ART_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line.startswith("[") and line.endswith("]"):
                    if current is not None:
                        sections[current] = "\n".join(lines).strip("\n")
                    current = line[1:-1]
                    lines = []
                elif current is not None:
                    lines.append(line)
        if current is not None:
            sections[current] = "\n".join(lines).strip("\n")
    except FileNotFoundError:
        pass
    return {
        "idle":      sections.get("idle",       "彡(-.-)ミ\n  ^   ^"),
        "listening": sections.get("listening",  "彡(ᵔᵕᵔ)ミ\n  ^   ^"),
        "thinking":  [
            sections.get("thinking_0", "彡('o')ミ"),
            sections.get("thinking_1", "彡('o')ミ"),
        ],
    }


_CRAB_ART = _load_crab_art()


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

class UI:
    """Full-screen terminal UI: status bar, conversation history, prompt strip."""

    def __init__(self) -> None:
        self._status = "idle"
        self._last_prompt = ""
        self._partial = ""
        self._history: deque[Any] = deque(maxlen=30)
        self._frame = 0
        self._live: Live | None = None
        self._pulse_task: asyncio.Task[None] | None = None
        self._layout = self._make_layout()

    # -- Layout --------------------------------------------------------------

    def _make_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=12),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["top"].split_row(
            Layout(name="visualiser", size=26),
            Layout(name="instructions"),
        )
        return layout

    # -- Renderers -----------------------------------------------------------

    def _visualiser_panel(self) -> Panel:
        if self._status == "idle":
            art = _CRAB_ART["idle"]
            label = "Idle"
            color = "bright_red"
        elif self._status == "listening":
            art = _CRAB_ART["listening"]
            label = "Listening..."
            color = "bright_green"
        else:
            art = _CRAB_ART["thinking"][(self._frame // 5) % 2]
            label = "Thinking..."
            color = "bright_yellow"
        t = Text(art, style=color, justify="center")
        return Panel(
            Align.center(t, vertical="middle"),
            title="[bold]CRAB VISUALISER[/bold]",
            subtitle=f"[{color}]{label}[/{color}]",
            border_style=color,
        )

    def _instructions_panel(self) -> Panel:
        t = Text(justify="left")
        t.append("How to use\n\n", style="bold")
        t.append("1. ", style="dim"); t.append('Say "CRAB-BOT"\n')
        t.append("2. ", style="dim"); t.append("Speak your command naturally\n")
        t.append("3. ", style="dim"); t.append("Pause — end of speech is detected automatically\n")
        t.append("4. ", style="dim"); t.append("Wait for Claude to respond\n")
        t.append("5. ", style="dim"); t.append('Say "CRAB-BOT" again for your next command')
        return Panel(Align.center(t, vertical="middle"), title="[bold]C.R.A.B — Claude Realtime Audio Bot[/bold]", border_style="dim")

    def _body_panel(self) -> Panel:
        content: Any = (
            Group(*list(self._history))
            if self._history
            else Align.center(Text("No conversation yet.", style="dim"))
        )
        return Panel(content, title="[dim]Conversation[/dim]", border_style="dim")

    def _footer_panel(self) -> Panel:
        display = self._partial or self._last_prompt
        t = Text()
        t.append("> ", style="dim")
        t.append(display)
        return Panel(t, border_style="dim")

    # -- Refresh -------------------------------------------------------------

    def _refresh(self) -> None:
        self._layout["visualiser"].update(self._visualiser_panel())
        self._layout["instructions"].update(self._instructions_panel())
        self._layout["body"].update(self._body_panel())
        self._layout["footer"].update(self._footer_panel())
        if self._live:
            self._live.refresh()

    async def _pulse(self) -> None:
        while True:
            self._frame += 1
            self._refresh()
            await asyncio.sleep(_DOT_INTERVAL)

    # -- Lifecycle -----------------------------------------------------------

    def start(self, live: Live) -> None:
        self._live = live
        try:
            self._pulse_task = asyncio.create_task(self._pulse(), name="ui-pulse")
        except RuntimeError:
            pass

    def stop(self) -> None:
        if self._pulse_task and not self._pulse_task.done():
            self._pulse_task.cancel()
        self._pulse_task = None

    # -- State mutations -----------------------------------------------------

    def set_status(self, status: str) -> None:
        self._status = status
        if status != "listening":
            self._partial = ""
        self._refresh()

    def set_partial(self, text: str) -> None:
        self._partial = text
        self._refresh()

    def add_user_message(self, text: str) -> None:
        self._last_prompt = text
        self._partial = ""
        t = Text()
        t.append("You: ", style="bold cyan")
        t.append(text)
        self._history.append(t)
        self._refresh()

    def add_assistant_text(self, text: str) -> None:
        clean = _ANSI_ESCAPE.sub("", text)
        self._history.append(Markdown(clean, code_theme="ansi_dark"))
        self._refresh()

    def add_tool_use(self, label: str) -> None:
        self._history.append(Text(label, style="dim"))
        self._refresh()

    def add_error_message(self, text: str) -> None:
        self._history.append(Text(text, style="bright_red"))
        self._refresh()


# ---------------------------------------------------------------------------
# Speaker store
# ---------------------------------------------------------------------------

def _load_speakers() -> dict[str, list[str]]:
    """Load enrolled speakers from file. Returns {name: [identifier, ...]}."""
    speakers: dict[str, list[str]] = {}
    try:
        with open(_SPEAKERS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                name, ids_str = line.split(":", 1)
                ids = [i for i in ids_str.split(",") if i]
                if name and ids:
                    speakers[name] = ids
    except FileNotFoundError:
        pass
    return speakers


def _save_speakers(speakers: dict[str, list[str]]) -> None:
    with open(_SPEAKERS_FILE, "w") as f:
        for name, ids in speakers.items():
            f.write(f"{name}:{','.join(ids)}\n")


def _dominant_speaker(results: list[dict[str, Any]]) -> str | None:
    """Return the most-frequent speaker label across all word alternatives."""
    counts: dict[str, int] = {}
    for result in results:
        for alt in result.get("alternatives", []):
            spk = alt.get("speaker")
            if spk:
                counts[spk] = counts.get(spk, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


# ---------------------------------------------------------------------------
# Normalisation / wake word
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase and collapse punctuation/whitespace for wake word matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class _State(str, Enum):
    IDLE = "IDLE"
    ACCUMULATING = "ACCUMULATING"


class VoiceController:
    """Coordinates microphone capture, ASR events, and prompt assembly."""

    def __init__(self, enrolled_labels: set[str], ui: UI) -> None:
        self._ui = ui
        self.enrolled_labels = enrolled_labels
        self.state: _State = _State.IDLE
        self.buffer: list[str] = []
        self._idle_buffer: str = ""
        self.listening: asyncio.Event = asyncio.Event()
        self.listening.set()
        self.prompt_ready: asyncio.Event = asyncio.Event()
        self.last_prompt: str = ""

    def handle_final(self, message: dict[str, Any]) -> None:
        """Handle an ADD_TRANSCRIPT (final) server message."""
        transcript = message.get("metadata", {}).get("transcript", "")
        if not transcript:
            return

        if self.enrolled_labels:
            speaker = _dominant_speaker(message.get("results", []))
            if speaker is not None and speaker not in self.enrolled_labels:
                if _DEBUG:
                    self._ui.add_tool_use(f"[DBG] ignored transcript from {speaker!r}")
                return

        if self.state is _State.IDLE:
            if _DEBUG:
                self._ui.add_tool_use(f"[DBG] {transcript!r}")
            self._idle_buffer = (self._idle_buffer + " " + transcript)[-_IDLE_BUFFER_MAX:]
            if _WAKE_WORD_PATTERN.search(_normalize(self._idle_buffer)):
                self._idle_buffer = ""
                self.state = _State.ACCUMULATING
                self._ui.set_status("listening")
            return

        # ACCUMULATING: strip wake word tail if present in the first final.
        match = _WAKE_WORD_PATTERN.search(_normalize(transcript))
        if match:
            transcript = transcript[match.end():]

        transcript = transcript.strip()
        if transcript and re.search(r"[a-zA-Z]", transcript):
            self.buffer.append(transcript)
            self._ui.set_partial(" ".join(self.buffer))

    def handle_end_of_utterance(self, message: dict[str, Any]) -> None:
        """Handle an END_OF_UTTERANCE server message."""
        del message
        if _DEBUG:
            self._ui.add_tool_use("[DBG MSG] END_OF_UTTERANCE")

        if self.state is _State.IDLE:
            self._idle_buffer = ""
            return

        prompt = " ".join(self.buffer).strip()
        self.buffer.clear()
        self._idle_buffer = ""

        if not prompt:
            # Wake word heard but no command yet — stay ACCUMULATING.
            return

        if not re.search(r"[a-zA-Z]{2,}", prompt):
            # Only punctuation/noise — reset silently so user can try again.
            self.state = _State.IDLE
            self._ui.set_status("idle")
            return

        self.state = _State.IDLE
        self.last_prompt = prompt
        self._ui.add_user_message(prompt)
        self._ui.set_status("thinking")
        self.prompt_ready.set()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _require_api_key() -> str:
    api_key = os.environ.get("SPEECHMATICS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "SPEECHMATICS_API_KEY is not set. Export it before running, "
            "e.g. `export SPEECHMATICS_API_KEY=...`."
        )
    return api_key


def _build_transcription_config(
    speakers: list[SpeakerIdentifier] | None = None,
) -> TranscriptionConfig:
    diarization_config = SpeakerDiarizationConfig(speakers=speakers) if speakers else None
    return TranscriptionConfig(
        language="en",
        operating_point=OperatingPoint.ENHANCED,
        diarization="speaker" if speakers else None,
        max_delay=1.0,
        conversation_config=ConversationConfig(
            end_of_utterance_silence_trigger=1.5,
        ),
        speaker_diarization_config=diarization_config,
        additional_vocab=[
            {"content": "CRAB-BOT", "sounds_like": ["crab bot", "grab bot", "crab bought"]},
        ],
    )


# ---------------------------------------------------------------------------
# Audio pumps
# ---------------------------------------------------------------------------

async def _audio_pump(
    client: AsyncClient,
    mic: Microphone,
    controller: VoiceController,
    chunk_size: int,
    stop_event: asyncio.Event,
) -> None:
    """Forward mic frames to the server only while controller.listening is set."""
    while not stop_event.is_set():
        try:
            frame = await mic.read(chunk_size=chunk_size)
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            stop_event.set()
            return

        if controller.listening.is_set():
            try:
                await client.send_audio(frame)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                stop_event.set()
                return


async def _audio_pump_raw(
    client: AsyncClient,
    mic: Microphone,
    chunk_size: int,
    stop_event: asyncio.Event,
) -> None:
    """Unconditional audio pump used during enrollment."""
    while not stop_event.is_set():
        try:
            frame = await mic.read(chunk_size=chunk_size)
            await client.send_audio(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return


# ---------------------------------------------------------------------------
# Speaker enrollment
# ---------------------------------------------------------------------------

async def _enroll_speaker(api_key: str, audio_format: AudioFormat) -> dict[str, list[str]]:
    """Capture 30 s of speech, request speaker identifiers, save to disk."""
    loop = asyncio.get_event_loop()
    name = await loop.run_in_executor(
        None, lambda: input("No enrolled speakers found. Enter your name: ").strip()
    )
    if not name:
        name = "owner"

    print(f"\n[ENROLL] Recording {_ENROLLMENT_SECONDS}s as '{name}'. Please speak naturally...\n")

    enrollment_config = TranscriptionConfig(
        language="en",
        operating_point=OperatingPoint.ENHANCED,
        diarization="speaker",
        max_delay=1.0,
        additional_vocab=[
            {"content": "CRAB-BOT", "sounds_like": ["crab bot", "grab bot", "crab bought"]},
        ],
    )

    speakers_future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()

    mic = Microphone(
        sample_rate=audio_format.sample_rate,
        chunk_size=audio_format.chunk_size,
    )
    if not mic.start():
        raise RuntimeError("Microphone not available for enrollment")

    try:
        async with AsyncClient(api_key=api_key, **({'url': _RT_URL} if _RT_URL else {})) as client:

            @client.on(ServerMessageType.RECOGNITION_STARTED)
            def _on_started(msg: dict[str, Any]) -> None:
                print("[ENROLL] Connected — recording now...")

            @client.on(ServerMessageType.SPEAKERS_RESULT)
            def _on_speakers(msg: dict[str, Any]) -> None:
                if not speakers_future.done():
                    speakers_future.set_result(msg.get("speakers", []))

            await client.start_session(
                transcription_config=enrollment_config,
                audio_format=audio_format,
            )

            stop_pump = asyncio.Event()
            pump_task = asyncio.create_task(
                _audio_pump_raw(client, mic, audio_format.chunk_size, stop_pump)
            )

            await asyncio.sleep(_ENROLLMENT_SECONDS)
            stop_pump.set()
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass

            print("[ENROLL] Recording complete. Requesting speaker identifiers...")
            await client.send_message({"message": ClientMessageType.GET_SPEAKERS})

            try:
                raw_speakers = await asyncio.wait_for(speakers_future, timeout=10.0)
            except asyncio.TimeoutError:
                raw_speakers = []

    finally:
        mic.stop()

    if not raw_speakers:
        raise RuntimeError("Enrollment failed: no speakers detected in the recording")

    best = max(raw_speakers, key=lambda s: len(s.get("speaker_identifiers", [])))
    enrolled = {name: best["speaker_identifiers"]}
    _save_speakers(enrolled)
    print(f"[ENROLL] '{name}' enrolled ({len(best['speaker_identifiers'])} identifier(s) saved).\n")
    return enrolled


# ---------------------------------------------------------------------------
# Claude output rendering + driver
# ---------------------------------------------------------------------------

def _handle_stream_event(event: dict[str, Any], ui: UI) -> None:
    """Dispatch a claude --output-format stream-json event to the UI."""
    if event.get("type") == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text":
                ui.add_assistant_text(block["text"])
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                if name in ("Edit", "Write"):
                    path = inp.get("file_path", "?")
                    ui.add_tool_use(f"[EDIT] {os.path.relpath(path)}")
                elif name == "Bash":
                    ui.add_tool_use(f"[BASH] {inp.get('command', '?')[:100]}")
                elif name:
                    ui.add_tool_use(f"[TOOL] {name}")
    elif event.get("type") == "result" and event.get("subtype") == "error":
        ui.add_error_message(f"[CLAUDE ERROR] {event.get('error', '')}")


async def claude_driver(
    controller: VoiceController,
    ui: UI,
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    api_key = _require_api_key()

    audio_format = AudioFormat(
        encoding=AudioEncoding.PCM_S16LE,
        sample_rate=16000,
        chunk_size=4096,
    )

    speakers = _load_speakers()
    if not speakers:
        speakers = await _enroll_speaker(api_key, audio_format)

    speaker_identifiers = [
        SpeakerIdentifier(label=name, speaker_identifiers=ids)
        for name, ids in speakers.items()
    ]
    enrolled_labels = set(speakers.keys())
    transcription_config = _build_transcription_config(speakers=speaker_identifiers)

    ui = UI()
    controller = VoiceController(enrolled_labels=enrolled_labels, ui=ui)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    mic = Microphone(
        sample_rate=audio_format.sample_rate,
        chunk_size=audio_format.chunk_size,
    )
    if not mic.start():
        print("PyAudio not available - install with `pip install pyaudio`.")
        return

    with Live(ui._layout, screen=True, auto_refresh=False) as live:
        ui.start(live)
        if _DEBUG:
            ui.add_tool_use("[READY] Debug mode ON.")

        try:
            async with AsyncClient(api_key=api_key, **({'url': _RT_URL} if _RT_URL else {})) as client:

                @client.on(ServerMessageType.RECOGNITION_STARTED)
                def _on_started(message: dict[str, Any]) -> None:
                    if _DEBUG:
                        ui.add_tool_use("[DBG MSG] RECOGNITION_STARTED")
                    ui.set_status("idle")

                @client.on(ServerMessageType.ADD_TRANSCRIPT)
                def _on_final(message: dict[str, Any]) -> None:
                    controller.handle_final(message)

                @client.on(ServerMessageType.END_OF_UTTERANCE)
                def _on_eou(message: dict[str, Any]) -> None:
                    controller.handle_end_of_utterance(message)

                @client.on(ServerMessageType.ERROR)
                def _on_server_error(message: dict[str, Any]) -> None:
                    if _DEBUG:
                        ui.add_tool_use("[DBG MSG] ERROR")
                    reason = message.get("reason", "unknown")
                    ui.add_error_message(f"[ERROR] {reason}")
                    stop_event.set()

                await client.start_session(
                    transcription_config=transcription_config,
                    audio_format=audio_format,
                )

                pump_task = asyncio.create_task(
                    _audio_pump(
                        client=client,
                        mic=mic,
                        controller=controller,
                        chunk_size=audio_format.chunk_size,
                        stop_event=stop_event,
                    ),
                    name="audio-pump",
                )
                driver_task = asyncio.create_task(
                    claude_driver(
                        controller=controller,
                        ui=ui,
                        stop_event=stop_event,
                    ),
                    name="claude-driver",
                )
                stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")

                try:
                    await asyncio.wait(
                        {pump_task, driver_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    pass
                finally:
                    stop_event.set()
                    ui.stop()
                    pump_task.cancel()
                    driver_task.cancel()
                    stop_task.cancel()
                    for task in (pump_task, driver_task, stop_task):
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    try:
                        await asyncio.wait_for(client.stop_session(), timeout=3.0)
                    except Exception:
                        pass

        except AuthenticationError as exc:
            ui.add_error_message(f"[ERROR] Authentication failed: {exc}")
            await asyncio.sleep(2)
        finally:
            mic.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[BYE] Exiting.")
