from __future__ import annotations

import re

_VOWELS = "аеєиіїоуюя"
_VOWEL_REPEAT_RE = re.compile(rf"([{_VOWELS}])\1+")
_PUNCT_RE = re.compile(r"[^\w\sа-яіїєґё]", re.IGNORECASE)


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace and repeated vowels."""
    normalized = " ".join(text.strip().lower().split())
    normalized = " ".join(_PUNCT_RE.sub(" ", normalized).split())
    normalized = _VOWEL_REPEAT_RE.sub(r"\1", normalized)
    return normalized
