"""Voice controller: wake word + accumulate + end-of-utterance + Claude integration.

Streams microphone audio to Speechmatics RT ASR. On first run, enrolls the
speaker by capturing 30 seconds of audio and saving identifiers to speakers.txt.
On subsequent runs, loads enrolled speakers and ignores transcripts from
unrecognised voices. Detects wake phrase "Alright Claude" in finals, accumulates
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
import sys
from enum import Enum
from typing import Any

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

from rich.console import Console
from rich.markdown import Markdown

_console = Console()

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


_WAKE_WORD_PATTERN = re.compile(r"\ball\s*right\s+claude\b|\balright\s+claude\b")
_DEBUG = bool(os.environ.get("DEBUG"))
_IDLE_BUFFER_MAX = 120

_SPEAKERS_FILE = "speakers.txt"
_ENROLLMENT_SECONDS = 30

_RT_URL = "ws://127.0.0.1:9002/v2" if os.environ.get("SM_LOCAL_CLAUDE_TRANSCRIPTION") else None


# ---------------------------------------------------------------------------
# Status dot
# ---------------------------------------------------------------------------

_DOT_FRAMES = ("●", "◉", "◎", "◉")
_DOT_INTERVAL = 0.2

_DOT_STATES: dict[str, tuple[str, str]] = {
    #              ANSI colour   label
    "idle":      ("\033[91m", "Waiting for wake word..."),   # bright red
    "listening": ("\033[92m", "Claude is listening..."),     # bright green
    "thinking":  ("\033[93m", "Claude is thinking..."),      # bright yellow
}
_RESET = "\033[0m"


class StatusDot:
    """Animated single-line status indicator.

    Pulses a coloured dot on the current terminal line using \\r so it is
    overwritten cleanly when transcript text or Claude output arrives.
    Colour and label change with the voice-controller state.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._state = "idle"
        self._frame = 0

    async def _run(self) -> None:
        while True:
            color, label = _DOT_STATES[self._state]
            dot = _DOT_FRAMES[self._frame % len(_DOT_FRAMES)]
            sys.stdout.write(f"\r{color}{dot} {label}{_RESET}  ")
            sys.stdout.flush()
            self._frame += 1
            await asyncio.sleep(_DOT_INTERVAL)

    def clear(self) -> None:
        """Erase the status line so other output can print cleanly."""
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def start(self, state: str) -> None:
        """Start (or switch to) a new state, restarting the animation."""
        self.stop()
        self._state = state
        self._frame = 0
        try:
            self._task = asyncio.create_task(self._run(), name="status-dot")
        except RuntimeError:
            pass  # event loop not running (e.g. during shutdown)

    def stop(self) -> None:
        """Cancel the animation and erase the status line."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.clear()


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

    def __init__(self, enrolled_labels: set[str], dot: StatusDot) -> None:
        self._dot = dot
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
            # Only filter when the server returned a label — if None the
            # endpoint doesn't support diarization; let the transcript through.
            if speaker is not None and speaker not in self.enrolled_labels:
                if _DEBUG:
                    print(f"[DBG] ignored transcript from speaker {speaker!r}")
                return

        if self.state is _State.IDLE:
            if _DEBUG:
                self._dot.clear()
                print(f"[DBG] {transcript!r}")
                self._dot.start("idle")
            self._idle_buffer = (self._idle_buffer + " " + transcript)[-_IDLE_BUFFER_MAX:]
            if _WAKE_WORD_PATTERN.search(_normalize(self._idle_buffer)):
                self._idle_buffer = ""
                self.state = _State.ACCUMULATING
                self._dot.start("listening")
            return

        # ACCUMULATING: strip wake word tail if present in the first final.
        match = _WAKE_WORD_PATTERN.search(_normalize(transcript))
        if match:
            transcript = transcript[match.end():]

        transcript = transcript.strip()
        if transcript and re.search(r"[a-zA-Z]", transcript):
            if not self.buffer:
                # First real word — clear the listening dot so transcript owns the line.
                self._dot.stop()
            self.buffer.append(transcript)
            print(f"\r  {' '.join(self.buffer)}", end="", flush=True)

    def handle_end_of_utterance(self, message: dict[str, Any]) -> None:
        """Handle an END_OF_UTTERANCE server message."""
        del message
        if _DEBUG:
            print("[DBG MSG] END_OF_UTTERANCE")

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
            self._dot.start("idle")
            return

        self.state = _State.IDLE
        self.last_prompt = prompt
        print(f"\n[SENDING] {prompt}", flush=True)
        self._dot.start("thinking")
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
            {"content": "Claude", "sounds_like": ["clawed", "cloud"]},
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
        except RuntimeError as exc:
            print(f"[ERROR] Microphone read failed: {exc}")
            stop_event.set()
            return

        if controller.listening.is_set():
            try:
                await client.send_audio(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] send_audio failed: {exc}")
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
            {"content": "Claude", "sounds_like": ["clawed", "cloud"]},
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

def _print_stream_event(event: dict[str, Any]) -> None:
    """Print human-readable output from a claude --output-format stream-json event."""
    if event.get("type") == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text":
                text = _ANSI_ESCAPE.sub("", block["text"])
                _console.print(Markdown(text, code_theme="ansi_dark"))
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                if name in ("Edit", "Write"):
                    path = inp.get("file_path", "?")
                    print(f"[EDIT] {os.path.relpath(path)}", flush=True)
                elif name == "Bash":
                    print(f"[BASH] {inp.get('command', '?')[:100]}", flush=True)
                elif name:
                    print(f"[TOOL] {name}", flush=True)
    elif event.get("type") == "result" and event.get("subtype") == "error":
        print(f"[CLAUDE ERROR] {event.get('error', '')}", flush=True)


async def claude_driver(
    controller: VoiceController,
    dot: StatusDot,
    stop_event: asyncio.Event,
) -> None:
    """Run `claude -p` for each assembled prompt, streaming output.

    Clears controller.listening while Claude runs so the audio pump drops
    frames, then restores it when done.
    """
    child_env = {k: v for k, v in os.environ.items() if k != "DEBUG"}
    first_prompt = True

    while not stop_event.is_set():
        await controller.prompt_ready.wait()
        controller.prompt_ready.clear()
        prompt_text = controller.last_prompt
        if not prompt_text:
            continue

        controller.listening.clear()
        dot.stop()
        print(f"[CLAUDE] {prompt_text}\n")

        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--verbose",
            "--output-format", "stream-json",
        ]
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
                    print(_ANSI_ESCAPE.sub("", line), flush=True)
                    continue
                _print_stream_event(event)
            await proc.wait()
        except asyncio.CancelledError:
            if proc is not None:
                proc.terminate()
            raise
        except Exception as exc:
            print(f"[CLAUDE] Error: {exc}")

        print()
        dot.start("idle")
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
    print(f"[READY] Enrolled speakers: {', '.join(enrolled_labels)}")

    transcription_config = _build_transcription_config(speakers=speaker_identifiers)
    dot = StatusDot()
    controller = VoiceController(enrolled_labels=enrolled_labels, dot=dot)
    stop_event = asyncio.Event()

    mic = Microphone(
        sample_rate=audio_format.sample_rate,
        chunk_size=audio_format.chunk_size,
    )
    if not mic.start():
        print("PyAudio not available - install with `pip install pyaudio`.")
        return

    try:
        async with AsyncClient(api_key=api_key, **({'url': _RT_URL} if _RT_URL else {})) as client:

            @client.on(ServerMessageType.RECOGNITION_STARTED)
            def _on_started(message: dict[str, Any]) -> None:
                if _DEBUG:
                    print("[DBG MSG] RECOGNITION_STARTED")
                print("[READY] Connected. Say 'Alright Claude' to begin.")
                dot.start("idle")

            @client.on(ServerMessageType.ADD_TRANSCRIPT)
            def _on_final(message: dict[str, Any]) -> None:
                controller.handle_final(message)

            @client.on(ServerMessageType.END_OF_UTTERANCE)
            def _on_eou(message: dict[str, Any]) -> None:
                controller.handle_end_of_utterance(message)

            @client.on(ServerMessageType.ERROR)
            def _on_server_error(message: dict[str, Any]) -> None:
                if _DEBUG:
                    print("[DBG MSG] ERROR")
                dot.stop()
                reason = message.get("reason", "unknown")
                print(f"[ERROR] Server error: {reason}")
                stop_event.set()

            if _DEBUG:
                print("[READY] Debug mode ON.")
            print("[READY] Connecting... (press Ctrl+C to exit)")
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
                    dot=dot,
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
                dot.stop()
                pump_task.cancel()
                driver_task.cancel()
                stop_task.cancel()
                for task in (pump_task, driver_task, stop_task):
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

    except AuthenticationError as exc:
        print(f"[ERROR] Authentication failed: {exc}")
    finally:
        mic.stop()


if __name__ == "__main__":
    def _handle_sigint(sig: int, frame: object) -> None:
        signal.signal(signal.SIGINT, lambda *_: os._exit(1))

    signal.signal(signal.SIGINT, _handle_sigint)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[BYE] Exiting.")
