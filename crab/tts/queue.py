"""Single-consumer TTS playback queue.

Concurrent ``speak()`` calls used to interrupt each other when each one
killed the previous ``say`` subprocess. The queue enforces FIFO order so
narrate + ``<tts>`` block + menu question all play sequentially.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from crab.tts import _TTS_PROVIDER_MACOS

_LOGGER = logging.getLogger(__name__)

_QueueItem = Optional[tuple[str, Optional["asyncio.Future[None]"]]]


class TtsQueue:
    """Owns the playback queue, the worker task, and the in-flight subprocess.

    Usage:
        q = TtsQueue()
        q.start()                       # in on_mount
        q.speak("first")                # fire-and-forget
        await q.speak_and_wait("then")  # blocks until ahead-of-queue + own clip done
        q.shutdown()                    # in on_unmount

    Settings UI mutates ``enabled`` / ``provider`` directly.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        provider: str = _TTS_PROVIDER_MACOS,
    ) -> None:
        self.enabled: bool = enabled
        self.provider: str = provider
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start the worker task. Idempotent."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="tts-worker")

    def shutdown(self) -> None:
        """Signal worker exit + kill any in-flight clip. Safe to call from on_unmount."""
        try:
            self._queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # -- public API ---------------------------------------------------------

    def speak(self, text: str) -> None:
        """Fire-and-forget queue put. Never interrupts what's playing."""
        if not text.strip():
            return
        self.enqueue(text, wait=False)

    async def speak_and_wait(self, text: str) -> None:
        """Queue text and block until this clip (after anything ahead) finishes."""
        if not text.strip():
            return
        fut = self.enqueue(text, wait=True)
        if fut is not None:
            try:
                await fut
            except asyncio.CancelledError:
                raise

    def enqueue(
        self,
        text: str,
        *,
        wait: bool,
    ) -> asyncio.Future[None] | None:
        """Append a clip to the playback queue.

        Returns a Future that resolves when *this clip* finishes (only when
        ``wait=True``). Callers passing ``wait=False`` get ``None`` and rely
        on the queue's FIFO ordering for correctness.
        """
        fut: asyncio.Future[None] | None = None
        if wait:
            fut = asyncio.get_event_loop().create_future()
        try:
            self._queue.put_nowait((text, fut))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("TTS enqueue failed: %s", exc)
            if fut is not None and not fut.done():
                fut.set_result(None)
        return fut

    # -- worker -------------------------------------------------------------

    async def _worker(self) -> None:
        """Single consumer that plays queued clips one at a time."""
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                return
            if item is None:
                return
            text, fut = item
            try:
                if self.enabled:
                    await self._speak_one(text)
            except asyncio.CancelledError:
                if fut is not None and not fut.done():
                    fut.set_result(None)
                return
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("TTS error: %s", exc)
            finally:
                if fut is not None and not fut.done():
                    fut.set_result(None)

    async def _speak_one(self, text: str) -> None:
        """Play a single TTS clip. Currently only the macOS ``say`` provider
        is implemented; the Python-side provider is a placeholder (settings
        UI exposes it but it's a no-op)."""
        if self.provider == _TTS_PROVIDER_MACOS:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    "say", "-v", "Daniel (Enhanced)", text,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await self._proc.wait()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("TTS failed: %s", exc)
            finally:
                self._proc = None
