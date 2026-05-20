"""Utilities for querying available audio input devices."""

from __future__ import annotations


def list_input_devices() -> list[tuple[int, str]]:
    """Return (index, name) pairs for all available audio input devices.

    Returns an empty list if PyAudio is not installed or device query fails.
    """
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        devices: list[tuple[int, str]] = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                devices.append((i, str(info["name"])))
        pa.terminate()
        return devices
    except Exception:
        return []
