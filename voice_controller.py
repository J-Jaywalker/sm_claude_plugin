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
from enum import Enum
from typing import Any

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

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


# Matches "alright claude" or "all right claude" on normalised text.
# Both spellings appear in ASR output; \s* handles the word-boundary artefact.
_WAKE_WORD_PATTERN = re.compile(r"\ball\s*right\s+claude\b|\balright\s+claude\b")
_DEBUG = bool(os.environ.get("DEBUG"))
# Rolling idle buffer is capped at this many characters — enough to catch a
# wake word split across two consecutive finals without growing unbounded.
_IDLE_BUFFER_MAX = 120

_SPEAKERS_FILE = "speakers.txt"
_ENROLLMENT_SECONDS = 30


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
    """Coordinates microphone capture, ASR events, and prompt assembly.

    Attributes:
        enrolled_labels: Set of speaker labels that are allowed to trigger
            commands. Empty set means no filtering (no diarization configured).
        state: Current controller state.
        buffer: Final transcript fragments collected during ACCUMULATING.
        listening: When set, the audio pump forwards frames to the server.
            When clear, frames are dropped while a prompt is being handled.
        prompt_ready: Signalled once per completed utterance.
        last_prompt: The most recent assembled prompt text.
    """

    def __init__(self, enrolled_labels: set[str]) -> None:
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
            # Reject speech from unrecognised voices so background speakers
            # can't accidentally trigger commands.
            if speaker not in self.enrolled_labels:
                if _DEBUG:
                    print(f"[DBG] ignored transcript from speaker {speaker!r}")
                return

        if self.state is _State.IDLE:
            if _DEBUG:
                print(f"[DBG] {transcript!r}")
            # Append to a rolling window rather than the full history so the
            # wake-word regex never has to scan an ever-growing string.
            self._idle_buffer = (self._idle_buffer + " " + transcript)[-_IDLE_BUFFER_MAX:]
            if _WAKE_WORD_PATTERN.search(_normalize(self._idle_buffer)):
                self._idle_buffer = ""
                self.state = _State.ACCUMULATING
                print("\nClaude is listening...")
            return

        # ACCUMULATING: strip wake word if it arrived in the first final
        # (the user spoke the wake phrase and command in one breath).
        match = _WAKE_WORD_PATTERN.search(_normalize(transcript))
        if match:
            transcript = transcript[match.end():]

        transcript = transcript.strip()
        if transcript:
            self.buffer.append(transcript)
            print(f"\r  {' '.join(self.buffer)}", end="", flush=True)

    def handle_end_of_utterance(self, message: dict[str, Any]) -> None:
        """Handle an END_OF_UTTERANCE server message."""
        del message  # unused; present only to satisfy the event-handler signature
        if _DEBUG:
            print("[DBG MSG] END_OF_UTTERANCE")

        if self.state is _State.IDLE:
            # Clear idle buffer on silence so stale text can't re-trigger
            # the wake word after a long pause.
            self._idle_buffer = ""
            return

        prompt = " ".join(self.buffer).strip()
        self.buffer.clear()
        self._idle_buffer = ""

        if not prompt:
            # Wake word heard but no command in this utterance yet —
            # stay ACCUMULATING and wait for the user to continue.
            return

        self.state = _State.IDLE
        self.last_prompt = prompt
        print(f"\n[SENDING] {prompt}", flush=True)
        self.prompt_ready.set()
        print("Claude is thinking...\n")


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
        # max_delay=1.0 keeps finals arriving quickly so the wake word is
        # detected within ~1 s of being spoken rather than batched.
        max_delay=1.0,
        conversation_config=ConversationConfig(
            # 1.5 s of silence reliably ends a command without cutting off
            # natural mid-sentence pauses (~0.5–0.8 s in normal speech).
            end_of_utterance_silence_trigger=1.5,
        ),
        speaker_diarization_config=diarization_config,
        additional_vocab=[
            # "Claude" is easily mis-heard; these phonetic hints improve recall.
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
    """Read mic frames continuously, forwarding them only while listening."""
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
        # When listening is clear (Claude is processing), frames are read and
        # discarded so the mic buffer doesn't overflow, but nothing is sent
        # to ASR — this prevents stale audio replaying after Claude responds.


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
        async with AsyncClient(api_key=api_key) as client:

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

    # The user spoke for the full enrollment window, so their cluster will have
    # accumulated the most identifiers; any background noise clusters won't.
    best = max(raw_speakers, key=lambda s: len(s.get("speaker_identifiers", [])))
    enrolled = {name: best["speaker_identifiers"]}
    _save_speakers(enrolled)
    print(f"[ENROLL] '{name}' enrolled ({len(best['speaker_identifiers'])} identifier(s) saved).\n")
    return enrolled


# ---------------------------------------------------------------------------
# Claude output rendering
# ---------------------------------------------------------------------------

def _print_stream_event(event: dict[str, Any]) -> None:
    """Print human-readable output from a claude --output-format stream-json event."""
    if event.get("type") == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text":
                print(_ANSI_ESCAPE.sub("", block["text"]), end="", flush=True)
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


# ---------------------------------------------------------------------------
# Claude driver
# ---------------------------------------------------------------------------

async def claude_driver(
    controller: VoiceController,
    stop_event: asyncio.Event,
) -> None:
    """Run claude -p for each assembled prompt and stream the response.

    Uses claude's non-interactive print mode to avoid fighting the TUI.
    ASR is paused while Claude is running and resumed when it exits.

    Args:
        controller: Shared voice controller carrying prompts and the
            listening gate.
        stop_event: Cooperative shutdown event.
    """
    # Strip DEBUG so Claude Code's own verbose logging isn't polluted by ours.
    child_env = {k: v for k, v in os.environ.items() if k != "DEBUG"}
    first_prompt = True

    while not stop_event.is_set():
        await controller.prompt_ready.wait()
        controller.prompt_ready.clear()
        prompt_text = controller.last_prompt
        if not prompt_text:
            continue

        controller.listening.clear()
        print(f"\n[CLAUDE] {prompt_text}\n")

        base_cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--verbose",
            "--output-format", "stream-json",
        ]
        # --continue resumes the same Claude session so follow-up commands
        # share context with previous ones; omitted on the very first call
        # because there is no prior session to continue.
        if not first_prompt:
            base_cmd.append("--continue")
        base_cmd += ["-p", prompt_text]
        first_prompt = False

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *base_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=child_env,
                cwd=os.getcwd(),
            )
            assert proc.stdout is not None  # guaranteed by PIPE above; satisfies type checker
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

        print("\n")
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

    # Enrollment: run once if no speakers file exists.
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
    controller = VoiceController(enrolled_labels=enrolled_labels)
    stop_event = asyncio.Event()

    mic = Microphone(
        sample_rate=audio_format.sample_rate,
        chunk_size=audio_format.chunk_size,
    )
    if not mic.start():
        print("PyAudio not available - install with `pip install pyaudio`.")
        return

    try:
        async with AsyncClient(api_key=api_key) as client:

            @client.on(ServerMessageType.RECOGNITION_STARTED)
            def _on_started(message: dict[str, Any]) -> None:
                if _DEBUG:
                    print("[DBG MSG] RECOGNITION_STARTED")
                print("[READY] Connected. Say 'Alright Claude' to begin.\n")

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
                    stop_event=stop_event,
                ),
                name="claude-driver",
            )
            stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")

            try:
                # Any one task finishing (pump error, driver error, or
                # stop_event set) should tear down the whole session.
                await asyncio.wait(
                    {pump_task, driver_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                pass
            finally:
                stop_event.set()
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
        # Second Ctrl+C force-exits if asyncio teardown is hanging.
        signal.signal(signal.SIGINT, lambda *_: os._exit(1))

    signal.signal(signal.SIGINT, _handle_sigint)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[BYE] Exiting.")
