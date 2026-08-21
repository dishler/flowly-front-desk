from __future__ import annotations

from datetime import datetime

import pytest

from app.application.dto.normalized_message import NormalizedMessage
from app.application.services.booking_service import BookingService
from app.application.services.front_desk_config_service import FrontDeskConfigService
from app.application.services.intent_service import IntentService
from app.application.services.knowledge_service import KnowledgeService
from app.application.services.language_service import LanguageService
from app.application.services.memory_service import MemoryService
from app.application.services.message_processor import MessageProcessor
from app.application.services.reply_service import ReplyService


class DummyAIService:
    def try_generate_reply(
        self,
        user_message: str,
        history=None,
        grounding_context=None,
        system_instruction=None,
    ) -> dict:
        return {"reply_text": None}


class RecordingAIService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

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
        return {"reply_text": "Уточніть, будь ласка, деталі, щоб зорієнтувати коректно."}


class DummyDedupService:
    def is_duplicate(self, message_mid: str) -> bool:
        return False

    def mark_processed(self, message_mid: str) -> None:
        pass


class DummyOutboundService:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_reply(self, platform: str, recipient_id: str, text: str) -> dict:
        self.sent.append({"platform": platform, "recipient_id": recipient_id, "text": text})
        return {"sent": True}


class DummySpeechService:
    async def transcribe_audio(self, file_url: str) -> str:
        return ""


class RecordingConfiguredCalendarService:
    def __init__(self) -> None:
        self.google_calendar_client = self
        self.checked: list[dict] = []
        self.created: list[dict] = []

    def is_configured(self) -> bool:
        return True

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        return True

    def get_available_slots(self, language: str):
        return ["завтра о 12:00", "завтра о 15:00"]

    def create_booking_event(
        self,
        start_dt,
        duration_minutes: int = 30,
        summary: str = "",
        description: str = "",
        attendee_emails=None,
    ):
        self.created.append(
            {
                "start_dt": start_dt,
                "duration_minutes": duration_minutes,
                "summary": summary,
                "description": description,
                "attendee_emails": attendee_emails or [],
            }
        )

        class CreatedEvent:
            event_id = "dental-event-1"
            html_link = "https://calendar.example/dental-event-1"
            status = "confirmed"

        return CreatedEvent()


@pytest.fixture
def dental_processor():
    return _build_dental_processor()


def _build_dental_processor(ai_service=None):
    memory_service = MemoryService()
    calendar_service = RecordingConfiguredCalendarService()
    config_service = FrontDeskConfigService("app/data/front_desk_config.json")
    booking_service = BookingService(
        calendar_service=calendar_service,
        language_service=LanguageService(),
        front_desk_config_service=config_service,
    )
    reply_service = ReplyService(
        ai_service=ai_service or DummyAIService(),
        memory_service=memory_service,
        knowledge_service=KnowledgeService("app/data/knowledge_base.json"),
        front_desk_config_service=config_service,
    )
    processor = MessageProcessor(
        memory_service=memory_service,
        reply_service=reply_service,
        outbound_service=DummyOutboundService(),
        dedup_service=DummyDedupService(),
        intent_service=IntentService(),
        booking_service=booking_service,
        speech_service=DummySpeechService(),
    )
    return processor, calendar_service


def _message(text: str, sender_id: str = "patient-1") -> NormalizedMessage:
    return NormalizedMessage(
        platform="instagram",
        sender_id=sender_id,
        recipient_id="clinic",
        message_mid="",
        user_message=text,
    )


def _assert_no_flowly_leakage(*texts: str) -> None:
    forbidden = [
        "flowly",
        "ai bot sales pitch",
        "implementation pricing",
        "спеціаліст на дзвінку",
        "generic sales consultation",
        "automation sales",
        "впровадження бота",
        "ai-бот для месенджерів",
        "налаштовуємо ai-бот",
        "старт від 200",
        "usd",
    ]
    combined = "\n".join(texts).lower()
    for marker in forbidden:
        assert marker not in combined


@pytest.mark.asyncio
async def test_dental_greeting_uses_clinic_identity_without_flowly_sales(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("Привіт"))

    assert result["intent"] == "general_question"
    assert "Smile Dental Clinic" in result["reply_text"]
    assert "чим можемо допомогти" in result["reply_text"].lower()
    assert not result["reply_text"].startswith("Привіт! Вітаю!")
    assert "сімейна стоматологічна клініка" not in result["reply_text"].lower()
    assert "профілактикою" not in result["reply_text"].lower()
    assert len(result["reply_text"]) < 80
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_english_greeting_is_short_and_uses_clinic_identity(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("Hello"))

    assert result["intent"] == "general_question"
    assert "Smile Dental Clinic" in result["reply_text"]
    assert "How can we help" in result["reply_text"]
    assert "family dental clinic" not in result["reply_text"].lower()
    assert "профілактикою" not in result["reply_text"].lower()
    assert len(result["reply_text"]) < 80
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_greeting_with_confirmed_booking_does_not_start_booking_action(dental_processor):
    processor, _calendar = dental_processor
    processor.booking_service._mark_booking_completed(
        "patient-1",
        start_dt=datetime(2026, 4, 27, 12, 0),
        email=None,
        phone="+380501112233",
        calendar_event_id="dental-event-existing",
    )

    result = await processor.process(_message("Привіт"))

    assert result["intent"] == "general_question"
    assert result["booking_result"] is None
    assert "Smile Dental Clinic" in result["reply_text"]
    assert "перенесли" not in result["reply_text"].lower()
    assert "скасував" not in result["reply_text"].lower()
    assert processor.booking_service.has_confirmed_booking("patient-1")
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_text", "expected"),
    [
        ("Які послуги у вас є?", ["професійна гігієна", "лікування карієсу"]),
        ("Які у вас послуги?", ["професійна гігієна", "лікування карієсу"]),
        ("Скільки коштує чистка?", ["від 1800 грн"]),
        ("Де ви знаходитесь?", ["Липська, 12", "Арсенальна"]),
        ("Коли ви працюєте?", ["09:00-19:00", "10:00-16:00"]),
    ],
)
async def test_dental_kb_answers_are_grounded(dental_processor, user_text, expected):
    processor, _calendar = dental_processor

    result = await processor.process(_message(user_text))

    for phrase in expected:
        assert phrase in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_pricing_short_followup_uses_recent_price_context(dental_processor):
    processor, _calendar = dental_processor

    first = await processor.process(_message("Скільки коштує консультація?"))
    followup = await processor.process(_message("а чистка?"))

    assert "700 грн" in first["reply_text"]
    assert "1800 грн" in followup["reply_text"]
    assert "уточніть" not in followup["reply_text"].lower()
    _assert_no_flowly_leakage(first["reply_text"], followup["reply_text"])


@pytest.mark.asyncio
async def test_dental_business_question_can_still_use_knowledge_base(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("Які послуги у вас є?"))

    assert "професійна гігієна" in result["reply_text"].lower()
    assert "лікування карієсу" in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_booking_unrelated_question_has_no_flowly_leakage(dental_processor):
    processor, _calendar = dental_processor

    await processor.process(_message("Хочу записатися на чистку у вівторок о 14"))
    result = await processor.process(_message("а де ви знаходитесь?"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "booking_unrelated_question"
    assert "візит" in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_booking_uses_visit_terms_calendar_availability_and_required_phone(dental_processor):
    processor, calendar = dental_processor

    start = await processor.process(_message("Хочу записатися на чистку"))
    time = await processor.process(_message("завтра о 12:00"))
    contact = await processor.process(_message("Олена Петренко +380501112233"))

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert "візит" in start["reply_text"]
    assert time["booking_result"]["status"] == "waiting_for_contact"
    assert "візит" in time["reply_text"]
    assert calendar.checked
    assert calendar.checked[0]["duration_minutes"] == 30
    assert contact["booking_result"]["status"] == "confirmed"
    assert contact["booking_result"]["customer_name"] == "Олена Петренко"
    assert contact["booking_result"]["contact_phone"] == "+380501112233"
    assert calendar.created
    assert calendar.created[0]["duration_minutes"] == 30
    assert calendar.created[0]["summary"] == "Візит booking"
    _assert_no_flowly_leakage(
        start["reply_text"],
        time["reply_text"],
        contact["reply_text"],
        calendar.created[0]["summary"],
        calendar.created[0]["description"],
    )


@pytest.mark.asyncio
async def test_dental_diagnosis_request_uses_safe_handoff(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("У мене дуже болить зуб, скажіть що це"))

    assert result["routing_category"] == "safe_handoff"
    assert result["intent"] == "diagnosis_request"
    assert "Не можу поставити діагноз" in result["reply_text"]
    assert "огляд стоматолога" in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_emergency_uses_safe_escalation(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("Це терміново, сильна кровотеча"))

    assert result["routing_category"] == "safe_handoff"
    assert result["intent"] == "medical_emergency"
    assert "екстреної медичної допомоги" in result["reply_text"]
    assert "чаті не можу безпечно оцінити" in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_unrelated_question_uses_clinic_fallback_without_sales_cta(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("Яка сьогодні погода?"))

    assert "послуга, ціна, графік чи запис на візит" in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_ai_fallback_prompts_are_not_flowly_sales():
    ai_service = RecordingAIService()
    processor, _calendar = _build_dental_processor(ai_service=ai_service)

    unknown_result = await processor.process(_message("Чи є у вас паркінг?"))

    assert unknown_result["reply_text"]
    assert len(ai_service.calls) == 1

    service_instruction = processor.reply_service._get_service_system_instruction("uk")
    instructions = "\n".join(
        [service_instruction]
        + [str(call["system_instruction"]) for call in ai_service.calls]
    )
    assert "Smile Dental Clinic" in instructions
    assert "Flowly" not in instructions
    assert "sales" not in instructions.lower()
    assert "спеціалістом на дзвінку" not in instructions.lower()
    assert "старт від 200" not in instructions.lower()
