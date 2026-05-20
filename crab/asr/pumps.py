"""Audio pump coroutines for live ASR and enrollment sessions."""

from __future__ import annotations

import asyncio

from speechmatics.rt import AsyncClient, Microphone

from crab.asr.controller import VoiceController


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
