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

    scored.sort(
        key=lambda pair: (
            pair[0].score,
            len(pair[0].matched_stems),
            pair[0].specificity,
            -pair[1],
        ),
        reverse=True,
    )
    return [match for match, _earliest in scored]


def match_service(
    query_text: str,
    profiles: dict[str, ServiceStemProfile],
    *,
    include_description: bool = False,
) -> ServiceMatch | None:
    results = match_services(query_text, profiles, include_description=include_description)
    return results[0] if results else None


# Typo-tolerant fallback -- deliberately separate from match_services/
# match_service above, which stay exact-only. A single character
# substitution/insertion/deletion is common natural typing noise ("чиску"
# for "чистку"); this is scoped narrowly enough to catch that without
# turning into general fuzzy search:
#   - only ever tried when exact matching (match_service) found nothing;
#   - only alias stems, never description prose (too much surface area for
#     accidental collisions);
#   - only stems of at least MIN_FUZZY_STEM_LEN characters, so short
#     stems (which have many one-edit neighbors) are never fuzzy-matched;
#   - an alias must have ALL of its stems satisfied (exactly or by typo) --
#     a partial fuzzy hit on a multi-word alias is not enough;
#   - if a query stem is a one-edit neighbor of more than one distinct
#     alias stem, or if more than one service would end up matching, the
#     whole lookup returns None rather than guessing -- a wrong match is
#     worse than no match.
MIN_FUZZY_STEM_LEN = 4


def _is_one_edit_typo(a: str, b: str) -> bool:
    if a == b:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        mismatches = sum(1 for x, y in zip(a, b) if x != y)
        return mismatches == 1
    # Insertion/deletion: find the single skipped position in the longer
    # stem. Rejected when that position is at either boundary (first or
    # last character) -- that is exactly how a plain derivational pair
    # differs in Ukrainian, in both directions: a trailing consonant added
    # to an adjective root ("чист" vs the noun "чистк") or a verb prefix
    # added to the front ("став" i.e. "became" vs "встав" i.e. "insert!"
    # from "вставити"). Treating either as one-edit-away would make
    # ordinary, unrelated words match a service. A typo dropping or adding
    # a letter *inside* a word (e.g. "чиску" for "чистку") stays accepted,
    # since the missing/extra letter there isn't at either boundary.
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = j = edits = 0
    edit_index: int | None = None
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        edit_index = j
        j += 1
    if edit_index is None:
        # shorter is a plain prefix of longer -- the extra trailing
        # character(s) sit right at the end.
        edit_index = len(longer) - 1
    return edit_index != 0 and edit_index != len(longer) - 1


def _alias_typo_match(alias_stems: frozenset[str], query_stems: frozenset[str]) -> frozenset[str] | None:
    matched: set[str] = set()
    for alias_stem in alias_stems:
        if alias_stem in query_stems:
            matched.add(alias_stem)
            continue
        if len(alias_stem) < MIN_FUZZY_STEM_LEN:
            return None
        candidates = [
            q
            for q in query_stems
            if len(q) >= MIN_FUZZY_STEM_LEN and _is_one_edit_typo(alias_stem, q)
        ]
        if len(candidates) != 1:
            return None
        matched.add(candidates[0])
    return frozenset(matched)


def match_service_typo_tolerant(
    query_text: str,
    profiles: dict[str, ServiceStemProfile],
) -> ServiceMatch | None:
    normalized = normalizer.normalize(query_text)
    stem_sequence = tokenizer.content_stem_sequence(normalized)
    query_stems = frozenset(stem_sequence)
    if not query_stems:
        return None
    first_position: dict[str, int] = {}
    for index, s in enumerate(stem_sequence):
        first_position.setdefault(s, index)

    scored: list[tuple[ServiceMatch, int]] = []
    for profile in profiles.values():
        best_alias_matched: frozenset[str] | None = None
        best_alias_len = 0
        for alias in profile.alias_stem_sets:
            if not alias.stems:
                continue
            matched = _alias_typo_match(alias.stems, query_stems)
            if matched is None:
                continue
            if len(alias.stems) > best_alias_len:
                best_alias_len = len(alias.stems)
                best_alias_matched = matched
        if best_alias_matched:
            earliest = min(first_position[s] for s in best_alias_matched)
            scored.append(
                (
                    ServiceMatch(profile.service_id, float(best_alias_len), best_alias_len, best_alias_matched),
                    earliest,
                )
            )

    if not scored:
        return None
    scored.sort(key=lambda pair: (pair[0].score, -pair[1]), reverse=True)
    if len(scored) > 1 and scored[0][0].score == scored[1][0].score:
        return None
    return scored[0][0]
