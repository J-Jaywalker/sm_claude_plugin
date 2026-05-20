"""Persistence and aggregation helpers for enrolled speakers."""

from __future__ import annotations

from typing import Any

from crab.config import _SPEAKERS_FILE


# ---------------------------------------------------------------------------
# Speaker store
# ---------------------------------------------------------------------------

def _load_speakers() -> dict[str, list[str]]:
    """Load enrolled speakers from file. Returns {name: [identifier, ...]}."""
    speakers: dict[str, list[str]] = {}
    try:
        with open(_SPEAKERS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                name, ids_str = line.split(":", 1)
                ids = [i for i in ids_str.split(",") if i]
                if name and ids:
                    speakers[name] = ids
    except FileNotFoundError:
        pass
    return speakers


def _save_speakers(speakers: dict[str, list[str]]) -> None:
    with open(_SPEAKERS_FILE, "w") as f:
        for name, ids in speakers.items():
            f.write(f"{name}:{','.join(ids)}\n")


def _dominant_speaker(results: list[dict[str, Any]]) -> str | None:
    """Return the most-frequent speaker label across all word alternatives."""
    counts: dict[str, int] = {}
    for result in results:
        for alt in result.get("alternatives", []):
            spk = alt.get("speaker")
            if spk:
                counts[spk] = counts.get(spk, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])
