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
    # Remaining common prepositions -- this list started incomplete (missing
    # these let "після" leak through as a "content" word and pick up a weak
    # but decisive match via a service description's prose, e.g. "після 16"
    # scoring against pediatric_caries_treatment purely because its
    # description happens to contain "після" too).
    "після", "при", "через", "під", "над", "без", "для", "коло", "біля",
    "між", "серед",
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
# (not surface form) so every inflection (може/можу/можемо/...) is covered
# without enumerating each one, and so it can never collide with an
# unrelated short KB word the way the un-stemmed forms don't.
#
# NOTE: "можна" (-> stem "можн") is deliberately NOT included here even
# though it's the same modal -- message_processor.py's own booking-intent
# heuristics rely on a service lookup that includes "можна" incidentally
# matching to decide a date+call-request message continues an active
# booking (see _looks_like_booking_message). Excluding "можн" here breaks
# that unrelated mechanism. Left as a known asymmetry rather than widening
# this fix into message_processor.py, which is out of scope here.
#
# "записати" (to book/register) is excluded for a different reason: booking
# intent is already detected by a dedicated subsystem (booking_service.py +
# message_processor.py's own booking heuristics), so it never needs to be a
# service/FAQ-matching signal -- and left in, it's dangerously generic: an
# FAQ whose question reduces to just that one stem ("Як записатися?") would
# match almost any in-progress booking message ("хочу тоді записатись" etc,
# which stems the same way via the reflexive "-атися/-атись" ending) and
# hijack it into a static informational answer instead of letting the
# booking flow continue.
_STOPWORD_STEMS = {"мож", "записати"}


def content_stem_sequence(text: str) -> list[str]:
    normalized = normalizer.normalize(text)
    words = _TOKEN_RE.findall(normalized)
    stems = (stemmer.stem(word) for word in words if word not in _STOPWORDS and len(word) > 1)
    return [s for s in stems if s not in _STOPWORD_STEMS]


def content_stem_tokens(text: str) -> frozenset[str]:
    return frozenset(content_stem_sequence(text))
