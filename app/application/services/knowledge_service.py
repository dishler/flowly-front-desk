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

    def find_service(self, text: str) -> Optional[dict[str, Any]]:
        query_tokens = self._service_query_tokens(text)
        if not query_tokens:
            return None

        best_service = None
        best_score = 0
        for service in self.get_services():
            service_tokens = self._service_tokens(service)
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

    def _service_tokens(self, service: dict[str, Any]) -> set[str]:
        parts = [
            str(service.get("id") or ""),
            str(service.get("name") or ""),
            str(service.get("description") or ""),
        ]
        aliases = service.get("aliases")
        if isinstance(aliases, list):
            parts.extend(str(alias) for alias in aliases if alias)
        return self._service_query_tokens(" ".join(parts))

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

    def get_objections(self) -> list[dict[str, Any]]:
        return self.data.get("objections", [])

    def get_objection_by_key(self, key: str, language: str = "uk") -> Optional[str]:
        for item in self.get_objections():
            if item.get("key") == key:
                return item.get("answer_uk") if language == "uk" else item.get("answer_en")
        return None

    def get_constraints(self) -> dict[str, Any]:
        return self.data.get("constraints", {})
