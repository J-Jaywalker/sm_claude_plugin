"""TTS helpers — provider identifiers and the ``<tts>`` block extractor."""

from __future__ import annotations

from crab.config import _TTS_TAG_RE

# Provider id constants (used by settings UI + the queue worker).
_TTS_PROVIDER_MACOS = "macos"
_TTS_PROVIDER_PYTHON = "python"


def _extract_tts(text: str) -> tuple[str, str]:
    """Return ``(display_text, tts_text)`` parsed from assistant output.

    Strips any ``<tts>...</tts>`` block from ``text`` to form the
    display text, and returns the inner contents as the spoken TTS
    text. If no ``<tts>`` block is present, ``tts_text`` is empty and
    ``display_text`` equals the input.
    """
    match = _TTS_TAG_RE.search(text)
    if not match:
        return text, ""
    tts_text = match.group(1).strip()
    display_text = _TTS_TAG_RE.sub("", text).rstrip()
    return display_text, tts_text
