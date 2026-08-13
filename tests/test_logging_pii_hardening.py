from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace

import httpx

from app.api.routes import meta_webhook
from app.application.dto.normalized_message import NormalizedMessage
from app.application.services.booking_service import BookingService
from app.application.services.intent_service import IntentService
from app.application.services.language_service import LanguageService
from app.application.services.memory_service import MemoryService
from app.application.services.message_processor import MessageProcessor
from app.application.services.reply_service import ReplyService
from app.domain.enums import BookingState
from app.infrastructure.google.calendar_client import GoogleCalendarClient


SECRET_MESSAGE = "private-message-SECRET123"
SECRET_TRANSCRIPT = "private-transcript-SECRET456"
SECRET_EMAIL = "private@example.test"
SECRET_PHONE = "+380991234567"
SECRET_MEDIA_URL = "https://media.example/SECRET_TOKEN"
SECRET_ACCESS_TOKEN = "SECRET_ACCESS_TOKEN"
SECRET_EVENT_LINK = "calendar-event-secret-link"
SECRET_REPLY = "assistant-reply-SECRET789"


def _logged_text(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


class FakeRequest:
    def __init__(self, payload: dict, processor) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {}
        self.app = SimpleNamespace(state=SimpleNamespace(message_processor=processor))

    async def body(self) -> bytes:
        return self._body


class TranscriptProcessor:
    async def process(self, message: NormalizedMessage) -> dict:
        message.user_message = SECRET_TRANSCRIPT
        return {"intent": "ok", "reply_text": "safe reply"}


async def test_meta_webhook_logs_do_not_expose_payload_message_transcript_or_media_url(
    caplog,
    monkeypatch,
):
    payload = {
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "sender-1"},
                        "recipient": {"id": "recipient-1"},
                        "message": {
                            "mid": "mid-1",
                            "text": SECRET_MESSAGE,
                            "attachments": [
                                {"payload": {"url": SECRET_MEDIA_URL}},
                            ],
                        },
                    }
                ]
            }
        ]
    }
    monkeypatch.setattr(meta_webhook, "_verify_meta_signature", lambda **kwargs: True)

    caplog.set_level(logging.INFO, logger="app.api.routes.meta_webhook")

    await meta_webhook.receive_meta_webhook(FakeRequest(payload, TranscriptProcessor()))

    logs = _logged_text(caplog)
    assert "webhook_received" in logs
    assert "has_text=True" in logs
    assert "has_audio=True" in logs
    assert "transcription_present=True" in logs
    assert SECRET_MESSAGE not in logs
    assert SECRET_TRANSCRIPT not in logs
    assert SECRET_MEDIA_URL not in logs


def test_graph_media_failure_log_does_not_expose_access_token(caplog, monkeypatch):
    request = httpx.Request(
        "GET",
        f"https://graph.facebook.com/media?access_token={SECRET_ACCESS_TOKEN}",
    )

    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def get(self, url, params):
            raise httpx.RequestError(
                f"failed url={request.url}",
                request=request,
            )

    monkeypatch.setattr(meta_webhook.settings, "meta_page_access_token", SECRET_ACCESS_TOKEN)
    monkeypatch.setattr(meta_webhook.httpx, "Client", FailingClient)
    caplog.set_level(logging.WARNING, logger="app.api.routes.meta_webhook")

    assert meta_webhook.get_media_url("media-id") == ""

    logs = _logged_text(caplog)
    assert "RequestError" in logs
    assert SECRET_ACCESS_TOKEN not in logs


def test_intent_service_does_not_log_raw_user_text(caplog):
    caplog.set_level(logging.INFO, logger="app.application.services.intent_service")

    IntentService().detect_intent(SECRET_MESSAGE)

    logs = _logged_text(caplog)
    assert "Intent detected:" in logs
    assert SECRET_MESSAGE not in logs


class FailingCreateCalendarService:
    class ConfiguredClient:
        def is_configured(self) -> bool:
            return True

    google_calendar_client = ConfiguredClient()

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        return True

    def create_booking_event(self, *args, **kwargs):
        raise RuntimeError("calendar create failed")


def test_booking_create_failure_log_does_not_expose_pending_contact_data(caplog):
    booking_service = BookingService(
        calendar_service=FailingCreateCalendarService(),
        language_service=LanguageService(),
    )
    booking_service._save_booking_state(
        "user-1",
        state=BookingState.WAITING_FOR_CONTACT,
        language="uk",
        start_dt=datetime(2026, 4, 27, 12, 0),
        contact_email=SECRET_EMAIL,
        contact_phone=SECRET_PHONE,
        customer_name="Secret Name",
        context_summary=SECRET_MESSAGE,
    )
    caplog.set_level(logging.WARNING, logger="app.application.services.booking_service")

    result = booking_service.process_booking_message("user-1", "Secret Name " + SECRET_PHONE)

    assert result["status"] == "manual_followup"
    logs = _logged_text(caplog)
    assert "booking create_event failed" in logs
    assert "has_email=True" in logs
    assert "has_phone=True" in logs
    assert SECRET_EMAIL not in logs
    assert SECRET_PHONE not in logs
    assert "Secret Name" not in logs
    assert SECRET_MESSAGE not in logs


class DummyDedupService:
    def is_duplicate(self, message_mid: str) -> bool:
        return False

    def mark_processed(self, message_mid: str) -> None:
        return None


class DummyOutboundService:
    def send_reply(self, platform: str, recipient_id: str, text: str) -> dict:
        return {"sent": True}


class DummySpeechService:
    async def transcribe_audio(self, file_url: str) -> str:
        return ""


class NoopBookingService:
    def get_booking_state(self, sender_id: str) -> BookingState:
        return BookingState.NONE

    def has_confirmed_booking(self, sender_id: str) -> bool:
        return False

    def start_booking_flow(self, sender_id: str, message_text: str, source_channel=None):
        return None


def _message(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        platform="instagram",
        sender_id="user-1",
        recipient_id="bot-1",
        message_mid="mid-1",
        user_message=text,
    )


async def test_message_processor_logs_reply_metadata_not_reply_text(caplog, monkeypatch):
    memory_service = MemoryService()
    reply_service = ReplyService(
        ai_service=None,
        memory_service=memory_service,
        knowledge_service=None,
    )
    monkeypatch.setattr(reply_service, "generate_reply", lambda message, intent=None: SECRET_REPLY)
    monkeypatch.setattr(
        reply_service,
        "classify_question_level",
        lambda user_text, intent, history: ("basic", "safe_reason"),
    )
    processor = MessageProcessor(
        memory_service=memory_service,
        reply_service=reply_service,
        outbound_service=DummyOutboundService(),
        dedup_service=DummyDedupService(),
        intent_service=IntentService(),
        booking_service=NoopBookingService(),
        speech_service=DummySpeechService(),
    )
    caplog.set_level(logging.INFO, logger="app.application.services.message_processor")

    result = await processor.process(_message("unknown neutral question"))

    assert result["reply_text"] == SECRET_REPLY
    logs = _logged_text(caplog)
    assert "Reply before guard metadata" in logs
    assert "Reply after guard metadata" in logs
    assert SECRET_REPLY not in logs


def test_calendar_logs_do_not_expose_raw_response_busy_items_or_event_link(caplog):
    class FakeFreebusyQuery:
        def __init__(self) -> None:
            self.body = None

        def query(self, body):
            self.body = body
            return self

        def execute(self):
            return {
                "calendars": {
                    "calendar@example.test": {
                        "busy": [
                            {
                                "start": "2026-04-27T12:00:00+03:00",
                                "end": "2026-04-27T12:30:00+03:00",
                                "secret": SECRET_MEDIA_URL,
                            }
                        ]
                    }
                },
                "secret": SECRET_MESSAGE,
            }

    class FakeEvents:
        def insert(self, calendarId, body, sendUpdates):
            return self

        def execute(self):
            return {
                "id": "event-id-1",
                "htmlLink": SECRET_EVENT_LINK,
                "status": "confirmed",
            }

    class FakeService:
        def freebusy(self):
            return FakeFreebusyQuery()

        def events(self):
            return FakeEvents()

    client = GoogleCalendarClient()
    client.enabled = True
    client.calendar_id = "calendar@example.test"
    client.service_account_json = "{}"
    client._service = FakeService()
    caplog.set_level(logging.INFO, logger="app.infrastructure.google.calendar_client")

    client.get_busy_periods(
        datetime.fromisoformat("2026-04-27T12:00:00+03:00"),
        datetime.fromisoformat("2026-04-27T13:00:00+03:00"),
    )
    client.create_event(
        start_dt=datetime.fromisoformat("2026-04-27T14:00:00+03:00"),
        end_dt=datetime.fromisoformat("2026-04-27T14:30:00+03:00"),
        summary="Consultation call",
    )

    logs = _logged_text(caplog)
    assert "busy_count=1" in logs
    assert "calendar create_event success" in logs
    assert SECRET_MESSAGE not in logs
    assert SECRET_MEDIA_URL not in logs
    assert SECRET_EVENT_LINK not in logs
