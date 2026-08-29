from __future__ import annotations

import re

from app.application.services.nlu import matcher
from app.application.services.nlu.types import ServiceStemProfile

_GREETING_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"привіт|прив|вітаю|доброго\s+(?:ранку|дня|вечора)|"
    r"добрий\s+(?:день|вечір|ранок)|хай|hello|hi|hey|"
    r"good\s+(?:morning|afternoon|evening)"
    r")\s*[,.!:)]*\s*",
    re.IGNORECASE,
)


def strip_greeting_prefix(text: str) -> tuple[str, str]:
    """Returns (greeting_part, remainder). remainder is '' if the whole
    message was just a greeting."""
    match = _GREETING_PREFIX_RE.match(text)
    if not match:
        return "", text
    return text[: match.end()].strip(), text[match.end() :].strip()


def has_substantive_remainder(remainder: str, profiles: dict[str, ServiceStemProfile]) -> bool:
    """Service-aware substantive check: a remainder is substantive if it
    resolves to any known service, or asks a question -- not judged by a
    fixed keyword list."""
    if not remainder:
        return False
    if "?" in remainder:
        return True
    return bool(matcher.match_services(remainder, profiles))
