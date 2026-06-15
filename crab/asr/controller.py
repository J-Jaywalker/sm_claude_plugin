"""Voice controller state machine: wake-word, accumulation, EoU dispatch."""

from __future__ import annotations

import asyncio
import re
from enum import Enum
from typing import Any

from pathlib import Path
import time

from crab.config import _DEBUG, _IDLE_BUFFER_MAX, _WAKE_WORD_PATTERN
from crab.speaker_store import _dominant_speaker
from crab.ui.protocol import _UI


_VOICE_DEBUG_LOG = Path("/tmp/crab-channel-debug.log")


def _dlog(msg: str) -> None:
    try:
        with _VOICE_DEBUG_LOG.open("a") as f:
            f.write(f"{time.time():.3f}  voice   {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Normalisation / wake word
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase and collapse punctuation/whitespace for wake word matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class _State(str, Enum):
    IDLE = "IDLE"
    ACCUMULATING = "ACCUMULATING"


class VoiceController:
    """Coordinates microphone capture, ASR events, and prompt assembly."""

    def __init__(self, enrolled_labels: set[str], ui: _UI) -> None:
        self._ui = ui
        self.enrolled_labels = enrolled_labels
        self.state: _State = _State.IDLE
        self.buffer: list[str] = []
        self._idle_buffer: str = ""
        self.listening: asyncio.Event = asyncio.Event()
        self.listening.set()
        self.prompt_ready: asyncio.Event = asyncio.Event()
        self.last_prompt: str = ""
        self.response_done: asyncio.Event = asyncio.Event()
        # Permission-relay listening mode (Phase 2). When active, voice input
        # is captured into permission_answer instead of going through the
        # wake-word state machine.
        self.permission_listening: bool = False
        self._permission_buffer: list[str] = []
        self.permission_answer: str = ""
        self.permission_received: asyncio.Event = asyncio.Event()

    def begin_permission_listen(self) -> None:
        """Switch to capturing the next utterance as a yes/no permission answer."""
        self._permission_buffer.clear()
        self.permission_answer = ""
        self.permission_received.clear()
        self.permission_listening = True
        _dlog("begin_permission_listen")

    def end_permission_listen(self) -> str:
        """Exit permission mode and return the captured answer text."""
        self.permission_listening = False
        _dlog(f"end_permission_listen answer={self.permission_answer!r}")
        return self.permission_answer

    def handle_final(self, message: dict[str, Any]) -> None:
        """Handle an ADD_TRANSCRIPT (final) server message."""
        transcript = message.get("metadata", {}).get("transcript", "")
        if not transcript:
            return

        if self.enrolled_labels:
            speaker = _dominant_speaker(message.get("results", []))
            if speaker is not None and speaker not in self.enrolled_labels:
                if _DEBUG:
                    self._ui.add_tool_use(f"[DBG] ignored transcript from {speaker!r}")
                return

        # Permission mode bypasses the wake-word state machine.
        if self.permission_listening:
            self._permission_buffer.append(transcript.strip())
            self._ui.set_partial(" ".join(self._permission_buffer))
            return

        if self.state is _State.IDLE:
            if _DEBUG:
                self._ui.add_tool_use(f"[DBG] {transcript!r}")
            self._idle_buffer = (self._idle_buffer + " " + transcript)[-_IDLE_BUFFER_MAX:]
            if _WAKE_WORD_PATTERN.search(_normalize(self._idle_buffer)):
                self._idle_buffer = ""
                self.state = _State.ACCUMULATING
                self._ui.set_status("listening")
            return

        # ACCUMULATING: strip wake word tail if present in the first final.
        match = _WAKE_WORD_PATTERN.search(_normalize(transcript))
        if match:
            transcript = transcript[match.end():]

        transcript = transcript.strip()
        if transcript and re.search(r"[a-zA-Z]", transcript):
            self.buffer.append(transcript)
            self._ui.set_partial(" ".join(self.buffer))

    def handle_end_of_utterance(self, message: dict[str, Any]) -> None:
        """Handle an END_OF_UTTERANCE server message."""
        del message
        if _DEBUG:
            self._ui.add_tool_use("[DBG MSG] END_OF_UTTERANCE")

        if self.permission_listening:
            answer = " ".join(self._permission_buffer).strip()
            self._permission_buffer.clear()
            self.permission_answer = answer
            _dlog(f"permission EoU answer={answer!r}")
            self.permission_received.set()
            return

        if self.state is _State.IDLE:
            _dlog(f"EoU (state=IDLE, idle_buf={self._idle_buffer!r}) — ignoring")
            self._idle_buffer = ""
            return

        prompt = " ".join(self.buffer).strip()
        self.buffer.clear()
        self._idle_buffer = ""

        if not prompt:
            _dlog("EoU (state=ACCUMULATING, empty buffer) — staying")
            # Wake word heard but no command yet — stay ACCUMULATING.
            return

        if not re.search(r"[a-zA-Z]{2,}", prompt):
            _dlog(f"EoU (noise-only prompt={prompt!r}) — reset")
            # Only punctuation/noise — reset silently so user can try again.
            self.state = _State.IDLE
            self._ui.set_status("idle")
            return

        _dlog(f"EoU FIRE prompt={prompt!r} prompt_ready_was_set={self.prompt_ready.is_set()}")
        self.state = _State.IDLE
        self.last_prompt = prompt
        self._ui.add_user_message(prompt)
        self._ui.set_status("thinking")
        self.prompt_ready.set()
