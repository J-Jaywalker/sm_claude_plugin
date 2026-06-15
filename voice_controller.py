"""Voice controller: wake word + accumulate + end-of-utterance + Claude integration.

Streams microphone audio to Speechmatics RT ASR. On first run, enrolls the
speaker by capturing 30 seconds of audio and saving identifiers to speakers.txt.
On subsequent runs, loads enrolled speakers and ignores transcripts from
unrecognised voices. Detects wake phrase "CRAB-BOT" in finals, accumulates
until EndOfUtterance, then submits the prompt to Claude Code.

Two backends:
  - default: long-running interactive Claude driven via a custom MCP channel
    (crab.channel.server) — preserves permission gating and a single session
  - --legacy: per-turn `claude -p` subprocess (the original PoC path)

Usage:
    SPEECHMATICS_API_KEY=... python voice_controller.py
    SPEECHMATICS_API_KEY=... python voice_controller.py --legacy
    DEBUG=1 SPEECHMATICS_API_KEY=... python voice_controller.py
"""

from __future__ import annotations

import argparse
import asyncio
import os

from speechmatics.rt import (
    AudioEncoding,
    AudioFormat,
    ConversationConfig,
    OperatingPoint,
    SpeakerDiarizationConfig,
    SpeakerIdentifier,
    TranscriptionConfig,
)

from crab.asr.enrollment import _enroll_speaker
from crab.speaker_store import _load_speakers
from crab.ui.app import CrabApp


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
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Speechmatics voice controller for Claude Code.",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help=(
            "Fall back to the original `claude -p` subprocess driver. Default is "
            "the channels MCP path (long-running interactive Claude under a hidden "
            "PTY, bridged via a custom MCP server). Permission relay for the "
            "channels path lands in Phase 2 — until then non-Read tool calls are "
            "auto-denied with a visible error."
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    args = _parse_args()
    use_channels = not args.legacy

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
    speaker_name = next(iter(enrolled_labels), "You")

    app = CrabApp(
        api_key=api_key,
        audio_format=audio_format,
        transcription_config=transcription_config,
        speaker_name=speaker_name,
        enrolled_labels=enrolled_labels,
        use_channels=use_channels,
    )
    await app.run_async()

    # Textual has exited but the asyncio event loop (asyncio.run) is still live.
    # Wait here for the speechmatics task to finish its own cleanup (stop_session,
    # mic.stop) before we let asyncio.run() return and Python starts joining threads.
    await app.await_asr_shutdown(timeout=4.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    print("\n[BYE] Exiting.")
    # os._exit skips Python's atexit thread-join phase (concurrent.futures executor
    # threads from PyAudio / Textual internals) which hangs on a second Ctrl+C.
    os._exit(0)
