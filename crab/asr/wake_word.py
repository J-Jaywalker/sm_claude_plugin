"""Local wake word detection via OpenWakeWord (no API key required)."""

from __future__ import annotations

import asyncio

# Built-in model names shipped with openwakeword (onnx variants)
BUILTIN_MODELS = [
    "hey_jarvis_v0.1",
    "alexa_v0.1",
    "hey_mycroft_v0.1",
    "hey_rhasspy_v0.1",
]
DEFAULT_MODEL = "hey_jarvis_v0.1"
_CHUNK = 1280   # 80 ms at 16 kHz — recommended frame size for openwakeword
_RATE = 16000


class OpenWakeWordDetector:
    """Runs OpenWakeWord in a loop until the wake word fires or stop is signalled.

    *model* can be a built-in model name (e.g. ``"hey_jarvis_v0.1"``) or a
    filesystem path to a custom ``.onnx`` file.  Defaults to ``hey_jarvis_v0.1``
    until a custom CRAB-BOT model is trained.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        threshold: float = 0.5,
    ) -> None:
        self._model = model or DEFAULT_MODEL
        self._threshold = threshold

    async def wait_for_wake(
        self,
        device_index: int | None,
        stop_event: asyncio.Event,
    ) -> bool:
        """Block until the wake word is detected or *stop_event* is set.

        Returns True if the wake word fired, False if stopped externally.
        """
        try:
            from openwakeword.model import Model
            import numpy as np
            import pyaudio
        except ImportError as exc:
            raise RuntimeError(f"openwakeword/pyaudio not installed: {exc}") from exc

        oww = Model(wakeword_models=[self._model], inference_framework="onnx")

        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=_RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=_CHUNK,
        )

        loop = asyncio.get_event_loop()
        detected = False

        try:
            while not stop_event.is_set():
                pcm_bytes = await loop.run_in_executor(
                    None,
                    lambda: stream.read(_CHUNK, exception_on_overflow=False),
                )
                if stop_event.is_set():
                    break
                audio = np.frombuffer(pcm_bytes, dtype=np.int16)
                predictions = oww.predict(audio)
                if any(v >= self._threshold for v in predictions.values()):
                    detected = True
                    break
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()

        return detected
