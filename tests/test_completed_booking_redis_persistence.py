from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.application.dto.normalized_message import NormalizedMessage
from app.application.services.booking_service import BookingService
from app.application.services.intent_service import IntentService
from app.application.services.language_service import LanguageService
from app.application.services.memory_service import MemoryService
from app.application.services.message_processor import MessageProcessor
from app.application.services.outbound_service import OutboundService
from app.application.services.redis_booking_state_service import RedisBookingStateService
from app.application.services.redis_memory_service import RedisMemoryService
from app.application.services.reply_service import ReplyService


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value
        self.ttls[key] = ttl

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    def get(self, key: str):
        return self.store.get(key)

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def delete(self, key: str) -> None:
        self.store.pop(key, None)
        self.ttls.pop(key, None)


class DummyDedupService:
    def __init__(self) -> None:
        self.processed = set()

    def is_duplicate(self, message_mid: str) -> bool:
        return message_mid in self.processed

    def mark_processed(self, message_mid: str) -> None:
        self.processed.add(message_mid)


class DummyOutboundService(OutboundService):
    def __init__(self) -> None:
        self.sent = []

    def send_reply(self, platform: str, recipient_id: str, text: str) -> dict:
        self.sent.append(
            {
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
            }
        )
        return {"sent": True}


class DummySpeechService:
    async def transcribe_audio(self, file_url: str) -> str:
        return ""


class RecordingCalendarService:
    google_calendar_client = None

    def __init__(self) -> None:
        self.deleted_event_ids = []
        self.rescheduled_events = []

    def delete_event(self, event_id: str) -> None:
        self.deleted_event_ids.append(event_id)

    def reschedule_event(self, event_id: str, start_dt, duration_minutes: int = 30) -> None:
        self.rescheduled_events.append(
            {
                "event_id": event_id,
                "start_dt": start_dt,
                "duration_minutes": duration_minutes,
            }
        )

    def get_available_slots(self, language: str):
        return ["завтра о 11:00", "завтра о 15:00"]

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        return True


class FailingDeleteCalendarService(RecordingCalendarService):
    def delete_event(self, event_id: str) -> None:
        raise RuntimeError("delete failed")


class FailingRescheduleCalendarService(RecordingCalendarService):
    def reschedule_event(self, event_id: str, start_dt, duration_minutes: int = 30) -> None:
        raise RuntimeError("reschedule failed")


class UnconfiguredCalendarService(RecordingCalendarService):
    class UnconfiguredClient:
        def is_configured(self) -> bool:
            return False

    google_calendar_client = UnconfiguredClient()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


def _state_service(fake_redis: FakeRedis) -> RedisBookingStateService:
    return RedisBookingStateService(
        fake_redis,
        ttl_seconds=3600,
        completed_ttl_seconds=2_592_000,
    )


def _booking_service(fake_redis: FakeRedis, calendar_service=None) -> BookingService:
    return BookingService(
        calendar_service=calendar_service or RecordingCalendarService(),
        language_service=LanguageService(),
        booking_state_service=_state_service(fake_redis),
    )


def _processor(
    booking_service: BookingService,
    memory_service: MemoryService | RedisMemoryService | None = None,
) -> MessageProcessor:
    memory_service = memory_service or MemoryService()
    return MessageProcessor(
        memory_service=memory_service,
        reply_service=ReplyService(
            ai_service=None,
            memory_service=memory_service,
            knowledge_service=None,
        ),
        outbound_service=DummyOutboundService(),
        dedup_service=DummyDedupService(),
        intent_service=IntentService(),
        booking_service=booking_service,
        speech_service=DummySpeechService(),
    )


def _message(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        platform="instagram",
        sender_id="user-1",
        recipient_id="bot-1",
        message_mid="",
        user_message=text,
    )


def _mark_completed(
    booking_service: BookingService,
    *,
    event_id: str = "calendar-event-123",
) -> None:
    booking_service._mark_booking_completed(
        "user-1",
        start_dt=datetime(2026, 4, 27, 12, 0),
        customer_name="Іван",
        email="client@example.com",
        phone="0987121328",
        calendar_event_id=event_id,
    )


def _completed_payload(fake_redis: FakeRedis) -> dict:
    raw = fake_redis.get("booking:completed:user-1")
    assert raw is not None
    return json.loads(raw)


def test_completed_booking_survives_new_booking_service_instance(fake_redis):
    _mark_completed(_booking_service(fake_redis))

    restarted = _booking_service(fake_redis)

    assert restarted.has_confirmed_booking("user-1")


def test_has_confirmed_booking_reads_persisted_completed_state_after_restart(fake_redis):
    first = _booking_service(fake_redis)
    _mark_completed(first)

    restarted = _booking_service(fake_redis)

    assert restarted.has_confirmed_booking("user-1") is True


def test_completed_booking_metadata_is_retrievable_after_restart(fake_redis):
    _mark_completed(_booking_service(fake_redis))

    restarted = _booking_service(fake_redis)
    completed = restarted._get_completed_booking("user-1")

    assert completed == {
        "start_dt": "2026-04-27T12:00:00+03:00",
        "customer_name": "Іван",
        "email": "client@example.com",
        "phone": "0987121328",
        "calendar_event_id": "calendar-event-123",
    }


async def test_status_flow_recognizes_confirmed_booking_after_restart(fake_redis):
    _mark_completed(_booking_service(fake_redis))
    processor = _processor(_booking_service(fake_redis))

    result = await processor.process(_message("поставив дзвінок?"))

    assert result["intent"] == "booking_status_confirmed"
    assert "дзвінок підтверджено" in result["reply_text"]
    assert "12:00" in result["reply_text"]


async def test_cancellation_after_restart_uses_persisted_calendar_event_id(fake_redis):
    _mark_completed(_booking_service(fake_redis), event_id="calendar-event-456")
    calendar_service = RecordingCalendarService()
    processor = _processor(_booking_service(fake_redis, calendar_service=calendar_service))

    result = await processor.process(_message("Скасуйте дзвінок"))

    assert result["intent"] == "booking_cancel"
    assert calendar_service.deleted_event_ids == ["calendar-event-456"]


async def test_successful_cancellation_clears_completed_redis_state(fake_redis):
    _mark_completed(_booking_service(fake_redis))
    processor = _processor(_booking_service(fake_redis))

    result = await processor.process(_message("Скасуйте дзвінок"))

    assert result["booking_result"]["status"] == "cancelled"
    assert fake_redis.exists("booking:completed:user-1") == 0


async def test_failed_calendar_deletion_preserves_completed_redis_state(fake_redis):
    _mark_completed(_booking_service(fake_redis))
    processor = _processor(
        _booking_service(fake_redis, calendar_service=FailingDeleteCalendarService())
    )

    result = await processor.process(_message("Скасуйте дзвінок"))

    assert result["booking_result"]["status"] == "cancel_handoff"
    assert fake_redis.exists("booking:completed:user-1") == 1


async def test_reschedule_after_restart_updates_persisted_start_dt(fake_redis):
    _mark_completed(_booking_service(fake_redis))
    calendar_service = RecordingCalendarService()
    processor = _processor(_booking_service(fake_redis, calendar_service=calendar_service))

    result = await processor.process(_message("післязавтра 15:00"))

    assert result["intent"] == "booking_reschedule"
    assert result["booking_result"]["status"] == "rescheduled"
    assert calendar_service.rescheduled_events
    assert calendar_service.rescheduled_events[0]["event_id"] == "calendar-event-123"
    assert _completed_payload(fake_redis)["start_dt"].endswith("T15:00:00+03:00")


async def test_fresh_booking_after_completed_state_does_not_reschedule_persisted_event(fake_redis):
    _mark_completed(_booking_service(fake_redis))
    original_start_dt = _completed_payload(fake_redis)["start_dt"]
    calendar_service = RecordingCalendarService()
    processor = _processor(_booking_service(fake_redis, calendar_service=calendar_service))

    result = await processor.process(_message("хочу записатись на чистку завтра о 12"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "перенесли" not in result["reply_text"]
    assert calendar_service.rescheduled_events == []
    assert _completed_payload(fake_redis)["start_dt"] == original_start_dt
    pending = _state_service(fake_redis).get_pending_confirmation("user-1")
    assert pending is not None
    assert pending["state"] == "WAITING_FOR_CONTACT"


async def test_explicit_reschedule_after_completed_state_still_updates_persisted_event(fake_redis):
    _mark_completed(_booking_service(fake_redis))
    calendar_service = RecordingCalendarService()
    processor = _processor(_booking_service(fake_redis, calendar_service=calendar_service))

    result = await processor.process(_message("хочу перенести запис на завтра о 12"))

    assert result["intent"] == "booking_reschedule"
    assert result["booking_result"]["status"] == "rescheduled"
    assert calendar_service.rescheduled_events
    assert calendar_service.rescheduled_events[0]["event_id"] == "calendar-event-123"
    assert _completed_payload(fake_redis)["start_dt"].endswith("T12:00:00+03:00")


async def test_fresh_booking_with_redis_memory_after_completed_state(fake_redis):
    _mark_completed(_booking_service(fake_redis))
    original_start_dt = _completed_payload(fake_redis)["start_dt"]
    calendar_service = RecordingCalendarService()
    processor = _processor(
        _booking_service(fake_redis, calendar_service=calendar_service),
        memory_service=RedisMemoryService(fake_redis),
    )

    await processor.process(_message("поставив дзвінок?"))
    result = await processor.process(_message("хочу записатись на чистку завтра о 12"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert calendar_service.rescheduled_events == []
    assert _completed_payload(fake_redis)["start_dt"] == original_start_dt
    assert fake_redis.exists("meta_bot:memory:user-1") == 1
    assert fake_redis.exists("booking:pending:user-1") == 1


async def test_failed_reschedule_preserves_persisted_start_dt(fake_redis):
    _mark_completed(_booking_service(fake_redis))
    original_start_dt = _completed_payload(fake_redis)["start_dt"]
    processor = _processor(
        _booking_service(fake_redis, calendar_service=FailingRescheduleCalendarService())
    )

    result = await processor.process(_message("післязавтра 15:00"))

    assert result["intent"] == "booking_reschedule"
    assert result["booking_result"]["status"] == "reschedule_handoff"
    assert "перенесли на" not in result["reply_text"]
    assert _completed_payload(fake_redis)["start_dt"] == original_start_dt


async def test_reschedule_missing_calendar_event_id_hands_off(fake_redis):
    _mark_completed(_booking_service(fake_redis), event_id=None)
    original_start_dt = _completed_payload(fake_redis)["start_dt"]
    calendar_service = RecordingCalendarService()
    processor = _processor(_booking_service(fake_redis, calendar_service=calendar_service))

    result = await processor.process(_message("післязавтра 15:00"))

    assert result["intent"] == "booking_reschedule"
    assert result["booking_result"]["status"] == "reschedule_handoff"
    assert "перенесли на" not in result["reply_text"]
    assert calendar_service.rescheduled_events == []
    assert _completed_payload(fake_redis)["start_dt"] == original_start_dt


def test_manual_followup_does_not_create_completed_booking_state(fake_redis):
    booking_service = _booking_service(fake_redis, calendar_service=UnconfiguredCalendarService())

    result = booking_service.start_booking_flow(
        sender_id="user-1",
        message_text="Давайте дзвінок завтра о 12 Іван 0991234567",
        source_channel="instagram",
    )

    assert result["status"] == "manual_followup"
    assert fake_redis.exists("booking:completed:user-1") == 0


def test_booking_service_without_redis_keeps_in_memory_completed_behavior():
    booking_service = BookingService(
        calendar_service=RecordingCalendarService(),
        language_service=LanguageService(),
    )

    _mark_completed(booking_service)

    assert booking_service.has_confirmed_booking("user-1")
    assert booking_service._get_completed_booking("user-1") == {
        "start_dt": "2026-04-27T12:00:00+03:00",
        "customer_name": "Іван",
        "email": "client@example.com",
        "phone": "0987121328",
        "calendar_event_id": "calendar-event-123",
    }
