from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AliasStems:
    alias_text: str
    stems: frozenset[str]


@dataclass(frozen=True)
class ServiceStemProfile:
    service_id: str
    alias_stem_sets: tuple[AliasStems, ...]
    description_stems: frozenset[str]
    all_alias_stems: frozenset[str]
    booking_family_id: str


@dataclass(frozen=True)
class ServiceMatch:
    service_id: str
    score: float
    specificity: int
    matched_stems: frozenset[str]
