import json

import pytest

from app.application.services.front_desk_config_service import (
    FrontDeskConfigError,
    FrontDeskConfigService,
)


def _valid_config() -> dict:
    return {
        "business": {
            "name": "Smile Dental Clinic",
        },
        "assistant": {
            "supported_languages": ["uk", "en"],
            "tone": "calm, concise, helpful front desk receptionist",
            "default_language": "uk",
        },
        "booking": {
            "enabled": True,
            "appointment_label": "візит",
            "duration_minutes": 30,
            "required_contact_fields": ["name", "phone"],
        },
        "qualification": {
            "enabled": False,
            "questions": [],
        },
        "safety": {
            "do_not_claim": ["medical diagnosis"],
            "unknown_fallback": "Уточніть, будь ласка, що саме вас цікавить?",
        },
        "handoff": {
            "rules": ["complex_request"],
            "reply": "Передам адміністратору, щоб уточнили деталі.",
        },
    }


def _write_config(tmp_path, data: dict):
    path = tmp_path / "front_desk_config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_front_desk_config_loads_valid_config(tmp_path):
    path = _write_config(tmp_path, _valid_config())

    service = FrontDeskConfigService(str(path))

    assert service.get_business()["name"] == "Smile Dental Clinic"
    assert service.get_assistant()["default_language"] == "uk"
    assert service.get_booking()["appointment_label"] == "візит"


def test_front_desk_config_rejects_missing_required_fields(tmp_path):
    data = _valid_config()
    data["business"]["name"] = ""
    path = _write_config(tmp_path, data)

    with pytest.raises(FrontDeskConfigError, match="business.name"):
        FrontDeskConfigService(str(path))


def test_front_desk_config_rejects_knowledge_base_facts(tmp_path):
    data = _valid_config()
    data["services"] = [{"name": "Cleaning", "price": "100"}]
    data["pricing"] = {"starting_from": "100"}
    data["faq"] = [{"question": "Hours?", "answer": "9-5"}]
    path = _write_config(tmp_path, data)

    with pytest.raises(FrontDeskConfigError, match="knowledge-base fields"):
        FrontDeskConfigService(str(path))


def test_default_front_desk_config_is_dental_demo_and_non_flowly():
    service = FrontDeskConfigService()
    serialized = json.dumps(service.data, ensure_ascii=False).lower()

    assert service.get_business()["name"] == "Smile Dental Clinic"
    assert service.get_booking()["appointment_label"] == "візит"
    assert "flowly" not in serialized
    assert "200" not in serialized
    assert "automation" not in serialized
