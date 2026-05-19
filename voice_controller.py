"""Voice controller STT phase: wake word + accumulate + end-of-utterance.

This is the speech-to-text pipeline for a voice-controlled Claude Code
wrapper. It streams microphone audio continuously to Speechmatics RT ASR,
listens for the wake phrase "Alright Claude" in final transcripts, then
accumulates finals until the server reports EndOfUtterance.
The accumulated text is printed as the prompt that would be handed to
Claude Code in a later phase. No Claude integration yet.

Usage:
    SPEECHMATICS_API_KEY=... python voice_controller.py
    DEBUG=1 SPEECHMATICS_API_KEY=... python voice_controller.py
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
from enum import Enum
from typing import Any

from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    AuthenticationError,
    ConversationConfig,
    Microphone,
    OperatingPoint,
    ServerMessageType,
    TranscriptionConfig,
)


# Matches "alright claude" or "all right claude" on normalised text.
_WAKE_WORD_PATTERN = re.compile(r"\ball\s*right\s+claude\b|\balright\s+claude\b")
_DEBUG = bool(os.environ.get("DEBUG"))
# Rolling idle buffer is capped at this many characters — enough to catch a
# wake word split across two consecutive finals without growing unbounded.
_IDLE_BUFFER_MAX = 120


def _normalize(text: str) -> str:
    """Lowercase and collapse punctuation/whitespace for wake word matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class _State(str, Enum):
    IDLE = "IDLE"
    ACCUMULATING = "ACCUMULATING"


class VoiceController:
    """Coordinates microphone capture, ASR events, and prompt assembly.

    Attributes:
        state: Current controller state.
        buffer: Final transcript fragments collected during ACCUMULATING.
        listening: When set, the audio pump forwards frames to the server.
            When clear, frames are dropped (e.g. while a prompt is being
            handled in a future Claude-integration phase).
        prompt_ready: Signalled once per completed utterance.
        last_prompt: The most recent assembled prompt text.
    """

    def __init__(self) -> None:
        self.state: _State = _State.IDLE
        self.buffer: list[str] = []
        self._idle_buffer: str = ""  # rolling window of recent idle text
        self.listening: asyncio.Event = asyncio.Event()
        self.listening.set()
        self.prompt_ready: asyncio.Event = asyncio.Event()
        self.last_prompt: str = ""

    def handle_final(self, message: dict[str, Any]) -> None:
        """Handle an ADD_TRANSCRIPT (final) server message."""
        transcript = message.get("metadata", {}).get("transcript", "")
        if not transcript:
            return

        if self.state is _State.IDLE:
            if _DEBUG:
                print(f"[DBG] {transcript!r}")
            self._idle_buffer = (self._idle_buffer + " " + transcript)[-_IDLE_BUFFER_MAX:]
            if _WAKE_WORD_PATTERN.search(_normalize(self._idle_buffer)):
                self._idle_buffer = ""
                self.state = _State.ACCUMULATING
                print("\nClaude is listening...")
            return

        # ACCUMULATING: strip wake word if it arrived in the first final
        match = _WAKE_WORD_PATTERN.search(_normalize(transcript))
        if match:
            transcript = transcript[match.end():]

        transcript = transcript.strip()
        if transcript:
            self.buffer.append(transcript)
            print(f"[TRANSCRIPT] {transcript}")

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
        self.state = _State.IDLE

        if not prompt:
            self.state = _State.IDLE
            return

        self.last_prompt = prompt
        print(f"\n[SENDING] {prompt}")
        self.prompt_ready.set()
        print("Claude is thinking...\n")


def _require_api_key() -> str:
    api_key = os.environ.get("SPEECHMATICS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "SPEECHMATICS_API_KEY is not set. Export it before running, "
            "e.g. `export SPEECHMATICS_API_KEY=...`."
        )
    return api_key


def _build_transcription_config() -> TranscriptionConfig:
    return TranscriptionConfig(
        language="en",
        operating_point=OperatingPoint.ENHANCED,
        max_delay=1.0,
        conversation_config=ConversationConfig(
            end_of_utterance_silence_trigger=0.5,
        ),
        additional_vocab=[
            {"content": "Claude", "sounds_like": ["clawed", "cloud"]},
        ],
    )


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


async def main() -> None:
    api_key = _require_api_key()

    audio_format = AudioFormat(
        encoding=AudioEncoding.PCM_S16LE,
        sample_rate=16000,
        chunk_size=4096,
    )
    transcription_config = _build_transcription_config()

    controller = VoiceController()
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
            stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")

            try:
                await asyncio.wait(
                    {pump_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                pass
            finally:
                stop_event.set()
                pump_task.cancel()
                stop_task.cancel()
                for task in (pump_task, stop_task):
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
