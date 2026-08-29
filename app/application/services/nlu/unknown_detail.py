from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.application.services.nlu import stemmer, tokenizer
from app.application.services.nlu.types import ServiceStemProfile

# Small, closed set of technique/brand/material words that patients sometimes
# name explicitly and that this KB does not model anywhere (or that name a
# *specific* service that other services must not silently claim). Anything
# NOT in this set is never flagged -- ordinary descriptive/contextual words
# (symptom duration, family words, hedges, numbers) are safe by default.
SEED_ATTRIBUTE_STEMS: frozenset[str] = frozenset(
    stemmer.stem(w)
    for w in [
        "цирконій",
        "invisalign",
        "інвізілайн",
        "лазер",
        "лазерний",  # adjective form ("-н-") doesn't share a stem with the noun
        "мікроскоп",
        "наркоз",
        "седація",
        "емакс",
        "металокераміка",
        "сапфірові",
        "straumann",
        "nobel",
        "airflow",
    ]
)


def build_attribute_index(
    profiles: dict[str, ServiceStemProfile],
    services: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """stem -> set of service_ids for which this attribute is confirmed.

    Deliberately narrow: only two sources feed this index, both inherently
    safe from cross-service leakage --
    1. price_option label residuals: a price option belongs to exactly one
       service by construction, so its qualifier words can never accidentally
       apply elsewhere.
    2. the small manually-curated seed list above.

    (A sibling-family "distinguishing alias" heuristic was tried and dropped:
    a stem unique within one family, like "консультаці", would leak into the
    index as "confirmed only for service X" even though the same stem is
    also a generic, non-distinguishing word used by many unrelated services
    -- causing false positives on ordinary alias words like a service's own
    name. Precision matters more than the extra recall that heuristic would
    have bought.)
    """
    index: dict[str, set[str]] = defaultdict(set)

    for service in services:
        service_id = str(service["id"])
        profile = profiles.get(service_id)
        if profile is None:
            continue
        for option in service.get("price_options") or []:
            label_stems = tokenizer.content_stem_tokens(str(option.get("label") or ""))
            for stem in label_stems - profile.all_alias_stems:
                index[stem].add(service_id)

    for stem in SEED_ATTRIBUTE_STEMS:
        index.setdefault(stem, set())

    return dict(index)


def unknown_detail_stems(
    query_text: str,
    service_id: str,
    attribute_index: dict[str, set[str]],
    profile: ServiceStemProfile | None = None,
) -> frozenset[str]:
    """A stem is flagged only if it's in the closed attribute index AND not
    confirmed for `service_id` there AND not already part of that service's
    own alias vocabulary -- a service's own words can never be "unconfirmed"
    for itself, even if the same stem also happens to appear as a residual
    qualifier word on a different service's price option (e.g. "імплант" is
    dental_implant's own name, even though "Коронка на імпланті" also
    mentions it as a crown variant).
    """
    query_stems = tokenizer.content_stem_tokens(query_text)
    own_stems = profile.all_alias_stems if profile is not None else frozenset()
    return frozenset(
        stem
        for stem in query_stems
        if stem in attribute_index
        and service_id not in attribute_index[stem]
        and stem not in own_stems
    )
