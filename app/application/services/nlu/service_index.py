from __future__ import annotations

from typing import Any

from app.application.services.nlu import tokenizer
from app.application.services.nlu.types import AliasStems, ServiceStemProfile


def build_service_index(services: list[dict[str, Any]]) -> dict[str, ServiceStemProfile]:
    profiles: dict[str, ServiceStemProfile] = {}
    for service in services:
        service_id = str(service["id"])
        alias_texts = [str(service.get("name") or "")] + [
            str(a) for a in (service.get("aliases") or [])
        ]
        alias_stem_sets: list[AliasStems] = []
        seen_stem_sets: set[frozenset[str]] = set()
        for alias_text in alias_texts:
            if not alias_text.strip():
                continue
            stems = tokenizer.content_stem_tokens(alias_text)
            # A service's display name often duplicates one of its own
            # aliases in shorter form (e.g. name "Коронки" / alias
            # "коронка" both stem to {"коронк"}) -- count each distinct
            # stem set once so it isn't double-weighted in the scorer.
            if stems and stems not in seen_stem_sets:
                seen_stem_sets.add(stems)
                alias_stem_sets.append(AliasStems(alias_text=alias_text, stems=stems))

        description_stems = tokenizer.content_stem_tokens(str(service.get("description") or ""))
        all_alias_stems = frozenset(s for a in alias_stem_sets for s in a.stems)

        profiles[service_id] = ServiceStemProfile(
            service_id=service_id,
            alias_stem_sets=tuple(alias_stem_sets),
            description_stems=description_stems,
            all_alias_stems=all_alias_stems,
            booking_family_id=str(service.get("booking_service_id") or service_id),
        )
    return profiles
