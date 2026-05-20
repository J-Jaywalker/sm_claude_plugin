"""Speaker enrollment flows for the CLI bootstrap and the TUI modal."""

from __future__ import annotations

import asyncio
from typing import Any

from speechmatics.rt import (
    AsyncClient,
    AudioFormat,
    ClientMessageType,
    Microphone,
    OperatingPoint,
    ServerMessageType,
    TranscriptionConfig,
)

from crab.asr.pumps import _audio_pump_raw
from crab.config import _ENROLLMENT_SECONDS, _RT_URL
from crab.speaker_store import _save_speakers


async def _enroll_speaker_tui(
    api_key: str,
    audio_format: AudioFormat,
    name: str,
    on_status: Any,
    rt_url: str | None = None,
) -> dict[str, list[str]]:
    """Run a 30-second enrollment session, updating *on_status* with progress.

    *on_status* is a callable(str) that accepts a status string (plain text).
    Returns the enrolled speakers dict and saves it to disk.
    """
    enrollment_config = TranscriptionConfig(
        language="en",
        operating_point=OperatingPoint.ENHANCED,
        diarization="speaker",
        max_delay=1.0,
        additional_vocab=[
            {"content": "CRAB-BOT", "sounds_like": ["crab bot", "grab bot", "crab bought"]},
        ],
    )

    loop = asyncio.get_event_loop()
    speakers_future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()

    mic = Microphone(sample_rate=audio_format.sample_rate, chunk_size=audio_format.chunk_size)
    if not mic.start():
        raise RuntimeError("Microphone not available")

    try:
        async with AsyncClient(api_key=api_key, url=rt_url) as client:

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

            for remaining in range(_ENROLLMENT_SECONDS, 0, -1):
                on_status(f"Recording as '{name}'... {remaining}s remaining — speak naturally")
                await asyncio.sleep(1)

            stop_pump.set()
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass

            on_status("Processing speaker identifiers...")
            await client.send_message({"message": ClientMessageType.GET_SPEAKERS})

            try:
                raw_speakers = await asyncio.wait_for(speakers_future, timeout=10.0)
            except asyncio.TimeoutError:
                raw_speakers = []
    finally:
        mic.stop()

    if not raw_speakers:
        raise RuntimeError("Enrollment failed: no speaker identifiers detected")

    best = max(raw_speakers, key=lambda s: len(s.get("speaker_identifiers", [])))
    enrolled = {name: best["speaker_identifiers"]}
    _save_speakers(enrolled)
    return enrolled


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
        async with AsyncClient(api_key=api_key, url=_RT_URL) as client:

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
