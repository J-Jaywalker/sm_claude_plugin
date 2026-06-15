"""Voice-friendly yes/no parser for the permission-relay flow.

Returns "allow" for clear yes-class answers, "deny" for no-class answers, and
``None`` for anything else. Tolerates a leading wake-word slip ("crab bot, yes
please" still parses to 'allow').
"""

from __future__ import annotations

import re
from typing import Optional

# Strip an optional leading "crab bot" / "crab-bot" prefix and any trailing
# punctuation/whitespace before matching.
_STRIP_PREFIX_RE = re.compile(
    r"^\s*(crab[\s\-]*bot[\s,.:;-]*)?",
    flags=re.IGNORECASE,
)

_YES_RE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|alright|allow|approve|confirm|"
    r"go(?:\s+ahead)?|do\s+it|please|continue|proceed|fine|affirmative)\b",
    flags=re.IGNORECASE,
)

_NO_RE = re.compile(
    r"^(no|nope|nah|deny|cancel|stop|don'?t|do\s+not|abort|skip|reject|"
    r"negative)\b",
    flags=re.IGNORECASE,
)


def parse_yes_no(text: str) -> Optional[str]:
    """Parse a transcript into a permission verdict.

    Returns:
        "allow" if the text starts with a yes-class word.
        "deny"  if the text starts with a no-class word.
        ``None`` if the answer is ambiguous, empty, or off-topic.
    """
    if not text:
        return None
    cleaned = _STRIP_PREFIX_RE.sub("", text).strip()
    if not cleaned:
        return None
    if _YES_RE.match(cleaned):
        return "allow"
    if _NO_RE.match(cleaned):
        return "deny"
    return None
