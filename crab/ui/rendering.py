"""Rich renderables: markdown detection, chat-bubble panel, history view."""

from __future__ import annotations

import io
from typing import Any

from rich import box as rich_box
from rich.align import Align
from rich.console import Console as _RichConsole
from rich.console import Group
from rich.constrain import Constrain
from rich.markdown import Markdown
from rich.measure import Measurement
from rich.panel import Panel
from rich.text import Text

from crab.config import _NARRATE_TAG_RE
from crab.tts import _extract_tts


# ---------------------------------------------------------------------------
# Chat bubble renderable (60% max width, computed at render time)
# ---------------------------------------------------------------------------

def _contains_markdown(renderable: Any) -> bool:
    """Return True if the renderable is or contains a Markdown instance.

    Markdown has no __rich_measure__, so Measurement.get falls back to
    maximum == options.max_width — useless for sizing. We detect this and
    fall back to render-based measurement.
    """
    if isinstance(renderable, Markdown):
        return True
    if isinstance(renderable, Group):
        return any(_contains_markdown(child) for child in renderable.renderables)
    return False


def _measure_by_render(max_width: int, renderable: Any) -> int:
    """Return the longest rendered line width by rendering off-screen."""
    probe = _RichConsole(
        width=max_width,
        file=io.StringIO(),
        force_terminal=False,
        color_system=None,
        legacy_windows=False,
        record=False,
    )
    lines = probe.render_lines(renderable, probe.options.update_width(max_width), pad=False)
    return max((sum(seg.cell_length for seg in line) for line in lines), default=0)


class _Bubble:
    """Panel that grows to fit its content, capped at 60% of console width.

    Uses render-based measurement for Markdown content (which lacks
    __rich_measure__), and Measurement.get for plain Text and similar.

    ``bg`` sets a Rich style string for the panel background so bubbles
    appear solid over the rain animation layer.
    """

    def __init__(
        self,
        content: Any,
        *,
        align: str = "left",
        **panel_kw: Any,
    ) -> None:
        self._content = content
        self._align = align
        self._panel_kw = panel_kw

    def _measure_title(self, console: Any, options: Any) -> int:
        title = self._panel_kw.get("title")
        if not title:
            return 0
        title_text = console.render_str(title, markup=True) if isinstance(title, str) else title
        return Measurement.get(console, options, title_text).maximum

    def __rich_console__(self, console: Any, options: Any) -> Any:
        cap = max(20, int(options.max_width * 0.6))
        probe_options = options.update_width(cap)

        if _contains_markdown(self._content):
            natural = _measure_by_render(cap, self._content)
        else:
            natural = Measurement.get(console, probe_options, self._content).maximum

        title_width = self._measure_title(console, probe_options)
        content_width = max(natural, title_width)
        width = max(20, min(content_width + 4, cap))  # +4: 2 border + 2 padding

        panel = Panel(self._content, expand=False, **self._panel_kw)
        constrained = Constrain(panel, width=width)
        if self._align == "right":
            yield from Align.right(constrained).__rich_console__(console, options)
        else:
            yield from constrained.__rich_console__(console, options)


# ---------------------------------------------------------------------------
# Conversation history → renderable
# ---------------------------------------------------------------------------

def render_history(history: list[dict[str, Any]], speaker_name: str) -> Any:
    """Build a Rich renderable for the conversation history list.

    `history` items are dicts with a "role" key:
      - "user": ``{"role": "user", "text": str}``
      - "assistant": ``{"role": "assistant", "segments": [(kind, str), ...],
                        "tts"?: str}`` where kind ∈ {"text", "tool"}
      - "error": ``{"role": "error", "text": str}``
    """
    if not history:
        return Align.center(Text("No conversation yet.", style="dim"))

    items: list[Any] = []
    for msg in history:
        role = msg["role"]
        if role == "user":
            items.append(_Bubble(
                Text(msg["text"]),
                align="left",
                title=f"[bold cyan]{speaker_name}[/bold cyan]",
                box=rich_box.ROUNDED,
                border_style="cyan",
            ))
        elif role == "assistant":
            parts: list[Any] = []
            text_chunks: list[str] = []
            for kind, seg in msg["segments"]:
                if kind == "text":
                    text_chunks.append(seg)
                else:
                    if text_chunks:
                        combined = _NARRATE_TAG_RE.sub("", "".join(text_chunks)).strip()
                        display_text, _ = _extract_tts(combined)
                        if display_text:
                            parts.append(Markdown(display_text))
                        text_chunks = []
                    parts.append(Text(seg, style="dim"))
            if text_chunks:
                combined = _NARRATE_TAG_RE.sub("", "".join(text_chunks)).strip()
                display_text, _ = _extract_tts(combined)
                if display_text:
                    parts.append(Markdown(display_text))
            items.append(_Bubble(
                Group(*parts) if parts else Text(""),
                align="right",
                title="[dim]CRAB[/dim]",
                box=rich_box.ROUNDED,
                border_style="dim",
            ))
        elif role == "error":
            items.append(Text(msg["text"], style="bright_red"))
        items.append(Text(""))

    return Group(*items)
