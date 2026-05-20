"""Reusable Textual widgets for the CRAB TUI."""

from __future__ import annotations

from textual.message import Message
from textual.widgets import Static


# ---------------------------------------------------------------------------
# Settings panel widget
# ---------------------------------------------------------------------------

class SettingsPanel(Static):
    """Square panel in the top-right corner. Green normally, orange on hover."""

    can_focus = False

    class OpenSettings(Message):
        pass

    def on_mount(self) -> None:
        self.update("SETTINGS")

    def on_click(self) -> None:
        self.post_message(self.OpenSettings())
