"""Constants, regex patterns, and on-disk asset loaders for CRAB."""

from __future__ import annotations

import os
import re
from typing import Any


_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_TTS_TAG_RE = re.compile(r"<tts>(.*?)</tts>", re.DOTALL | re.IGNORECASE)
_NARRATE_TAG_RE = re.compile(r"<narrate>(.*?)</narrate>", re.DOTALL | re.IGNORECASE)
_WAKE_WORD_PATTERN = re.compile(r"\bcrab[\s\-]+bot\b")
_DEBUG = bool(os.environ.get("DEBUG"))
_IDLE_BUFFER_MAX = 120

_SPEAKERS_FILE = os.path.join(os.path.dirname(__file__), "..", "speakers.txt")
_ENROLLMENT_SECONDS = 30

_RT_URL = "ws://127.0.0.1:9002/v2" if os.environ.get("SM_LOCAL_CLAUDE_TRANSCRIPTION") else None

_SYSTEM_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "..", "assets", "system_prompt.md")

def _load_system_prompt() -> str:
    try:
        with open(_SYSTEM_PROMPT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

_DOT_INTERVAL = 0.2

_CRAB_ART_FILE = os.path.join(os.path.dirname(__file__), "..", "assets", "crab_art.txt")


def _load_crab_art() -> dict[str, Any]:
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    try:
        with open(_CRAB_ART_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line.startswith("[") and line.endswith("]"):
                    if current is not None:
                        sections[current] = "\n".join(lines).strip("\n")
                    current = line[1:-1]
                    lines = []
                elif current is not None:
                    lines.append(line)
        if current is not None:
            sections[current] = "\n".join(lines).strip("\n")
    except FileNotFoundError:
        pass
    return {
        "title":     sections.get("title",      "C.R.A.B"),
        "idle":      sections.get("idle",       "彡(-.-)ミ\n  ^   ^"),
        "listening": sections.get("listening",  "彡(ᵔᵕᵔ)ミ\n  ^   ^"),
        "thinking":  [
            sections.get("thinking_0", "彡('o')ミ"),
            sections.get("thinking_1", "彡('o')ミ"),
        ],
    }


_CRAB_ART = _load_crab_art()

_THINKING_LABELS: list[str] = [
    "Snipping...",
    "Snapping...",
    "Rangoonin'...",
    "Clawd-ing...",
    "Scuttling...",
    "Pinching...",
    "Shelling...",
    "Nipping...",
    "Pondering...",
    "Molting...",
    "Scripting...",
    "Hermit-ing...",
    "Crabbing...",
    "Crab moding...",
    "Clawpilling...",
]
