from __future__ import annotations

import re

from app.application.services.nlu import normalizer, stemmer

_TOKEN_RE = re.compile(r"[а-яіїєґёa-z0-9]+", re.IGNORECASE)

# Generic function words only -- deliberately small. Content words (symptom
# descriptions, family words, numbers, hedges) are NOT filtered here; the
# whole point of this module is that ordinary descriptive words stay
# harmless by default instead of needing to be enumerated as "safe".
_STOPWORDS = {
    "і", "й", "а", "у", "в", "з", "із", "зі", "на", "до", "по", "за", "про",
    "це", "то", "чи", "що", "як", "ну", "та", "але", "або", "не", "ні",
    "я", "ви", "ти", "ми", "він", "вона", "воно", "вони",
    # Bare conversational acknowledgment/discourse markers. These carry no
    # service-identifying content on their own, and excluding them here
    # avoids short-word stem collisions with unrelated KB vocabulary (e.g.
    # "ясно" i.e. "got it" stems the same as "ясна" i.e. "gums"). Reuses the
    # same words reply_service.py already treats as pure acknowledgments
    # (_looks_like_short_acknowledgement) rather than picking a new list.
    "ага", "ясно", "зрозуміло", "зрозумів", "зрозуміла", "ок", "окей",
    "добре", "гаразд", "звісно",
}


# Modal-auxiliary verb stems ("могти" -- can/may). Structurally a function
# word like "is"/"can"/"will" in English, not content -- excluded by stem
# (not surface form) so every inflection (може/можу/можна/можемо/...) is
# covered without enumerating each one, and so it can never collide with an
# unrelated short KB word the way the un-stemmed forms don't.
_STOPWORD_STEMS = {"мож"}


def content_stem_sequence(text: str) -> list[str]:
    normalized = normalizer.normalize(text)
    words = _TOKEN_RE.findall(normalized)
    stems = (stemmer.stem(word) for word in words if word not in _STOPWORDS and len(word) > 1)
    return [s for s in stems if s not in _STOPWORD_STEMS]


def content_stem_tokens(text: str) -> frozenset[str]:
    return frozenset(content_stem_sequence(text))
