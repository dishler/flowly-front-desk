import json
from pathlib import Path
from typing import Any


class FrontDeskConfigError(RuntimeError):
    pass


class FrontDeskConfigService:
    DEFAULT_PATH = "app/data/front_desk_config.json"

    def __init__(self, file_path: str = DEFAULT_PATH) -> None:
        self.file_path = Path(file_path)
        self.data = self._load()
        self._validate()

    def _load(self) -> dict[str, Any]:
        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError as exc:
            raise FrontDeskConfigError(
                f"Front desk config file not found: {self.file_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise FrontDeskConfigError(
                f"Front desk config file is invalid JSON: {self.file_path}"
            ) from exc

        if not isinstance(data, dict):
            raise FrontDeskConfigError("Front desk config must be a JSON object")
        return data

    def _validate(self) -> None:
        business = self.get_business()
        assistant = self.get_assistant()
        booking = self.get_booking()
        qualification = self.get_qualification()
        safety = self.get_safety()
        handoff = self.get_handoff()

        errors: list[str] = []
        knowledge_only_fields = {
            "services",
            "pricing",
            "faq",
            "location",
            "working_hours",
            "contacts",
            "policies",
        }
        duplicated_fields = sorted(knowledge_only_fields.intersection(self.data))
        if duplicated_fields:
            errors.append(
                "knowledge-base fields do not belong in front desk config: "
                + ", ".join(duplicated_fields)
            )

        if not str(business.get("name", "")).strip():
            errors.append("business.name is required")

        supported_languages = assistant.get("supported_languages")
        if not isinstance(supported_languages, list) or not supported_languages:
            errors.append("assistant.supported_languages must be a non-empty list")

        default_language = str(assistant.get("default_language", "")).strip()
        if not default_language:
            errors.append("assistant.default_language is required")
        elif isinstance(supported_languages, list) and default_language not in supported_languages:
            errors.append("assistant.default_language must be in assistant.supported_languages")

        if not str(assistant.get("tone", "")).strip():
            errors.append("assistant.tone is required")

        if not isinstance(booking.get("enabled"), bool):
            errors.append("booking.enabled must be boolean")

        if not str(booking.get("appointment_label", "")).strip():
            errors.append("booking.appointment_label is required")

        duration = booking.get("duration_minutes")
        if not isinstance(duration, int) or duration <= 0:
            errors.append("booking.duration_minutes must be a positive integer")

        required_contact_fields = booking.get("required_contact_fields")
        allowed_contact_fields = {"name", "phone", "email"}
        if not isinstance(required_contact_fields, list):
            errors.append("booking.required_contact_fields must be a list")
        elif any(field not in allowed_contact_fields for field in required_contact_fields):
            errors.append("booking.required_contact_fields may contain only name, phone, email")

        if not isinstance(qualification.get("enabled"), bool):
            errors.append("qualification.enabled must be boolean")
        if not isinstance(qualification.get("questions"), list):
            errors.append("qualification.questions must be a list")

        if not isinstance(safety.get("do_not_claim", []), list):
            errors.append("safety.do_not_claim must be a list")
        if not str(safety.get("unknown_fallback", "")).strip():
            errors.append("safety.unknown_fallback is required")

        if not isinstance(handoff.get("rules", []), list):
            errors.append("handoff.rules must be a list")
        if not str(handoff.get("reply", "")).strip():
            errors.append("handoff.reply is required")

        if errors:
            raise FrontDeskConfigError(
                "Invalid front desk config: " + "; ".join(errors)
            )

    def get_business(self) -> dict[str, Any]:
        return self.data.get("business", {})

    def get_assistant(self) -> dict[str, Any]:
        return self.data.get("assistant", {})

    def get_booking(self) -> dict[str, Any]:
        return self.data.get("booking", {})

    def get_qualification(self) -> dict[str, Any]:
        return self.data.get("qualification", {"enabled": False, "questions": []})

    def get_safety(self) -> dict[str, Any]:
        return self.data.get("safety", {})

    def get_handoff(self) -> dict[str, Any]:
        return self.data.get("handoff", {})
