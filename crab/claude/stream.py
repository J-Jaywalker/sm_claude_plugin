"""Translate `claude --output-format stream-json` events into UI updates."""

from __future__ import annotations

import os
from typing import Any

from crab.ui.protocol import _UI


# ---------------------------------------------------------------------------
# Claude output rendering + driver
# ---------------------------------------------------------------------------

def _handle_stream_event(event: dict[str, Any], ui: _UI) -> None:
    """Dispatch a claude --output-format stream-json event to the UI."""
    event_type = event.get("type")

    if event_type == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text":
                ui.add_assistant_text(block["text"])
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                if name in ("Edit", "Write"):
                    ui.add_tool_use(f"[EDIT] {os.path.relpath(inp.get('file_path', '?'))}")
                elif name == "Bash":
                    ui.add_tool_use(f"[BASH] {inp.get('command', '?')[:120]}")
                elif name:
                    ui.add_tool_use(f"[TOOL] {name}")

    elif event_type == "user":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            content = block.get("content", "")
            if isinstance(content, list):
                output = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
            else:
                output = str(content)
            output = output.strip()
            if output:
                truncated = output[:200] + ("…" if len(output) > 200 else "")
                ui.add_tool_use(f"  └─ {truncated}")

    elif event_type == "result":
        if event.get("subtype") == "error":
            ui.add_error_message(f"[CLAUDE ERROR] {event.get('error', '')}")
        elif event.get("subtype") == "success":
            usage = event.get("usage", {})
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            duration_s = (event.get("duration_ms") or 0) / 1000
            parts = [f"{in_tok:,} in", f"{out_tok:,} out"]
            if cache_read:
                parts.append(f"{cache_read:,} cached")
            parts.append(f"Completed in {duration_s:.1f}s")
            ui.add_tool_use("[DONE] " + " · ".join(parts))
