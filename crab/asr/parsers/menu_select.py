"""Voice-friendly parser for the channel `ask_menu` flow.

Translates a free-form transcript (the user's spoken answer) into one of:
  - an integer option index (0-based) when the choice is clear
  - `CANCEL` (-1) for explicit dismiss intent ("cancel", "never mind")
  - ``None`` for ambiguous / unparseable input

Strategies tried in order:
  1. Cancel patterns
  2. Numeric / ordinal patterns ("one", "two", "first", "option 2", "the third")
  3. Exact label match against any option
  4. Single-option word-boundary substring match
  5. Token-overlap Jaccard match — catches natural phrasings like
     "the polish one", "I'd like the rewrite", "do the audit thing"
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

CANCEL: int = -1

# Words discounted when comparing the user's utterance to an option label.
# These appear in almost every spoken answer and would otherwise inflate the
# similarity of unrelated options.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "this", "that", "it", "of", "for", "to", "and", "or",
    "is", "be", "you", "me", "my",
    "i", "id", "ill", "im",
    "want", "like", "would", "could", "should",
    "go", "with", "do", "pick", "choose", "select", "prefer", "take",
    "let", "lets", "please", "thanks",
    "one", "option", "number",
    "yeah", "yes", "ok", "okay", "sure",
})

# Minimum Jaccard similarity required for the token-overlap fallback to
# accept a match. Tuned to allow short queries ("trim" against "Trim the
# training section" → overlap=1, union=2, sim=0.5) while rejecting noise.
_TOKEN_OVERLAP_MIN: float = 0.15

_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6,
    # Common ASR mishearings — Speechmatics frequently maps short spoken
    # digits to homophones.
    "won": 1, "wan": 1, "wun": 1,
    "to": 2, "too": 2, "tu": 2,
    "tree": 3, "thee": 3,
    "for": 4, "fore": 4,
    "fives": 5, "fire": 5,
}

_CANCEL_RE = re.compile(
    r"^\s*(crab[\s\-]*bot[\s,.:;-]*)?"
    r"(cancel|never\s*mind|nevermind|forget\s+it|skip|escape|exit|abort)\b",
    flags=re.IGNORECASE,
)

# Strip leading filler so the rest can be parsed cleanly. Order matters —
# longer prefixes first.
_PREFIX_RE = re.compile(
    r"^\s*(crab[\s\-]*bot[\s,.:;-]*)?"
    r"(?:"
    r"i'?(?:ll|d\s+like(?:\s+to)?|\s+want(?:\s+to)?|\s+pick|\s+choose|\s+select|\s+prefer)\s+(?:go\s+with\s+)?(?:option\s+|the\s+|number\s+)?"
    r"|let'?s\s+(?:go\s+with|do|try|pick)\s+(?:option\s+|the\s+|number\s+)?"
    r"|go\s+with\s+(?:option\s+|the\s+|number\s+)?"
    r"|option\s+(?:number\s+)?"
    r"|number\s+"
    r"|the\s+"
    r")?",
    flags=re.IGNORECASE,
)


def parse_menu_choice(text: str, options: list[str]) -> Optional[int]:
    """Parse a transcript into an option index, ``CANCEL``, or ``None``."""
    if not text or not options:
        return None

    raw = text.strip().lower().rstrip(".!?,")
    if not raw:
        return None

    if _CANCEL_RE.match(raw):
        return CANCEL

    cleaned = _PREFIX_RE.sub("", raw, count=1).strip()
    if not cleaned:
        return None

    # 1. Leading digit ("2", "2 please")
    m = re.match(r"^(\d+)\b", cleaned)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(options):
            return idx

    # 2. Number / ordinal word as the first token
    first_word = cleaned.split(maxsplit=1)[0]
    if first_word in _NUMBER_WORDS:
        idx = _NUMBER_WORDS[first_word] - 1
        if 0 <= idx < len(options):
            return idx

    # 3. Exact label match (case-insensitive, trimming trailing punctuation)
    norm_options = [opt.strip().lower().rstrip(".!?,") for opt in options]
    if cleaned in norm_options:
        return norm_options.index(cleaned)

    # 4. Single-option word-boundary match — only accept if exactly one option
    #    contains the spoken phrase as a whole word (or vice versa). Word
    #    boundaries avoid "a" matching "what" by virtue of the letter 'a';
    #    requiring a single match avoids "polish" arbitrarily picking
    #    between "Light polish" and "Incremental polish".
    needle_re = re.compile(r"\b" + re.escape(cleaned) + r"\b")
    matches: list[int] = []
    for i, label in enumerate(norm_options):
        if needle_re.search(label) or re.search(r"\b" + re.escape(label) + r"\b", cleaned):
            matches.append(i)
    if len(matches) == 1:
        return matches[0]

    # 5. Token-overlap fallback. Strip stopwords ("the", "I want", "please",
    #    etc.) from both sides, compute Jaccard similarity on the remaining
    #    content tokens, and pick the option with the strictly highest score
    #    (above the floor). Catches "the polish one", "do the audit thing",
    #    "I'd like the rewrite".
    return _token_overlap_match(cleaned, norm_options)


def _content_tokens(text: str) -> set[str]:
    """Return the set of content words in `text` (lowercase, stopwords removed)."""
    tokens = re.findall(r"\w+", text.lower())
    return {t for t in tokens if t not in _STOPWORDS}


def _prefix_match(a: str, b: str) -> bool:
    """Two tokens match if equal or one is a ≥4-char prefix of the other.

    Catches inflections like "trim" ↔ "trimming", "polish" ↔ "polishing",
    "head" ↔ "headings". Minimum length avoids junk like "do" matching
    "document".
    """
    if a == b:
        return True
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return len(short) >= 4 and long.startswith(short)


def _fuzzy_overlap_count(needle: set[str], haystack: set[str]) -> int:
    """Number of needle tokens that have a prefix-match in haystack."""
    count = 0
    for n in needle:
        for h in haystack:
            if _prefix_match(n, h):
                count += 1
                break
    return count


def _token_overlap_match(needle: str, options: list[str]) -> Optional[int]:
    """Jaccard-similarity match. Returns the uniquely-best option or None."""
    needle_tokens = _content_tokens(needle)
    if not needle_tokens:
        return None
    scores: list[float] = []
    for label in options:
        haystack = _content_tokens(label)
        union = needle_tokens | haystack
        if not union:
            scores.append(0.0)
            continue
        overlap = _fuzzy_overlap_count(needle_tokens, haystack)
        scores.append(overlap / len(union))
    best = max(scores)
    if best < _TOKEN_OVERLAP_MIN:
        return None
    top = [i for i, s in enumerate(scores) if s == best]
    return top[0] if len(top) == 1 else None


# ---------------------------------------------------------------------------
# LLM fallback — spawn a separate `claude -p` to interpret loose phrasings
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = (
    "A voice user is answering a multi-choice menu. Their phrasing may be "
    "loose, vague, or only loosely related to the literal option labels — "
    "interpret generously and pick the option they most likely meant.\n\n"
    "Respond with ONE short line:\n"
    "  - the option's 0-based index (an integer), OR\n"
    "  - the option's label (as written), OR\n"
    "  - the word 'cancel' if they clearly want to dismiss the menu, OR\n"
    "  - the word 'unclear' only if you genuinely cannot decide.\n\n"
    "Be decisive — prefer a best-guess interpretation over 'unclear'. Do not "
    "explain your reasoning. No markdown, no extra punctuation."
)

_LLM_TIMEOUT_S = 20.0


async def llm_interpret_menu(spoken: str, options: list[str]) -> Optional[int]:
    """Ask `claude -p` to interpret a loose menu answer.

    Used as a fallback when rule-based parsing returns None. The interpreter
    is run from a neutral cwd (``/tmp``) so the project's CLAUDE.md doesn't
    inject CRAB-BOT persona into it.

    The LLM may respond with an integer index, the option label verbatim, or
    a short descriptive phrase. We post-process by:
      1. Quick "cancel" / "unclear" keyword check on the response head
      2. Pull any integer from the response that lies in range
      3. Fall back to running the LLM's response through ``parse_menu_choice``
         — that catches "Light polish" → 0, "the polish one" → 0, etc.

    Returns the chosen index, :data:`CANCEL`, or ``None`` if everything fails.
    """
    if not spoken or not options:
        return None

    user_lines = [f'User said: "{spoken}"', "", "Options:"]
    for i, opt in enumerate(options):
        user_lines.append(f"  {i}: {opt}")
    user_lines.append("")
    user_lines.append("Which option did they most likely mean?")
    user_prompt = "\n".join(user_lines)

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p",
            "--system-prompt", _LLM_SYSTEM_PROMPT,
            user_prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd="/tmp",
        )
    except (FileNotFoundError, OSError):
        return None

    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=_LLM_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return None

    text = out_b.decode(errors="replace").strip()
    if not text:
        return None

    return _interpret_llm_response(text, options)


def _interpret_llm_response(text: str, options: list[str]) -> Optional[int]:
    """Multi-strategy parse of the LLM's free-form response."""
    head = text[:80].lower()

    # 'unclear' must take precedence over 'cancel' since the system prompt
    # treats 'unclear' as a refusal; 'cancel' is an active intent.
    if "unclear" in head:
        return None
    if re.search(r"\bcancel\b", head):
        return CANCEL

    # Any in-range integer in the response. Iterate so something like
    # "1 (Light polish)" or "Option 2" still resolves.
    for m in re.finditer(r"\b(\d+)\b", text):
        idx = int(m.group(1))
        if 0 <= idx < len(options):
            return idx

    # Run the LLM's full response through the rule-based parser. This
    # catches free-form picks like "Light polish", "the polish one",
    # "do the audit thing" by going through exact-match → word-boundary →
    # token-overlap strategies.
    return parse_menu_choice(text, options)
