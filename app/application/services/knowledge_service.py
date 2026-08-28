from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from app.core.config import get_settings


class KnowledgeService:
    def __init__(self, file_path: str | None = None) -> None:
        if file_path is None:
            file_path = get_settings().knowledge_base_path
        self.file_path = Path(file_path)
        self.data = self._load()

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
        return self._find_service(text, include_description=True)

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
                r"\b(?:скільки|коштує|вартість|ціна|ціну|прайс|записа\w*|запиш\w*|прийом)\b",
                normalized,
            )
        )
        if not (dental_qualified or bare_dental_reference or dental_price_or_booking_reference):
            return None
        return self.get_service_by_id("dental_consultation")

    def find_confident_service(self, text: str) -> Optional[dict[str, Any]]:
        normalized = self._normalize_question_for_match(text)
        query_tokens = self._service_query_tokens(normalized)
        if not query_tokens:
            return None

        pediatric_service = self._find_bounded_pediatric_service(normalized)
        if pediatric_service is not None:
            return pediatric_service

        best_service = None
        best_score = 0
        best_specificity = 0
        for service in self.get_services():
            score = 0
            specificity = 0
            matched_query_tokens: set[str] = set()
            aliases = service.get("aliases")
            if not isinstance(aliases, list):
                continue

            for alias in aliases:
                alias_text = str(alias or "").strip()
                if not alias_text:
                    continue
                alias_normalized = self._normalize_question_for_match(alias_text)
                alias_tokens = self._service_query_tokens(alias_normalized)
                if not alias_tokens:
                    continue

                if len(alias_tokens) > 1:
                    if alias_normalized in normalized:
                        score += len(alias_tokens)
                        specificity = max(specificity, len(alias_tokens))
                    continue

                alias_token = next(iter(alias_tokens))
                matched_query_token = next(
                    (
                        token
                        for token in query_tokens
                        if self._token_matches_any(alias_token, {token})
                    ),
                    None,
                )
                if matched_query_token is not None and matched_query_token not in matched_query_tokens:
                    matched_query_tokens.add(matched_query_token)
                    score += 1
                    specificity = max(specificity, 1)

            if score > best_score or (score == best_score and specificity > best_specificity):
                best_score = score
                best_specificity = specificity
                best_service = service

        return best_service if best_score > 0 else None

    def _find_bounded_pediatric_service(self, normalized: str) -> Optional[dict[str, Any]]:
        has_pediatric_context = bool(
            re.search(
                r"\b(?:дит(?:ині|ину|ини|яча|ячу|ячий|ячі|ячого|ячому|ячою)|дітям)\b",
                normalized,
            )
            or "для дитини" in normalized
            or "для дітей" in normalized
            or re.search(r"\bмолочн\w*\s+зуб", normalized)
        )
        if not has_pediatric_context:
            return None

        if re.search(r"\b(?:карієс\w*|пломб\w*)\b", normalized):
            return self.get_service_by_id("pediatric_caries_treatment")

        if re.search(r"\b(?:чистк\w*|гігієн\w*)\b", normalized):
            return self.get_service_by_id("pediatric_cleaning")

        if re.search(r"\b(?:записа\w*|запис\b|прийом\b|консультац\w*)\b", normalized):
            return self.get_service_by_id("pediatric_dentistry")

        return None

    def _find_service(self, text: str, *, include_description: bool) -> Optional[dict[str, Any]]:
        query_tokens = self._service_query_tokens(text)
        if not query_tokens:
            return None

        best_service = None
        best_score = 0
        for service in self.get_services():
            service_tokens = self._service_tokens(
                service,
                include_description=include_description,
            )
            if not service_tokens:
                continue
            score = sum(1 for token in query_tokens if self._token_matches_any(token, service_tokens))
            if score > best_score:
                best_score = score
                best_service = service

        return best_service if best_score > 0 else None

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
        """Returns extra service-detail tokens that are not grounded in the
        matched service's aliases/name/description/options. This catches
        "known broader topic + unknown variant/brand" without blacklisting
        particular procedures.

        Ordinary conversational/reporting verbs and generic booking-action
        words around the service mention ("Скажіть, ви ставите брекети?",
        "можна поставити брекети?", "скільки коштують брекети?") must not
        themselves count as an unrecognized "detail" -- they name no
        service variant at all. Matched by STEM (substring), not exact
        word-form, so this stays robust to conjugation and Ukrainian
        verb prefixation (ставите/ставити/поставити, коштує/коштують)
        without having to enumerate every inflected form by hand.
        """
        query_tokens = {
            token
            for token in self._service_query_tokens(text)
            if token not in self._service_action_exact_tokens()
            and not any(stem in token for stem in self._service_action_stems())
        }
        if not query_tokens:
            return set()
        service_tokens = self._service_tokens(service, include_description=True)
        unknown_tokens = {
            token
            for token in query_tokens
            if not self._token_matches_any(token, service_tokens)
        }
        return unknown_tokens

    def _service_action_exact_tokens(self) -> set[str]:
        return {
            "ви",
            "вас",
            "які",
            "яка",
            "який",
            "чи",
            "що",
            "ні",
            "тоді",
            "для",
            "день",
            "do",
            "you",
            "offer",
            "provide",
            "book",
            "want",
        }

    def _service_action_stems(self) -> set[str]:
        return {
            "вход",
            "роб",
            "став",
            "ліку",
            "запис",
            "запиш",
            "хоч",
            "можн",
            "потрібн",
            "треба",
            "кращ",
            "рок",
            "люд",
            "діт",
            "дитин",
            "дорослим",
            "дорослих",
            "прив",
            "добр",
            "скаж",
            "підкаж",
            "розкаж",
            "поясн",
            "кошт",
            "цікав",
        }

    def _token_matches_any(self, token: str, candidates: set[str]) -> bool:
        for candidate in candidates:
            if token == candidate:
                return True
            if len(token) >= 4 and len(candidate) >= 4:
                if token.startswith(candidate[:4]) or candidate.startswith(token[:4]):
                    return True
        return False

    def find_faq_answer(self, question_text: str, language: str = "uk") -> Optional[str]:
        normalized = self._normalize_question_for_match(question_text)
        query_tokens = self._question_tokens(normalized)
        for item in self.get_faq():
            question = self._normalize_question_for_match(str(item.get("question", "")))
            question_tokens = self._question_tokens(question)
            has_close_token_match = (
                bool(query_tokens)
                and bool(question_tokens)
                and question_tokens.issubset(query_tokens)
            )
            if question and (question in normalized or normalized in question or has_close_token_match):
                return (
                    item.get("answer_uk")
                    if language == "uk"
                    else item.get("answer_en") or item.get("answer_uk")
                )
        return None

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
        if not normalized or not self._looks_like_short_service_followup(normalized):
            return None
        if self._is_explicit_service_switch(normalized, service, resolved_service):
            return None

        if self._should_try_contextual_faq_before_price(normalized):
            faq_answer = self._find_contextual_faq_answer(service, normalized, language)
            if faq_answer is not None:
                return {"kind": "faq", "answer": faq_answer, "service": service}

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

        return None

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
            "обточ",
            "мікроскоп",
            "носити",
            "можна поставити",
        ]
        return token_count <= 8 and any(marker in compact for marker in followup_markers)

    def _is_explicit_service_switch(
        self,
        normalized: str,
        remembered_service: dict[str, Any],
        resolved_service: dict[str, Any] | None,
    ) -> bool:
        if resolved_service is None or resolved_service.get("id") == remembered_service.get("id"):
            return False

        relation_markers = [
            "входить",
            "включ",
            "один",
            "одиниц",
            "кращ",
            "обточ",
            "мікроскоп",
            "носити",
            "давно немає",
        ]
        if any(marker in normalized for marker in relation_markers):
            return False

        return self._mentions_service_identity(normalized, resolved_service)

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
        return any(marker in normalized for marker in ["носити", "візит", "входить", "включ", "кращ", "обточ"])

    def _looks_like_price_query(self, normalized: str) -> bool:
        return any(
            marker in normalized
            for marker in ["скільки", "сколько", "how much", "коштує", "ціна", "ціну", "ціни", "вартість", "прайс", "price", "cost"]
        )

    def _matching_price_options(self, service: dict[str, Any], normalized: str) -> list[dict[str, Any]]:
        price_options = service.get("price_options")
        if not isinstance(price_options, list):
            return []

        query_tokens = self._service_query_tokens(normalized)
        matches = []
        for option in price_options:
            if not isinstance(option, dict) or not option.get("label") or not option.get("price_note"):
                continue
            label_tokens = self._service_query_tokens(str(option["label"]))
            if any(self._token_matches_any(query_token, label_tokens) for query_token in query_tokens):
                matches.append(option)
        return matches

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
            service_overlap = sum(1 for token in question_tokens if self._token_matches_any(token, service_terms))
            if service_overlap and query_overlap >= max(1, min(2, len(query_tokens))):
                answer = (
                    item.get("answer_uk")
                    if language == "uk"
                    else item.get("answer_en") or item.get("answer_uk")
                )
                score = service_overlap * 10 + query_overlap * 2 - len(question_tokens)
                if score > best_score:
                    best_score = score
                    best_answer = answer

        return best_answer

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
