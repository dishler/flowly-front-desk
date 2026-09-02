from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DEBUG", "false")

from app.api.routes import telegram_webhook
from app.application.services.booking_service import BookingService
from app.application.services.calendar_service import CalendarService
from app.application.services.dedup_service import DedupService
from app.application.services.front_desk_config_service import FrontDeskConfigService
from app.application.services.intent_service import IntentService
from app.application.services.knowledge_service import KnowledgeService
from app.application.services.language_service import LanguageService
from app.application.services.memory_service import MemoryService
from app.application.services.message_processor import MessageProcessor
from app.application.services.outbound_service import OutboundService
from app.application.services.reply_service import ReplyService


class RecordingAIService:
    def __init__(self) -> None:
        self.calls = []

    def try_generate_reply(
        self,
        user_message: str,
        history=None,
        grounding_context=None,
        system_instruction=None,
    ) -> dict:
        self.calls.append(
            {
                "user_message": user_message,
                "history": history,
                "grounding_context": grounding_context,
                "system_instruction": system_instruction,
            }
        )
        return {"reply_text": None}


class DummySpeechService:
    async def transcribe_audio(self, file_url: str) -> str:
        return ""


class RecordingCalendarService(CalendarService):
    def __init__(self) -> None:
        super().__init__()
        self.created_events = []
        self.deleted_events = []
        self.rescheduled_events = []
        self.availability_checks = []

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.availability_checks.append(
            {
                "start_dt": start_dt,
                "duration_minutes": duration_minutes,
            }
        )
        return True

    def create_booking_event(
        self,
        start_dt,
        duration_minutes: int = 30,
        summary: str = "",
        description: str = "",
        attendee_emails=None,
    ):
        self.created_events.append(
            {
                "start_dt": start_dt,
                "duration_minutes": duration_minutes,
                "summary": summary,
                "description": description,
                "attendee_emails": attendee_emails or [],
            }
        )
        raise AssertionError("FAQ pipeline test must not create calendar events")

    def delete_event(self, event_id: str) -> None:
        self.deleted_events.append(event_id)
        raise AssertionError("FAQ pipeline test must not delete calendar events")

    def reschedule_event(self, event_id: str, start_dt, duration_minutes: int = 30) -> None:
        self.rescheduled_events.append(
            {
                "event_id": event_id,
                "start_dt": start_dt,
                "duration_minutes": duration_minutes,
            }
        )
        raise AssertionError("FAQ pipeline test must not reschedule calendar events")


class RecordingMetaClient:
    def __init__(self) -> None:
        self.calls = []

    def send_text_message(self, platform: str, recipient_id: str, text: str) -> dict:
        self.calls.append(
            {
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
            }
        )
        return {"sent": False, "stub": False, "platform": platform}


class RecordingTelegramClient:
    def __init__(self) -> None:
        self.calls = []

    def send_text_message(self, recipient_id: str, text: str) -> dict:
        self.calls.append(
            {
                "recipient_id": recipient_id,
                "text": text,
            }
        )
        return {
            "sent": True,
            "stub": False,
            "platform": "telegram",
            "recipient_id": recipient_id,
            "text": text,
            "telegram_response": {"ok": True},
        }


def _telegram_payload(update_id: int = 123, chat_id: int = 789, text: str = "яка адреса?") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 456,
            "date": 1710000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _build_pipeline_app():
    memory_service = MemoryService()
    calendar_service = RecordingCalendarService()
    booking_service = BookingService(
        calendar_service=calendar_service,
        language_service=LanguageService(),
        front_desk_config_service=FrontDeskConfigService("tests/fixtures/front_desk_config.json"),
    )
    ai_service = RecordingAIService()
    reply_service = ReplyService(
        ai_service=ai_service,
        memory_service=memory_service,
        knowledge_service=KnowledgeService("tests/fixtures/dental_knowledge_base.json"),
        front_desk_config_service=FrontDeskConfigService("tests/fixtures/front_desk_config.json"),
    )
    meta_client = RecordingMetaClient()
    telegram_client = RecordingTelegramClient()
    outbound_service = OutboundService(
        meta_client=meta_client,
        telegram_client=telegram_client,
    )
    message_processor = MessageProcessor(
        memory_service=memory_service,
        reply_service=reply_service,
        outbound_service=outbound_service,
        dedup_service=DedupService(),
        intent_service=IntentService(),
        booking_service=booking_service,
        speech_service=DummySpeechService(),
    )

    app = FastAPI()
    app.state.message_processor = message_processor
    app.include_router(telegram_webhook.router, prefix="/webhooks")
    return app, meta_client, telegram_client, calendar_service


def test_telegram_webhook_uses_real_processor_and_telegram_outbound(monkeypatch):
    monkeypatch.setattr(telegram_webhook.settings, "environment", "production")
    monkeypatch.setattr(telegram_webhook.settings, "telegram_webhook_secret", "secret-token")
    app, meta_client, telegram_client, calendar_service = _build_pipeline_app()
    client = TestClient(app)

    response = client.post(
        "/webhooks/telegram",
        json=_telegram_payload(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["normalized_message"]["platform"] == "telegram"
    assert body["normalized_message"]["sender_id"] == "telegram:789"
    assert body["normalized_message"]["message_mid"] == "telegram:update:123"
    assert body["result"]["outbound_result"]["sent"] is True
    assert body["result"]["outbound_result"]["platform"] == "telegram"
    assert "Київ, Печерськ, вул. Тестова 10" in body["result"]["reply_text"]
    assert telegram_client.calls == [
        {
            "recipient_id": "telegram:789",
            "text": body["result"]["reply_text"],
        }
    ]
    assert meta_client.calls == []
    assert calendar_service.created_events == []
    assert calendar_service.deleted_events == []
    assert calendar_service.rescheduled_events == []
    assert calendar_service.availability_checks == []


def test_telegram_retry_same_update_id_is_deduped_before_second_outbound(monkeypatch):
    monkeypatch.setattr(telegram_webhook.settings, "environment", "production")
    monkeypatch.setattr(telegram_webhook.settings, "telegram_webhook_secret", "secret-token")
    app, meta_client, telegram_client, calendar_service = _build_pipeline_app()
    client = TestClient(app)
    headers = {"X-Telegram-Bot-Api-Secret-Token": "secret-token"}

    first = client.post("/webhooks/telegram", json=_telegram_payload(update_id=321), headers=headers)
    second = client.post("/webhooks/telegram", json=_telegram_payload(update_id=321), headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["normalized_message"]["message_mid"] == "telegram:update:321"
    assert first.json()["result"]["outbound_result"]["sent"] is True
    assert second.json()["result"]["intent"] == "duplicate_skipped"
    assert len(telegram_client.calls) == 1
    assert meta_client.calls == []
    assert calendar_service.created_events == []
    assert calendar_service.deleted_events == []
    assert calendar_service.rescheduled_events == []
    assert calendar_service.availability_checks == []
