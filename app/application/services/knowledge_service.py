from __future__ import annotations

import json
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

    def get_pricing(self) -> dict[str, Any]:
        return self.data.get("pricing", {})

    def get_consultation(self) -> dict[str, Any]:
        return self.data.get("consultation", {})

    def get_faq(self) -> list[dict[str, Any]]:
        return self.data.get("faq", [])

    def get_all_faq(self) -> list[dict[str, Any]]:
        return self.get_faq()

    def find_faq_answer(self, question_text: str, language: str = "uk") -> Optional[str]:
        normalized = question_text.strip().lower()
        for item in self.get_faq():
            question = str(item.get("question", "")).strip().lower()
            if question and question in normalized:
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
