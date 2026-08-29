from __future__ import annotations

import re

from app.application.services.nlu import stemmer

_PEDIATRIC_WORD_RE = re.compile(
    r"\b(?:"
    r"дит(?:ин[иоуа]|ячи[йх]|ячу|ячий|ячі|ячого|ячому|ячою|ям|ині|ину)"
    r"|дітям|дітей|дітьми|дітях"
    r"|доньк\w*|доньці|донечк\w*|дочк\w*"
    r"|син(?:у|а|ові|ом)?"
    r"|їй|йому"
    r")\b"
)
_PEDIATRIC_PHRASES = (
    "для дитини",
    "для дітей",
    "обидва дітей",
    "обидва діти",
    "обох дітей",
    "обидвох дітей",
    "двох дітей",
)
_MILK_TOOTH_RE = re.compile(r"\bмолочн\w*\s+зуб")


def has_pediatric_context(normalized_text: str) -> bool:
    return bool(
        _PEDIATRIC_WORD_RE.search(normalized_text)
        or any(phrase in normalized_text for phrase in _PEDIATRIC_PHRASES)
        or _MILK_TOOTH_RE.search(normalized_text)
    )


_EMPHASIS_RE = re.compile(r"\b(?:саме|а\s+саме)\s+([а-яіїєґё]+)")
_NEGATION_RE = re.compile(r"\bне\s+([а-яіїєґё]+)")


def emphasis_stems(normalized_text: str) -> frozenset[str]:
    return frozenset(stemmer.stem(w) for w in _EMPHASIS_RE.findall(normalized_text))


def negated_stems(normalized_text: str) -> frozenset[str]:
    return frozenset(stemmer.stem(w) for w in _NEGATION_RE.findall(normalized_text))
