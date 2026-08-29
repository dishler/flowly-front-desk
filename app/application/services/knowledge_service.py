from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from app.application.services.nlu import matcher as nlu_matcher
from app.application.services.nlu import modifiers as nlu_modifiers
from app.application.services.nlu import normalizer as nlu_normalizer
from app.application.services.nlu import service_index as nlu_service_index
from app.application.services.nlu import unknown_detail as nlu_unknown_detail
from app.core.config import get_settings

_PEDIATRIC_CONTEXT_FALLBACK_MARKERS = (
    "зуб",
    "запис",
    "прийом",
    "прийма",
    "консультац",
    "стоматолог",
    "лікар",
    "можна",
    "до вас",
    "бої",
    "бою",
    "страх",
)


class KnowledgeService:
    def __init__(self, file_path: str | None = None) -> None:
        if file_path is None:
            file_path = get_settings().knowledge_base_path
        self.file_path = Path(file_path)
        self.data = self._load()
        self._nlu_profiles = nlu_service_index.build_service_index(self.get_services())
        self._nlu_attribute_index = nlu_unknown_detail.build_attribute_index(
            self._nlu_profiles, self.get_services()
        )
        # "pediatric_dentistry" is the generic pediatric catch-all; its own
        # alias set includes bare pediatric-context words (e.g. "дітей"),
        # which lets it out-score a more specific procedure alias (e.g.
        # "чистка") in a plain match purely because a pediatric-context word
        # is present -- not because it's actually the right service. Pediatric
        # resolution therefore matches against every other service first (see
        # find_confident_service) and only falls back to this generic bucket
        # when nothing more specific is found.
        self._nlu_non_generic_profiles = {
            service_id: profile
            for service_id, profile in self._nlu_profiles.items()
            if service_id != "pediatric_dentistry"
        }
        self._nlu_adult_to_pediatric = {
            adult_id: pediatric_id
            for pediatric_id, adult_id in self._PEDIATRIC_ADULT_COUNTERPART.items()
        }
        # FAQ entries reuse the same service-alias matcher: each FAQ's
        # "question" is its primary alias, and an optional "aliases" list
        # lets a topic be phrased multiple genuinely different ways (e.g.
        # "розтермінування" as a synonym of "розстрочка") without any
        # per-phrase branching in code -- the stemmed full-coverage matcher
        # already handles word-form variance (страховку/страховкою etc.).
        faq_index_entries = [
            {
                "id": str(index),
                "name": str(item.get("question") or ""),
                "aliases": [str(a) for a in (item.get("aliases") or []) if a],
            }
            for index, item in enumerate(self.get_faq())
        ]
        self._nlu_faq_profiles = nlu_service_index.build_service_index(faq_index_entries)

    def _load(self) -> dict[str, Any]:
        with self.file_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def get_company(self) -> dict[str, Any]:
        return self.data.get("company", {})

    def get_business(self) -> dict[str, Any]:
        return self.data.get("business", self.get_company())

    def get_services(self) -> list[dict[str, Any]]:
        return self.data.get("services", [])

    def get_primary_service(self) -> dict[str, Any]:
        services = self.get_services()
        return services[0] if services else {}

    def get_service_by_id(self, service_id: str) -> Optional[dict[str, Any]]:
        for service in self.get_services():
            if service.get("id") == service_id:
                return service
        return None

    _PEDIATRIC_ADULT_COUNTERPART = {
        "pediatric_caries_treatment": "caries_treatment",
        "pediatric_cleaning": "dental_cleaning",
        "pediatric_dentistry": "dental_consultation",
    }

    def get_adult_counterpart_service(self, pediatric_service_id: str) -> Optional[dict[str, Any]]:
        """Resolves the adult equivalent of a pediatric service family (e.g.
        pediatric_cleaning -> dental_cleaning) so a bare "а дорослим?"
        follow-up can be grounded in the adult version of whatever
        pediatric topic is currently active, rather than falling through to
        an unrelated service whose description text happens to mention
        "дорослим" in passing. Scoped per pediatric family, not a single
        universal "adult" target.
        """
        adult_id = self._PEDIATRIC_ADULT_COUNTERPART.get(pediatric_service_id)
        if adult_id is None:
            return None
        return self.get_service_by_id(adult_id)

    def find_service(self, text: str) -> Optional[dict[str, Any]]:
        match = nlu_matcher.match_service(text, self._nlu_profiles, include_description=True)
        return self.get_service_by_id(match.service_id) if match else None

    def find_bounded_consultation_service(self, text: str) -> Optional[dict[str, Any]]:
        """Resolves a dental consultation reference that alias matching
        alone misses -- "огляд" isn't a configured alias of
        dental_consultation, but it unambiguously means one when it's
        either explicitly dental-qualified ("огляд стоматолога",
        "стоматологічний огляд") or the bare object of a booking phrase
        ("хочу на огляд", "запишіть мене на огляд"). A qualifier attached
        directly to "огляд" (e.g. "огляд машини", "технічний огляд") means
        it isn't the trailing, unqualified noun these patterns require, so
        it correctly falls through unmatched -- no explicit exclusion list
        needed.
        """
        normalized = self._normalize_question_for_match(text)
        if not normalized:
            return None
        dental_qualified = bool(
            re.search(r"\bогляд\s+стоматолог", normalized)
            or re.search(r"\bстоматологічн\w*\s+огляд\b", normalized)
        )
        bare_dental_reference = bool(re.search(r"(?:^|\bна\s+)огляд[ау]?$", normalized))
        dental_price_or_booking_reference = bool(
            re.search(r"\bогляд[ау]?\b", normalized)
            and re.search(
                r"\b(?:скільки|коштує|вартість|ціна|ціну|прайс|записа\w*|запиш\w*|прийом|сьогодні|завтра|післязавтра|понеділ\w*|вівтор\w*|серед\w*|четвер\w*|пʼятниц\w*|п'ятниц\w*|пятниц\w*|субот\w*|неділ\w*|ранку|зранку|вранці|обіду|вечір|вечері)\b",
                normalized,
            )
        )
        if not (dental_qualified or bare_dental_reference or dental_price_or_booking_reference):
            return None
        return self.get_service_by_id("dental_consultation")

    def find_confident_service(self, text: str) -> Optional[dict[str, Any]]:
        normalized = nlu_normalizer.normalize(text)
        if not normalized:
            return None

        if nlu_modifiers.has_pediatric_context(normalized):
            return self._find_pediatric_aware_service(normalized)

        match = nlu_matcher.match_service(normalized, self._nlu_profiles)
        if match is None:
            # Exact/morphological matching found nothing at all -- only
            # then try a single-character-typo-tolerant fallback (e.g.
            # "чиску" for "чистку"). Never runs when exact matching already
            # found a service, so it can't override a good match.
            match = nlu_matcher.match_service_typo_tolerant(normalized, self._nlu_profiles)
        return self.get_service_by_id(match.service_id) if match else None

    def _find_pediatric_aware_service(self, normalized: str) -> Optional[dict[str, Any]]:
        """Matches against every service except the generic "pediatric_dentistry"
        catch-all first (see the comment in __init__ on why), redirects an
        adult match to its pediatric counterpart when one exists, keeps a
        match that has no pediatric counterpart as-is (e.g. a chipped-tooth
        restoration -- there's no pediatric-specific variant in this KB, so
        the honest answer is the adult service), and only falls back to the
        generic bucket when nothing more specific matched at all and the
        message still looks dental-relevant.
        """
        specific_match = nlu_matcher.match_service(normalized, self._nlu_non_generic_profiles)
        specific_id = specific_match.service_id if specific_match else None

        if specific_id in self._nlu_adult_to_pediatric:
            service_id = self._nlu_adult_to_pediatric[specific_id]
        elif specific_id is not None:
            service_id = specific_id
        elif any(marker in normalized for marker in _PEDIATRIC_CONTEXT_FALLBACK_MARKERS):
            service_id = "pediatric_dentistry"
        else:
            service_id = None

        return self.get_service_by_id(service_id) if service_id else None

    def get_pricing(self) -> dict[str, Any]:
        return self.data.get("pricing", {})

    def get_consultation(self) -> dict[str, Any]:
        return self.data.get("consultation", {})

    def get_faq(self) -> list[dict[str, Any]]:
        return self.data.get("faq", [])

    def get_all_faq(self) -> list[dict[str, Any]]:
        return self.get_faq()

    def _normalize_question_for_match(self, text: str) -> str:
        normalized = text.strip().lower()
        normalized = re.sub(r"[^\w\sа-яіїєґё]", " ", normalized, flags=re.IGNORECASE)
        return " ".join(normalized.split())

    def _question_tokens(self, text: str) -> set[str]:
        stopwords = {
            "а",
            "є",
            "у",
            "в",
            "ви",
            "вас",
            "ваші",
            "ваша",
            "ваше",
            "які",
            "яка",
            "який",
            "що",
            "чи",
            "про",
            "the",
            "a",
            "an",
            "do",
            "you",
            "your",
            "are",
            "is",
            "what",
            "which",
        }
        return {
            token
            for token in self._normalize_question_for_match(text).split()
            if len(token) > 2 and token not in stopwords
        }

    def _service_query_tokens(self, text: str) -> set[str]:
        stopwords = {
            "а",
            "і",
            "и",
            "по",
            "про",
            "у",
            "в",
            "на",
            "за",
            "ціна",
            "ціну",
            "ціни",
            "вартість",
            "скільки",
            "коштує",
            "прайс",
            "price",
            "cost",
            "about",
            "for",
            "the",
        }
        return {
            token
            for token in self._normalize_question_for_match(text).split()
            if len(token) > 2 and token not in stopwords
        }

    def _service_tokens(self, service: dict[str, Any], *, include_description: bool = True) -> set[str]:
        parts = [
            str(service.get("id") or ""),
        ]
        if include_description:
            parts.append(str(service.get("name") or ""))
            parts.append(str(service.get("description") or ""))
        aliases = service.get("aliases")
        if isinstance(aliases, list):
            parts.extend(str(alias) for alias in aliases if alias)
        price_options = service.get("price_options")
        if isinstance(price_options, list):
            for option in price_options:
                if isinstance(option, dict):
                    parts.append(str(option.get("label") or ""))
        return self._service_query_tokens(" ".join(parts))

    def unknown_detail_tokens_for_service(self, text: str, service: dict[str, Any]) -> set[str]:
        """Returns query stems that name a specific service attribute
        (material/brand/technique) not confirmed for the matched service --
        e.g. asking about "цирконій" while discussing a service whose price
        options don't include it. Allow-by-default: only stems in the small,
        closed attribute index (derived from price_option labels plus a
        short seed list of technique/brand words this KB doesn't model) can
        ever be flagged -- ordinary descriptive/contextual words are never
        in that index, so they're never flagged, with no growing safe-word
        list to maintain.
        """
        service_id = str(service.get("id") or "")
        profile = self._nlu_profiles.get(service_id)
        return set(
            nlu_unknown_detail.unknown_detail_stems(
                text, service_id, self._nlu_attribute_index, profile
            )
        )

    def _token_matches_any(self, token: str, candidates: set[str]) -> bool:
        for candidate in candidates:
            if token == candidate:
                return True
            if len(token) >= 4 and len(candidate) >= 4:
                if token.startswith(candidate[:4]) or candidate.startswith(token[:4]):
                    return True
        return False

    def find_faq_answer(self, question_text: str, language: str = "uk") -> Optional[str]:
        match = nlu_matcher.match_service(question_text, self._nlu_faq_profiles)
        if match is None:
            return None
        faq_items = self.get_faq()
        try:
            index = int(match.service_id)
        except ValueError:
            return None
        if index < 0 or index >= len(faq_items):
            return None
        item = faq_items[index]
        return (
            item.get("answer_uk")
            if language == "uk"
            else item.get("answer_en") or item.get("answer_uk")
        )

    def find_contextual_service_followup(
        self,
        text: str,
        *,
        remembered_service_id: str,
        resolved_service: dict[str, Any] | None = None,
        language: str = "uk",
    ) -> Optional[dict[str, Any]]:
        service = self.get_service_by_id(remembered_service_id)
        if service is None:
            return None

        normalized = self._normalize_question_for_match(text)
        is_relation_followup = self._looks_like_contextual_relation_followup(normalized)
        if (
            not normalized
            or (
                not self._looks_like_short_service_followup(normalized)
                and not is_relation_followup
            )
        ):
            return None
        if self._is_explicit_service_switch(normalized, service, resolved_service):
            return None

        consultation_service = self._contextual_consultation_service(service, normalized)
        if consultation_service is not None:
            if self._looks_like_price_query(normalized):
                return {"kind": "price", "service": consultation_service}
            return {"kind": "consultation_summary", "service": consultation_service}

        if (
            resolved_service is not None
            and resolved_service.get("id") != service.get("id")
            and (
                self._looks_like_price_query(normalized)
                or "дешевш" in normalized
                or "дорожч" in normalized
            )
        ):
            # The message explicitly names a different service while
            # comparing prices (e.g. "а елайнери дешевші?" right after a
            # брекети price answer) -- answer with THAT service's own
            # grounded price, not the previously-discussed one's price or
            # summary again. Checked only after the specialty-consultation
            # redirect above, so a generic "консультація" mention while in
            # a specialty context still redirects there first instead of
            # being treated as "a different service to compare against."
            if "дешевш" in normalized or "дорожч" in normalized:
                comparison_answer = self._build_price_comparison_reply(resolved_service, service)
                if comparison_answer is not None:
                    return {"kind": "faq", "answer": comparison_answer, "service": resolved_service}
            price_options = self._matching_price_options(resolved_service, normalized)
            if price_options:
                return {"kind": "price_options", "price_options": price_options, "service": resolved_service}
            return {"kind": "price", "service": resolved_service}

        if self._looks_like_price_query(normalized) and self._looks_like_total_price_followup(normalized):
            return {"kind": "price", "service": service}

        if self._should_try_contextual_faq_before_price(normalized):
            faq_answer = self._find_contextual_faq_answer(service, normalized, language)
            if faq_answer is not None:
                return {"kind": "faq", "answer": faq_answer, "service": service}

        if self._looks_like_contextual_option_advice_question(normalized):
            price_options = self._valid_price_options(service)
            if price_options:
                return {"kind": "option_advice", "price_options": price_options, "service": service}

        if self._looks_like_contextual_option_list_question(normalized):
            price_options = self._valid_price_options(service)
            if price_options:
                return {"kind": "price_options", "price_options": price_options, "service": service}

        if self._looks_like_price_query(normalized):
            price_options = self._matching_price_options(service, normalized)
            if price_options:
                return {"kind": "price_options", "price_options": price_options, "service": service}
            return {"kind": "price", "service": service}

        faq_answer = self._find_contextual_faq_answer(service, normalized, language)
        if faq_answer is not None:
            return {"kind": "faq", "answer": faq_answer, "service": service}

        if self._message_matches_service_text(normalized, service):
            return {"kind": "summary", "service": service}

        if self._looks_like_contextual_relation_followup(normalized):
            if self._looks_like_ungrounded_timing_feasibility_question(normalized, service):
                return None
            return {"kind": "summary", "service": service}

        return None

    def _looks_like_ungrounded_timing_feasibility_question(
        self,
        normalized: str,
        service: dict[str, Any],
    ) -> bool:
        query_tokens = self._question_tokens(normalized)
        service_terms = self._service_tokens(service, include_description=True)
        has_service_overlap = any(
            self._token_matches_any(token, service_terms)
            for token in query_tokens
        )
        if has_service_overlap:
            return False
        asks_feasibility = any(token.startswith(("можн", "встиг")) for token in query_tokens)
        mentions_timing_detail = any(
            token.startswith(("ден", "день", "дн", "раз", "візит", "визит"))
            for token in query_tokens
        )
        return asks_feasibility and mentions_timing_detail

    def _looks_like_short_service_followup(self, normalized: str) -> bool:
        compact = re.sub(r"^(?:а|і|и|тоді|то)\s+", "", normalized).strip()
        token_count = len(compact.split())
        if token_count <= 5:
            return True
        followup_markers = [
            "скільки",
            "коштує",
            "входить",
            "включ",
            "кращ",
            "порад",
            "під ключ",
            "з коронк",
            "точну ціну",
            "точна ціна",
            "обточ",
            "мікроскоп",
            "носити",
            "можна поставити",
            "окрем",
            "разом",
            "замість",
            "процес",
            "видален",
            "пів року",
            "немає зуб",
            "немає одного зуб",
            "передні зуб",
            "за щелеп",
            "верхн",
            "нижн",
            "всі зуб",
            "вони мені",
            "перед ним",
            "перед цим",
            "перед встановлен",
            "спочатку треба",
            "прийти до ортодонт",
            "не повністю",
            "виліз",
            "проріз",
        ]
        return token_count <= 8 and any(marker in compact for marker in followup_markers)

    def _looks_like_contextual_option_list_question(self, normalized: str) -> bool:
        compact = re.sub(r"^(?:а|і|и|тоді|то)\s+", "", normalized).strip()
        return bool(
            re.search(r"\b(?:які|який|яка)\b", compact)
            and any(marker in compact for marker in ["є", "саме", "варіант"])
        )

    def _looks_like_contextual_option_advice_question(self, normalized: str) -> bool:
        compact = re.sub(r"^(?:а|і|и|тоді|то)\s+", "", normalized).strip()
        return any(marker in compact for marker in ["кращ", "порад", "що обрати", "що вибрати"])

    def _looks_like_total_price_followup(self, normalized: str) -> bool:
        return any(marker in normalized for marker in ["точн", "під ключ", "разом", "повн"])

    def _is_explicit_service_switch(
        self,
        normalized: str,
        remembered_service: dict[str, Any],
        resolved_service: dict[str, Any] | None,
    ) -> bool:
        if resolved_service is None or resolved_service.get("id") == remembered_service.get("id"):
            return False

        if self._looks_like_contextual_relation_followup(normalized):
            return False

        if (
            resolved_service.get("id") == "dental_consultation"
            and self._contextual_consultation_service(remembered_service, normalized) is not None
        ):
            return False

        return self._mentions_service_identity(normalized, resolved_service)

    def _looks_like_contextual_relation_followup(self, normalized: str) -> bool:
        relation_markers = [
            "входить",
            "включ",
            "окрем",
            "разом",
            "один",
            "одиниц",
            "під ключ",
            "з коронк",
            "за щелеп",
            "щелеп",
            "верхн",
            "нижн",
            "всі зуб",
            "кращ",
            "порад",
            "точну ціну",
            "точна ціна",
            "обточ",
            "мікроскоп",
            "носити",
            "замість",
            "альтернатив",
            "типу",
            "порівн",
            "дорожч",
            "дешевш",
            "скільки часу",
            "як довго",
            "процес",
            "давно немає",
            "немає зуб",
            "немає одного зуб",
            "зуб вже видален",
            "видален",
            "пів року",
            "передні зуб",
            "перед ним",
            "перед цим",
            "не повністю",
            "виліз",
            "проріз",
            "така консультац",
            "таку консультац",
            "такої консультац",
            "спочатку треба",
            "спочатку потріб",
            "перед встановленням",
            "прийти до ортодонт",
            "робите це",
            "робите таке",
            "ставите це",
            "ставите таке",
            "лікуєте це",
            "лікуєте таке",
        ]
        if any(marker in normalized for marker in relation_markers):
            return True
        return bool(
            re.search(r"\b(?:вони|він|вона|це)\s+мені\s+підійд", normalized)
            or re.search(r"\bмені\s+.*\bпідійд", normalized)
        )

    def _contextual_consultation_service(
        self,
        service: dict[str, Any],
        normalized: str,
    ) -> Optional[dict[str, Any]]:
        has_consultation_reference = bool(
            re.search(r"\bконсультац\w*\b", normalized)
            or re.search(r"\bприйти\s+до\s+ортодонт", normalized)
            or "перед встановлен" in normalized
        )
        if not has_consultation_reference:
            return None
        compact = re.sub(r"^(?:а|і|и|то|тоді)\s+", "", normalized).strip()
        is_bare_consultation_followup = bool(
            re.fullmatch(r"(?:на\s+)?консультац\w*\??", compact)
        )
        if not is_bare_consultation_followup and not any(
            marker in normalized
            for marker in ["така", "таку", "цей", "цю", "по цій", "перед", "спочатку", "встановлен"]
        ):
            return None
        booking_service_id = service.get("booking_service_id")
        if not booking_service_id or booking_service_id == service.get("id"):
            return None
        return self.get_service_by_id(str(booking_service_id))

    def _mentions_service_identity(self, normalized: str, service: dict[str, Any]) -> bool:
        parts = [service.get("name"), *(service.get("aliases") or [])]
        for part in parts:
            candidate = self._normalize_question_for_match(str(part or ""))
            if not candidate:
                continue
            if len(candidate.split()) > 1 and candidate in normalized:
                return True
            if len(candidate.split()) == 1 and re.search(rf"\b{re.escape(candidate)}\w*\b", normalized):
                return True
        return False

    def _should_try_contextual_faq_before_price(self, normalized: str) -> bool:
        return any(
            marker in normalized
            for marker in [
                "носити",
                "візит",
                "входить",
                "включ",
                "окрем",
                "разом",
                "кращ",
                "порад",
                "під ключ",
                "обточ",
                "щелеп",
                "верхн",
                "нижн",
                "всі зуб",
                "передні зуб",
                "підійд",
                "видален",
            ]
        )

    def _looks_like_price_query(self, normalized: str) -> bool:
        if any(marker in normalized for marker in ["скільки часу", "як довго", "скільки трива"]):
            return False
        return any(
            marker in normalized
            for marker in [
                "скільки",
                "сколько",
                "скок",
                "how much",
                "коштує",
                "коштуе",
                "ціна",
                "ціну",
                "ціни",
                "вартість",
                "прайс",
                "price",
                "cost",
                "дешевше",
                "дешевший",
                "дешевша",
                "дешевші",
                "дорожче",
                "дорожчий",
                "дорожча",
                "дорожчі",
                "недорого",
            ]
        )

    def _matching_price_options(self, service: dict[str, Any], normalized: str) -> list[dict[str, Any]]:
        price_options = self._valid_price_options(service)

        query_tokens = self._service_query_tokens(normalized)
        matches = []
        for option in price_options:
            label_tokens = self._service_query_tokens(str(option["label"]))
            if any(self._token_matches_any(query_token, label_tokens) for query_token in query_tokens):
                matches.append(option)
        return matches

    def _valid_price_options(self, service: dict[str, Any]) -> list[dict[str, Any]]:
        price_options = service.get("price_options")
        if not isinstance(price_options, list):
            return []
        return [
            option
            for option in price_options
            if isinstance(option, dict) and option.get("label") and option.get("price_note")
        ]

    def _representative_priced_entry(self, service: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The single price-bearing entry (a price_option, or the service
        itself for a flat price_note) used to represent a service in a
        comparison. For a service with multiple price_options, the first
        listed one is used -- a fixed, non-numeric convention (no amount
        comparison), matching the order the KB already lists them in."""
        price_options = self._valid_price_options(service)
        if price_options:
            return price_options[0]
        if service.get("price_note"):
            return service
        return None

    def _price_display_text(self, entry: dict[str, Any]) -> Optional[str]:
        """The entry's own already-existing price text, used verbatim --
        never reformatted or amount-parsed. A price_option is shown as its
        label paired with its own price_note; a flat service-level
        price_note (already a full grounded sentence) is shown as-is."""
        price_note = entry.get("price_note")
        if not price_note:
            return None
        label = entry.get("label")
        if label:
            return f"{label} — {price_note}"
        return str(price_note)

    def _build_price_comparison_reply(
        self,
        resolved_service: dict[str, Any],
        remembered_service: dict[str, Any],
    ) -> Optional[str]:
        """Answers a genuine "X дешевше/дорожче?" comparison between the
        service named in the current message and the service already
        being discussed, using structured price_unit ONLY to determine
        whether the two are directly comparable -- never to compute or
        compare a numeric verdict. When the units differ, states plainly
        that a direct comparison isn't valid and shows both sides' own
        grounded price text. When either unit is missing, or the units
        match, returns None so the safe existing (unit-less) behavior
        applies."""
        resolved_entry = self._representative_priced_entry(resolved_service)
        remembered_entry = self._representative_priced_entry(remembered_service)
        if resolved_entry is None or remembered_entry is None:
            return None

        resolved_unit = resolved_entry.get("price_unit")
        remembered_unit = remembered_entry.get("price_unit")
        if not resolved_unit or not remembered_unit:
            return None
        if resolved_unit == remembered_unit:
            return None

        remembered_text = self._price_display_text(remembered_entry)
        resolved_text = self._price_display_text(resolved_entry)
        if remembered_text is None or resolved_text is None:
            return None
        if not remembered_text.endswith((".", "!", "?")):
            remembered_text = f"{remembered_text}."

        return f"Прямо порівняти ці ціни некоректно: {remembered_text} {resolved_text}"

    # Words too generic across the whole dental domain to count as proof
    # that an FAQ item is actually *about* a given service, for the
    # purpose of _find_contextual_faq_answer's own service_overlap check
    # below -- "для" is a bare preposition, and "зуб"/"зуби"/etc. and
    # "щелеп*" appear in nearly every service's aliases regardless of what
    # the FAQ is actually asking about (e.g. "капи для вирівнювання" for
    # aligners, or "кт щелепи" for X-ray diagnostics, neither of which
    # makes the braces-specific "is the price per jaw?" FAQ relevant to
    # that service). The "зуб*" portion mirrors an existing, separate
    # generic-word list already used by _contextual_faq_detail_tokens for
    # a related purpose; that list is left untouched here to avoid
    # changing its own, already-working behavior.
    _GENERIC_DENTAL_OVERLAP_TOKENS = frozenset(
        {
            "можна",
            "можн",
            "треба",
            "треб",
            "потрібно",
            "потріб",
            "тільки",
            "саме",
            "зуб",
            "зуби",
            "зуба",
            "зубів",
            "процедура",
            "процедури",
            "для",
            "щелеп",
            "щелепи",
            "щелепу",
        }
    )

    def _find_contextual_faq_answer(
        self,
        service: dict[str, Any],
        normalized: str,
        language: str,
    ) -> Optional[str]:
        service_terms = self._service_tokens(service, include_description=False)
        query_tokens = self._question_tokens(normalized)
        best_answer = None
        best_score = 0

        for item in self.get_faq():
            question = self._normalize_question_for_match(str(item.get("question", "")))
            question_tokens = self._question_tokens(question)
            if not question_tokens:
                continue

            query_overlap = sum(1 for token in query_tokens if self._token_matches_any(token, question_tokens))
            service_overlap = sum(
                1
                for token in question_tokens
                if token not in self._GENERIC_DENTAL_OVERLAP_TOKENS
                and self._token_matches_any(token, service_terms)
            )
            detail_tokens = self._contextual_faq_detail_tokens(question_tokens, service_terms)
            detail_overlap = sum(
                1 for token in query_tokens if self._token_matches_any(token, detail_tokens)
            )
            if (
                service_overlap
                and query_overlap >= max(1, min(2, len(query_tokens)))
                and (not detail_tokens or detail_overlap > 0)
            ):
                answer = (
                    item.get("answer_uk")
                    if language == "uk"
                    else item.get("answer_en") or item.get("answer_uk")
                )
                score = service_overlap * 10 + query_overlap * 2 + detail_overlap * 5 - len(question_tokens)
                if score > best_score:
                    best_score = score
                    best_answer = answer

        return best_answer

    def _contextual_faq_detail_tokens(
        self,
        question_tokens: set[str],
        service_terms: set[str],
    ) -> set[str]:
        generic_tokens = {
            "можна",
            "можн",
            "треба",
            "треб",
            "потрібно",
            "потріб",
            "тільки",
            "саме",
            "зуб",
            "зуби",
            "зуба",
            "зубів",
            "процедура",
            "процедури",
        }
        return {
            token
            for token in question_tokens
            if token not in generic_tokens
            and not self._token_matches_any(token, service_terms)
        }

    def _message_matches_service_text(self, normalized: str, service: dict[str, Any]) -> bool:
        query_tokens = self._service_query_tokens(normalized)
        if not query_tokens:
            return False
        service_tokens = self._service_tokens(service, include_description=True)
        return any(self._token_matches_any(token, service_tokens) for token in query_tokens)

    def get_objections(self) -> list[dict[str, Any]]:
        return self.data.get("objections", [])

    def get_objection_by_key(self, key: str, language: str = "uk") -> Optional[str]:
        for item in self.get_objections():
            if item.get("key") == key:
                return item.get("answer_uk") if language == "uk" else item.get("answer_en")
        return None

    def get_constraints(self) -> dict[str, Any]:
        return self.data.get("constraints", {})
