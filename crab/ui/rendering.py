"""Rich renderables: markdown detection and chat-bubble panel."""

from __future__ import annotations

import io
from typing import Any

from rich.align import Align
from rich.console import Console as _RichConsole
from rich.console import Group
from rich.constrain import Constrain
from rich.markdown import Markdown
from rich.measure import Measurement
from rich.panel import Panel


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
