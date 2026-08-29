from __future__ import annotations

from app.application.services.nlu import modifiers, normalizer, tokenizer
from app.application.services.nlu.types import ServiceMatch, ServiceStemProfile

DESCRIPTION_STEM_WEIGHT = 0.5


def match_services(
    query_text: str,
    profiles: dict[str, ServiceStemProfile],
    *,
    include_description: bool = False,
) -> list[ServiceMatch]:
    normalized = normalizer.normalize(query_text)
    stem_sequence = tokenizer.content_stem_sequence(normalized)
    query_stems = frozenset(stem_sequence)
    if not query_stems:
        return []
    # first position each stem appears at, for tie-breaking by "what was
    # mentioned first" when two services score identically.
    first_position: dict[str, int] = {}
    for index, s in enumerate(stem_sequence):
        first_position.setdefault(s, index)

    emphasized = modifiers.emphasis_stems(normalized)
    negated = modifiers.negated_stems(normalized)

    scored: list[tuple[ServiceMatch, int]] = []
    for profile in profiles.values():
        score = 0.0
        specificity = 0
        matched: set[str] = set()

        for alias in profile.alias_stem_sets:
            if not alias.stems or not alias.stems <= query_stems:
                continue
            weight = float(len(alias.stems))
            if alias.stems & emphasized:
                weight += 2.0
            if alias.stems & negated:
                weight -= 2.0
            score += weight
            specificity = max(specificity, len(alias.stems))
            matched |= alias.stems

        if include_description and profile.description_stems:
            overlap = profile.description_stems & query_stems
            score += DESCRIPTION_STEM_WEIGHT * len(overlap)
            matched |= overlap

        if score > 0:
            earliest = min(first_position[s] for s in matched)
            scored.append(
                (ServiceMatch(profile.service_id, score, specificity, frozenset(matched)), earliest)
            )

    scored.sort(key=lambda pair: (pair[0].score, pair[0].specificity, -pair[1]), reverse=True)
    return [match for match, _earliest in scored]


def match_service(
    query_text: str,
    profiles: dict[str, ServiceStemProfile],
    *,
    include_description: bool = False,
) -> ServiceMatch | None:
    results = match_services(query_text, profiles, include_description=include_description)
    return results[0] if results else None
