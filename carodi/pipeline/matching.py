"""Keyword matching that survives punctuation.

The identity helpers in models.py strip punctuation, which is right for dedupe
('ACME, Inc.' and 'ACME Inc' are one employer) and wrong for keywords: it turns
'c++' into 'c', which then matches the bare letter c in any prose. Keyword
matching therefore gets its own normalizer that keeps symbols.
"""

from __future__ import annotations

import re
from functools import lru_cache

_WS = re.compile(r"\s+")

# Treat only alphanumerics as "inside a word", so 'c++' matches at the end of a
# token and 'go' does not match inside 'golang'.
_LEFT = r"(?<![a-z0-9])"
_RIGHT = r"(?![a-z0-9])"


def normalize_text(text: str) -> str:
    """Lowercase and collapse whitespace, preserving punctuation."""
    return _WS.sub(" ", (text or "").casefold()).strip()


@lru_cache(maxsize=2048)
def phrase_regex(phrase: str) -> re.Pattern[str] | None:
    phrase = normalize_text(phrase)
    if not phrase:
        return None
    # Anchor with word boundaries only where the phrase edge is alphanumeric;
    # a phrase ending in '+' needs no right-hand guard beyond the literal.
    left = _LEFT if phrase[0].isalnum() else ""
    right = _RIGHT if phrase[-1].isalnum() else ""
    return re.compile(f"{left}{re.escape(phrase)}{right}")


def find_phrase(haystack: str, phrases: list[str]) -> str | None:
    """Return the first phrase present in the haystack, or None."""
    for phrase in phrases:
        pattern = phrase_regex(phrase)
        if pattern and pattern.search(haystack):
            return phrase
    return None


def matches(haystack: str, phrase: str) -> bool:
    pattern = phrase_regex(phrase)
    return bool(pattern and pattern.search(haystack))
