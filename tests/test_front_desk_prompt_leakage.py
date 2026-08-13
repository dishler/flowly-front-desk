import json

from app.application.services.front_desk_config_service import FrontDeskConfigService
from app.infrastructure.openai.client import OpenAIClient


def _write_config(tmp_path, data: dict):
    path = tmp_path / "front_desk_config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_openai_default_prompt_is_generic_front_desk_not_flowly_sales():
    prompt = OpenAIClient()._build_default_system_prompt()

    assert "front desk assistant" in prompt
    assert "Flowly" not in prompt
    assert "Flowly Agency" not in prompt
    assert "200 USD" not in prompt
    assert "AI bots" not in prompt


def test_openai_prompt_uses_front_desk_config_identity_and_tone(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "business": {"name": "Smile Dental Clinic"},
            "assistant": {
                "supported_languages": ["uk", "en"],
                "tone": "calm dental receptionist",
                "default_language": "uk",
            },
            "booking": {
                "enabled": True,
                "appointment_label": "візит",
                "duration_minutes": 30,
                "required_contact_fields": ["name", "phone"],
            },
            "qualification": {"enabled": False, "questions": []},
            "safety": {
                "do_not_claim": ["medical diagnosis"],
                "unknown_fallback": "Уточніть, будь ласка.",
            },
            "handoff": {
                "rules": ["medical_emergency"],
                "reply": "Передам адміністратору.",
            },
        },
    )
    config_service = FrontDeskConfigService(str(path))

    prompt = OpenAIClient(front_desk_config_service=config_service)._build_default_system_prompt()

    assert "Smile Dental Clinic" in prompt
    assert "calm dental receptionist" in prompt
    assert "medical diagnosis" in prompt
    assert "medical_emergency" in prompt
    assert "Flowly" not in prompt
    assert "200 USD" not in prompt
