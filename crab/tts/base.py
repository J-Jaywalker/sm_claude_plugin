"""TTS helpers: extract `<tts>` and re-export the `<narrate>` regex."""

from __future__ import annotations

from crab.config import _NARRATE_TAG_RE, _TTS_TAG_RE

__all__ = ["_extract_tts", "_NARRATE_TAG_RE"]


def _extract_tts(text: str) -> tuple[str, str]:
    """Return ``(display_text, tts_text)`` parsed from assistant output.

    Strips any ``<tts>...</tts>`` block from ``text`` to form the
    display text, and returns the inner contents as the spoken TTS
    text. If no ``<tts>`` block is present, ``tts_text`` is empty and
    ``display_text`` equals the input.

    Args:
        text: Raw assistant output, possibly containing a ``<tts>``
            block at the end.

    Returns:
        A tuple ``(display_text, tts_text)``. ``display_text`` has the
        ``<tts>`` block (and surrounding trailing whitespace) removed.
    """
    match = _TTS_TAG_RE.search(text)
    if not match:
        return text, ""
    tts_text = match.group(1).strip()
    display_text = _TTS_TAG_RE.sub("", text).rstrip()
    return display_text, tts_text
