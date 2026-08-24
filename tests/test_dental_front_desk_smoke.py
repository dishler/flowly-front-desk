from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

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


class BusyAt13ConfiguredCalendarService(RecordingConfiguredCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        return start_dt.hour != 13


class BusyAt12And13ConfiguredCalendarService(RecordingConfiguredCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        return start_dt.hour not in {12, 13}


class SelectiveConfiguredCalendarService(RecordingConfiguredCalendarService):
    def __init__(self, available_slots) -> None:
        super().__init__()
        self.available_slots = {
            (slot.date().isoformat(), slot.hour, slot.minute)
            for slot in available_slots
        }

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        return (start_dt.date().isoformat(), start_dt.hour, start_dt.minute) in self.available_slots


@pytest.fixture
def dental_processor():
    return _build_dental_processor()


def _build_dental_processor(ai_service=None, calendar_service=None):
    memory_service = MemoryService()
    calendar_service = calendar_service or RecordingConfiguredCalendarService()
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


def _kyiv_dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Kyiv"))


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
        "як працює бот",
        "для яких бізнес",
        "месенджерів",
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
@pytest.mark.parametrize("text", ["прив", "доброго"])
async def test_dental_short_greeting_variants_use_clinic_identity(dental_processor, text):
    processor, _calendar = dental_processor

    result = await processor.process(_message(text))

    assert result["intent"] == "general_question"
    assert "Smile Dental Clinic" in result["reply_text"]
    assert "чим можемо допомогти" in result["reply_text"].lower()
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
async def test_dental_pricing_short_followup_golden_multiturn_no_flowly_leakage(dental_processor):
    processor, _calendar = dental_processor

    first = await processor.process(_message("Скільки коштує консультація?"))
    followup = await processor.process(_message("А чистка?"))
    repeated_price = await processor.process(_message("ціна"))

    assert "700 грн" in first["reply_text"]
    assert "1800 грн" in followup["reply_text"]
    assert "професійна гігієна" in followup["reply_text"].lower()
    assert "1800 грн" in repeated_price["reply_text"]
    assert "2200 грн" not in repeated_price["reply_text"]
    assert "Для дзвінка підкажіть" not in followup["reply_text"]
    assert "Для дзвінка підкажіть" not in repeated_price["reply_text"]
    _assert_no_flowly_leakage(first["reply_text"], followup["reply_text"], repeated_price["reply_text"])


@pytest.mark.asyncio
async def test_dental_explicit_cleaning_price_is_specific_not_general(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("ціна чистки"))

    assert "1800 грн" in result["reply_text"]
    assert "2200 грн" not in result["reply_text"]
    assert "6500 грн" not in result["reply_text"]
    assert "Для дзвінка підкажіть" not in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_short_how_much_service_questions_are_specific(dental_processor):
    processor, _calendar = dental_processor

    cleaning = await processor.process(_message("скільки чистка?"))
    whitening = await processor.process(_message("а відбілювання?"))

    assert "1800 грн" in cleaning["reply_text"]
    assert "6500 грн" in whitening["reply_text"]
    assert "2200 грн" not in cleaning["reply_text"]
    assert "1800 грн" not in whitening["reply_text"]
    _assert_no_flowly_leakage(cleaning["reply_text"], whitening["reply_text"])


@pytest.mark.asyncio
async def test_dental_business_question_can_still_use_knowledge_base(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("Які послуги у вас є?"))

    assert "професійна гігієна" in result["reply_text"].lower()
    assert "лікування карієсу" in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_price_objection_without_context_cannot_leak_flowly_sales(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("це дорого"))

    assert result["intent"] == "front_desk_safe_fallback"
    assert result["routing_category"] == "safe_handoff"
    assert "старт від 200" not in result["reply_text"]
    assert "мінімального сценарію" not in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_pediatric_question_cannot_leak_flowly_niche_copy(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("є дитячий стоматолог"))

    assert result["intent"] in {"front_desk_contextual_answer", "front_desk_safe_fallback"}
    assert "для стоматологій" not in result["reply_text"].lower()
    assert "сценар" not in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_unknown_business_question_cannot_reach_flowly_sales_fallback(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("що ви взагалі робите"))

    assert result["intent"] == "front_desk_safe_fallback"
    assert result["routing_category"] == "safe_handoff"
    assert "що робить бот" not in result["reply_text"].lower()
    assert "для яких бізнесів" not in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_price_objection_after_price_context_stays_dental(dental_processor):
    processor, _calendar = dental_processor

    price = await processor.process(_message("Скільки коштує відбілювання?"))
    objection = await processor.process(_message("це дорого"))

    assert "6500" in price["reply_text"]
    assert objection["intent"] == "front_desk_safe_fallback"
    assert "старт від 200" not in objection["reply_text"]
    assert "мінімального сценарію" not in objection["reply_text"]
    _assert_no_flowly_leakage(price["reply_text"], objection["reply_text"])


@pytest.mark.asyncio
async def test_dental_booking_intent_still_enters_booking(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("хочу записатись"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert result["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_active_booking_faq_interruption_preserves_booking_context(dental_processor):
    processor, _calendar = dental_processor

    await processor.process(_message("хочу записатись"))
    faq = await processor.process(_message("скільки коштує відбілювання?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert faq["intent"] == "booking_grounded_question"
    assert "6500" in faq["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    _assert_no_flowly_leakage(faq["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "дорого",
        "чому так дорого",
        "а дешевше є",
        "як це працює",
        "це бот?",
        "ви бот?",
        "це штучний інтелект?",
        "які у вас можливості",
        "кому це підходить",
        "для стоматологій працює?",
        "лікуєте дітей?",
        "дитині 8 років можна?",
        "можна дитину записати?",
        "у вас є ортодонт?",
        "ставите брекети?",
    ],
)
async def test_dental_adversarial_sales_triggers_do_not_leak_flowly_copy(text):
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    _assert_no_flowly_leakage(result["reply_text"])
    assert "старт від 200" not in result["reply_text"]
    assert "для стоматологій це" not in result["reply_text"].lower()
    assert "бот може забрати першу лінію" not in result["reply_text"].lower()


@pytest.mark.asyncio
async def test_dental_booking_unrelated_question_has_no_flowly_leakage(dental_processor):
    processor, _calendar = dental_processor

    await processor.process(_message("Хочу записатися на чистку у вівторок о 14"))
    result = await processor.process(_message("а де ви знаходитесь?"))

    assert result["intent"] == "booking_grounded_question"
    assert result["booking_result"] is None
    assert "Липська, 12" in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_exact_time_booking_rejects_closed_sunday_before_calendar(dental_processor):
    processor, calendar = dental_processor

    result = await processor.process(_message("Хочу записатися на чистку завтра о 12"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "outside_business_hours"
    assert "клініка не працює" in result["reply_text"].lower()
    assert calendar.checked == []
    assert processor.booking_service.get_booking_state("patient-1").value == "NONE"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_exact_time_booking_rolls_saturday_9_to_next_week_and_rejects(dental_processor):
    processor, calendar = dental_processor

    result = await processor.process(_message("Хочу записатися у суботу о 9"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "outside_business_hours"
    assert result["booking_result"]["start_dt"] == "2026-08-29T09:00:00+03:00"
    assert calendar.checked == []
    assert "клініка не працює" in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_exact_time_booking_rolls_saturday_10_to_next_week_and_checks_calendar(dental_processor):
    processor, calendar = dental_processor

    result = await processor.process(_message("Хочу записатися у суботу о 10"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert calendar.checked[-1]["start_dt"].isoformat() == "2026-08-29T10:00:00+03:00"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_exact_time_booking_with_service_rolls_saturday_10(dental_processor):
    processor, calendar = dental_processor

    result = await processor.process(_message("Хочу записатися на чистку у суботу о 10"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert calendar.checked[-1]["start_dt"].isoformat() == "2026-08-29T10:00:00+03:00"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_daypart_multiturn_uses_pending_date_and_rejects_closed_tomorrow():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 23, 10)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("запис на завтра"))
    morning = await processor.process(_message("зранку"))

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert start["booking_result"]["requested_date"] == "2026-08-23"
    assert morning["booking_result"]["status"] == "outside_business_hours"
    assert "клініка не працює" in morning["reply_text"].lower()
    assert calendar.checked == []


@pytest.mark.asyncio
async def test_dental_daypart_one_turn_tomorrow_morning_rejects_closed_day():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 23, 10)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися завтра зранку"))

    assert result["booking_result"]["status"] == "outside_business_hours"
    assert result["booking_result"]["requested_date"] == "2026-08-23"
    assert calendar.checked == []


@pytest.mark.asyncio
async def test_dental_daypart_one_turn_monday_morning_suggests_calendar_verified_slots():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 24, 10),
        _kyiv_dt(2026, 8, 24, 11, 30),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися у понеділок зранку"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    selected = await processor.process(_message("10"))
    pending_after_select = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "daypart_slots_suggested"
    assert "У понеділок зранку" in result["reply_text"]
    assert "10:00" in result["reply_text"]
    assert "11:30" in result["reply_text"]
    assert "09:00" not in result["reply_text"]
    assert {slot["start_dt"] for slot in result["booking_result"]["suggested_slots"]} == {
        "2026-08-24T10:00:00+03:00",
        "2026-08-24T11:30:00+03:00",
    }
    assert pending["availability_context"] is True
    assert pending["requested_date"] == "2026-08-24"
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-24T10:00:00+03:00"
    assert pending_after_select["state"] == "WAITING_FOR_CONTACT"
    assert pending_after_select["start_dt"] == "2026-08-24T10:00:00+03:00"
    checked_keys = {
        (item["start_dt"].date().isoformat(), item["start_dt"].hour, item["start_dt"].minute)
        for item in calendar.checked
    }
    assert ("2026-08-24", 10, 0) in checked_keys
    assert ("2026-08-24", 11, 30) in checked_keys


@pytest.mark.asyncio
async def test_dental_time_window_after_suggests_calendar_verified_slots():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 15, 30),
        _kyiv_dt(2026, 8, 25, 16, 30),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися у вівторок після 15"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "У вівторок після 15:00" in result["reply_text"]
    assert "15:30" in result["reply_text"]
    assert "16:30" in result["reply_text"]
    assert "15:00" not in result["reply_text"].replace("після 15:00", "")
    assert {slot["start_dt"] for slot in result["booking_result"]["suggested_slots"]} == {
        "2026-08-25T15:30:00+03:00",
        "2026-08-25T16:30:00+03:00",
    }


@pytest.mark.asyncio
async def test_dental_time_window_before_suggests_only_slots_before_end():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 9),
        _kyiv_dt(2026, 8, 25, 13, 30),
        _kyiv_dt(2026, 8, 25, 14),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися у вівторок до 14"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert {slot["start_dt"] for slot in result["booking_result"]["suggested_slots"]} == {
        "2026-08-25T09:00:00+03:00",
        "2026-08-25T13:30:00+03:00",
    }


@pytest.mark.asyncio
async def test_dental_time_window_range_suggests_only_slots_inside_range():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 13),
        _kyiv_dt(2026, 8, 25, 15, 30),
        _kyiv_dt(2026, 8, 25, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися у вівторок з 13 до 16"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert {slot["start_dt"] for slot in result["booking_result"]["suggested_slots"]} == {
        "2026-08-25T13:00:00+03:00",
        "2026-08-25T15:30:00+03:00",
    }


@pytest.mark.asyncio
async def test_dental_time_window_multiturn_preserves_pending_date():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 25, 15, 30)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("запис на вівторок"))
    result = await processor.process(_message("після 15"))

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert start["booking_result"]["requested_date"] == "2026-08-25"
    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["requested_date"] == "2026-08-25"
    assert result["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-25T15:30:00+03:00"}
    ]


@pytest.mark.asyncio
async def test_dental_time_window_earlier_later_and_selection_keep_context():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 13),
        _kyiv_dt(2026, 8, 25, 14),
        _kyiv_dt(2026, 8, 25, 15, 30),
        _kyiv_dt(2026, 8, 25, 16, 30),
        _kyiv_dt(2026, 8, 25, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    initial = await processor.process(_message("Хочу записатися у вівторок після 15"))
    earlier = await processor.process(_message("а раніше?"))
    later = await processor.process(_message("а пізніше?"))
    selected = await processor.process(_message("давай 16:30"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert initial["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-25T15:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-25T16:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-25T18:00:00+03:00"},
    ]
    assert {slot["start_dt"] for slot in earlier["booking_result"]["suggested_slots"]} == {
        "2026-08-25T13:00:00+03:00",
        "2026-08-25T14:00:00+03:00",
    }
    assert later["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-25T15:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-25T16:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-25T18:00:00+03:00"},
    ]
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-25T16:30:00+03:00"
    assert pending["state"] == "WAITING_FOR_CONTACT"
    assert pending["customer_name"] is None
    assert pending.get("phone") is None


@pytest.mark.asyncio
async def test_dental_time_window_refinement_before_after_suggestions_keeps_date():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 10),
        _kyiv_dt(2026, 8, 25, 15, 30),
        _kyiv_dt(2026, 8, 25, 16, 30),
        _kyiv_dt(2026, 8, 25, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    initial = await processor.process(_message("Хочу записатися на чистку у вівторок після 15"))
    initial_call_count = len(calendar.checked)
    refined = await processor.process(_message("а є щось до 16?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert initial["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-25T15:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-25T16:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-25T18:00:00+03:00"},
    ]
    assert refined["booking_result"]["status"] == "time_window_slots_suggested"
    assert refined["booking_result"]["requested_date"] == "2026-08-25"
    assert {slot["start_dt"] for slot in refined["booking_result"]["suggested_slots"]} == {
        "2026-08-25T10:00:00+03:00",
        "2026-08-25T15:30:00+03:00",
    }
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-25"
    assert len(calendar.checked) > initial_call_count


@pytest.mark.asyncio
async def test_dental_time_window_refinement_after_after_suggestions_keeps_date():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 15, 30),
        _kyiv_dt(2026, 8, 25, 16, 30),
        _kyiv_dt(2026, 8, 25, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на чистку у вівторок після 15"))
    initial_call_count = len(calendar.checked)
    refined = await processor.process(_message("а після 17?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert refined["booking_result"]["status"] == "time_window_slots_suggested"
    assert refined["booking_result"]["requested_date"] == "2026-08-25"
    assert refined["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-25T18:00:00+03:00"}
    ]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-25"
    assert len(calendar.checked) > initial_call_count


@pytest.mark.asyncio
async def test_dental_time_window_refinement_between_after_suggestions_keeps_date():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 14),
        _kyiv_dt(2026, 8, 25, 15, 30),
        _kyiv_dt(2026, 8, 25, 16, 30),
        _kyiv_dt(2026, 8, 25, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на чистку у вівторок після 15"))
    initial_call_count = len(calendar.checked)
    refined = await processor.process(_message("а між 14 і 16?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert refined["booking_result"]["status"] == "time_window_slots_suggested"
    assert refined["booking_result"]["requested_date"] == "2026-08-25"
    assert {slot["start_dt"] for slot in refined["booking_result"]["suggested_slots"]} == {
        "2026-08-25T14:00:00+03:00",
        "2026-08-25T15:30:00+03:00",
    }
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-25"
    assert len(calendar.checked) > initial_call_count


@pytest.mark.asyncio
async def test_dental_active_booking_time_window_refinement_bypasses_contextual_pricing():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 10),
        _kyiv_dt(2026, 8, 27, 10, 30),
        _kyiv_dt(2026, 8, 27, 11),
        _kyiv_dt(2026, 8, 27, 11, 30),
        _kyiv_dt(2026, 8, 27, 12),
        _kyiv_dt(2026, 8, 27, 12, 30),
        _kyiv_dt(2026, 8, 27, 16, 30),
        _kyiv_dt(2026, 8, 27, 17),
        _kyiv_dt(2026, 8, 27, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    initial = await processor.process(_message("Хочу записатися у четвер до 15"))
    later = await processor.process(_message("а є щось пізніше?"))
    before_refinement_calls = len(calendar.checked)
    refined = await processor.process(_message("а після 16 є щось?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert initial["booking_result"]["status"] == "time_window_slots_suggested"
    assert later["booking_result"]["status"] == "time_window_slots_suggested"
    assert refined["intent"] == "booking_flow"
    assert refined["booking_result"]["status"] == "time_window_slots_suggested"
    assert refined["booking_result"]["requested_date"] == "2026-08-27"
    assert refined["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T16:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T17:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T18:00:00+03:00"},
    ]
    assert "коштує" not in refined["reply_text"].lower()
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"
    assert len(calendar.checked) > before_refinement_calls


@pytest.mark.asyncio
async def test_dental_active_booking_time_window_refinement_beats_stale_pricing_context():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 10),
        _kyiv_dt(2026, 8, 27, 16, 30),
        _kyiv_dt(2026, 8, 27, 17),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися у четвер до 15"))
    processor.memory_service.update_context(
        "patient-1",
        current_service_id="teeth_whitening",
        question_context="pricing",
    )
    refined = await processor.process(_message("а після 16 є щось?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert refined["intent"] == "booking_flow"
    assert refined["booking_result"]["status"] == "time_window_slots_suggested"
    assert refined["booking_result"]["requested_date"] == "2026-08-27"
    assert refined["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T16:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T17:00:00+03:00"},
    ]
    assert "6500" not in refined["reply_text"]
    assert "відбілювання" not in refined["reply_text"].lower()
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"


@pytest.mark.asyncio
async def test_dental_active_booking_time_window_keeps_legitimate_price_faq():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 10),
        _kyiv_dt(2026, 8, 27, 10, 30),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися у четвер до 15"))
    faq = await processor.process(_message("скільки коштує відбілювання?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert faq["intent"] == "booking_grounded_question"
    assert faq["booking_result"] is None
    assert "6500" in faq["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"


@pytest.mark.asyncio
async def test_dental_time_window_no_verified_slots_are_not_invented():
    calendar = SelectiveConfiguredCalendarService([])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися у вівторок після 15"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["suggested_slots"] == []
    assert "не бачу вільних слотів" in result["reply_text"].lower()


@pytest.mark.asyncio
async def test_dental_time_window_keeps_exact_time_booking_unchanged():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 25, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися у вівторок о 14"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-08-25T14:00:00+03:00"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 25, 14), "duration_minutes": 30}
    ]


@pytest.mark.asyncio
async def test_dental_common_service_day_booking_phrase_golden_multiturn(dental_processor):
    processor, calendar = dental_processor

    start = await processor.process(_message("хочу на чистку у вівторок"))
    time = await processor.process(_message("на 16"))

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert "вівторок" in start["reply_text"].lower()
    assert "який день" not in start["reply_text"].lower()
    assert time["booking_result"]["status"] == "waiting_for_contact"
    assert calendar.checked[-1]["start_dt"].weekday() == 1
    assert calendar.checked[-1]["start_dt"].hour == 16
    _assert_no_flowly_leakage(start["reply_text"], time["reply_text"])


@pytest.mark.asyncio
async def test_dental_busy_slot_another_time_preserves_day_golden_multiturn():
    calendar = BusyAt13ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("Хотів би записатись на чистку у вівторок"))
    busy = await processor.process(_message("На 13"))
    another = await processor.process(_message("Інший час"))
    recovered = await processor.process(_message("Давайте о 16"))

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert "вівторок" in start["reply_text"].lower()
    assert busy["booking_result"]["status"] == "slot_suggested"
    assert calendar.checked[0]["start_dt"].weekday() == 1
    assert calendar.checked[0]["start_dt"].hour == 13
    assert another["booking_result"]["status"] == "waiting_for_time"
    assert another["booking_result"]["requested_date"]
    assert "іншу годину" in another["reply_text"].lower()
    assert "вівторок" in another["reply_text"].lower()
    assert "який день" not in another["reply_text"].lower()
    assert recovered["booking_result"]["status"] == "waiting_for_contact"
    assert calendar.checked[-1]["start_dt"].weekday() == 1
    assert calendar.checked[-1]["start_dt"].hour == 16
    assert "16:00" in recovered["reply_text"]
    _assert_no_flowly_leakage(
        start["reply_text"],
        busy["reply_text"],
        another["reply_text"],
        recovered["reply_text"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["а раніше нема?", "а пізніше?"])
async def test_dental_busy_slot_earlier_later_stays_in_time_selection(text):
    calendar = BusyAt13ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу записатись на чистку у вівторок"))
    suggested = await processor.process(_message("на 13"))
    result = await processor.process(_message(text))

    assert suggested["booking_result"]["status"] == "slot_suggested"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "ім" not in result["reply_text"].lower()
    assert "номер телефону" not in result["reply_text"].lower()
    assert "вівторок" in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_suggested_slot_acceptance_does_not_become_customer_name():
    calendar = BusyAt13ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатись на чистку у вівторок"))
    suggested = await processor.process(_message("На 13"))
    accepted = await processor.process(_message("тоді на 14 буде гуд"))
    pending_after_accept = dict(processor.booking_service._get_pending_confirmation("patient-1"))
    phone = await processor.process(_message("0987121328"))
    pending_after_phone = dict(processor.booking_service._get_pending_confirmation("patient-1"))
    confirmed = await processor.process(_message("Дмитро"))

    assert suggested["booking_result"]["status"] == "slot_suggested"
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert "14:00" in accepted["reply_text"]
    assert pending_after_accept["customer_name"] is None
    assert phone["booking_result"]["status"] == "waiting_for_name"
    assert "ім" in phone["reply_text"].lower()
    assert "номер телефону" not in phone["reply_text"].lower()
    assert pending_after_phone["contact_phone"] == "0987121328"
    assert pending_after_phone["customer_name"] is None
    assert confirmed["booking_result"]["status"] == "confirmed"
    assert confirmed["booking_result"]["customer_name"] == "Дмитро"
    assert calendar.created[-1]["start_dt"].weekday() == 1
    assert calendar.created[-1]["start_dt"].hour == 14
    _assert_no_flowly_leakage(
        accepted["reply_text"],
        phone["reply_text"],
        confirmed["reply_text"],
    )


@pytest.mark.asyncio
async def test_dental_suggested_slot_acceptance_with_tak_norm_never_becomes_name():
    calendar = BusyAt12And13ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    requested = await processor.process(_message("хочу записатись на чистку у понеділок о 12"))
    accepted = await processor.process(_message("так, 14 норм"))
    pending_after_accept = dict(processor.booking_service._get_pending_confirmation("patient-1"))
    phone = await processor.process(_message("мій номер 0987121328"))
    pending_after_phone = dict(processor.booking_service._get_pending_confirmation("patient-1"))
    confirmed = await processor.process(_message("Дмитро"))

    assert requested["booking_result"]["status"] == "slot_suggested"
    assert "14:00" in requested["reply_text"]
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert "14:00" in accepted["reply_text"]
    assert pending_after_accept["customer_name"] is None
    assert pending_after_accept["contact_phone"] is None
    assert phone["booking_result"]["status"] == "waiting_for_name"
    assert pending_after_phone["contact_phone"] == "0987121328"
    assert pending_after_phone["customer_name"] is None
    assert confirmed["booking_result"]["status"] == "confirmed"
    assert confirmed["booking_result"]["customer_name"] == "Дмитро"
    assert "так норм" not in confirmed["reply_text"].lower()
    assert calendar.created[-1]["start_dt"].hour == 14
    _assert_no_flowly_leakage(accepted["reply_text"], phone["reply_text"], confirmed["reply_text"])


@pytest.mark.asyncio
async def test_dental_combined_contact_after_suggested_slot_acceptance_confirms_normally():
    calendar = BusyAt12And13ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу записатись на чистку у понеділок о 12"))
    accepted = await processor.process(_message("так, 14 норм"))
    confirmed = await processor.process(_message("Дмитро 0987121328"))

    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert confirmed["booking_result"]["status"] == "confirmed"
    assert confirmed["booking_result"]["customer_name"] == "Дмитро"
    assert confirmed["booking_result"]["contact_phone"] == "0987121328"
    assert "так норм" not in confirmed["reply_text"].lower()
    assert calendar.created[-1]["start_dt"].hour == 14
    _assert_no_flowly_leakage(accepted["reply_text"], confirmed["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize("acceptance_text", ["ок 14", "давай 14", "14 норм"])
async def test_dental_suggested_slot_acceptance_variants_never_become_customer_names(
    acceptance_text,
):
    calendar = BusyAt12And13ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу записатись на чистку у понеділок о 12"))
    accepted = await processor.process(_message(acceptance_text))
    pending_after_accept = dict(processor.booking_service._get_pending_confirmation("patient-1"))
    phone = await processor.process(_message("мій номер 0987121328"))
    pending_after_phone = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert pending_after_accept["customer_name"] is None
    assert pending_after_accept["contact_phone"] is None
    assert phone["booking_result"]["status"] == "waiting_for_name"
    assert pending_after_phone["contact_phone"] == "0987121328"
    assert pending_after_phone["customer_name"] is None
    assert calendar.checked[-1]["start_dt"].hour == 14
    _assert_no_flowly_leakage(accepted["reply_text"], phone["reply_text"])


@pytest.mark.asyncio
async def test_dental_combined_contact_completes_without_repeated_contact_question(dental_processor):
    processor, calendar = dental_processor

    await processor.process(_message("Хочу записатись на чистку у вівторок"))
    await processor.process(_message("На 16"))
    confirmed = await processor.process(_message("Дмитро, 0987121328"))

    assert confirmed["booking_result"]["status"] == "confirmed"
    assert confirmed["booking_result"]["customer_name"] == "Дмитро"
    assert confirmed["booking_result"]["contact_phone"] == "0987121328"
    assert "залиште" not in confirmed["reply_text"].lower()
    assert calendar.created[-1]["start_dt"].weekday() == 1
    assert calendar.created[-1]["start_dt"].hour == 16
    _assert_no_flowly_leakage(confirmed["reply_text"])


@pytest.mark.asyncio
async def test_dental_waiting_contact_customer_phone_phrase_is_captured(dental_processor):
    processor, calendar = dental_processor

    await processor.process(_message("Хочу записатись на чистку у вівторок"))
    await processor.process(_message("На 14"))
    phone = await processor.process(_message("мій номер 0987121328"))
    pending_after_phone = dict(processor.booking_service._get_pending_confirmation("patient-1"))
    confirmed = await processor.process(_message("Дмитро"))

    assert phone["booking_result"]["status"] == "waiting_for_name"
    assert pending_after_phone["contact_phone"] == "0987121328"
    assert pending_after_phone["customer_name"] is None
    assert "ім" in phone["reply_text"].lower()
    assert "Контакти:" not in phone["reply_text"]
    assert confirmed["booking_result"]["status"] == "confirmed"
    assert confirmed["booking_result"]["customer_name"] == "Дмитро"
    assert confirmed["booking_result"]["contact_phone"] == "0987121328"
    assert calendar.created[-1]["start_dt"].weekday() == 1
    assert calendar.created[-1]["start_dt"].hour == 14
    _assert_no_flowly_leakage(phone["reply_text"], confirmed["reply_text"])


@pytest.mark.asyncio
async def test_dental_waiting_contact_business_phone_question_remains_faq(dental_processor):
    processor, _calendar = dental_processor

    await processor.process(_message("Хочу записатись на чистку у вівторок"))
    await processor.process(_message("На 14"))
    faq = await processor.process(_message("який у вас номер телефону?"))
    pending_after_faq = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert faq["intent"] == "booking_grounded_question"
    assert "0987121328" not in faq["reply_text"]
    assert pending_after_faq["contact_phone"] is None
    assert pending_after_faq["customer_name"] is None
    assert pending_after_faq["state"] == "WAITING_FOR_CONTACT"
    _assert_no_flowly_leakage(faq["reply_text"])


@pytest.mark.asyncio
async def test_dental_waiting_contact_compact_time_correction_updates_time_not_name(dental_processor):
    processor, calendar = dental_processor

    await processor.process(_message("Хочу записатись на чистку у вівторок"))
    await processor.process(_message("На 14"))
    corrected = await processor.process(_message("не 14 а 16"))
    pending_after_correction = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert corrected["booking_result"]["status"] == "waiting_for_contact"
    assert pending_after_correction["customer_name"] is None
    assert pending_after_correction["contact_phone"] is None
    assert calendar.checked[-1]["start_dt"].weekday() == 1
    assert calendar.checked[-1]["start_dt"].hour == 16
    assert "16:00" in corrected["reply_text"]
    _assert_no_flowly_leakage(corrected["reply_text"])


@pytest.mark.asyncio
async def test_dental_waiting_contact_reject_and_replace_time_does_not_become_name(dental_processor):
    processor, calendar = dental_processor

    await processor.process(_message("Хочу записатись на чистку у вівторок"))
    await processor.process(_message("На 14"))
    corrected = await processor.process(_message("не хочу 14, краще 16"))
    pending_after_correction = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert corrected["booking_result"]["status"] == "waiting_for_contact"
    assert pending_after_correction["customer_name"] is None
    assert pending_after_correction["contact_phone"] is None
    assert calendar.checked[-1]["start_dt"].weekday() == 1
    assert calendar.checked[-1]["start_dt"].hour == 16
    assert "16:00" in corrected["reply_text"]
    _assert_no_flowly_leakage(corrected["reply_text"])


@pytest.mark.asyncio
async def test_dental_waiting_contact_service_correction_updates_service_not_name(dental_processor):
    processor, _calendar = dental_processor

    await processor.process(_message("Хочу записатись на консультацію у вівторок"))
    await processor.process(_message("На 14"))
    corrected = await processor.process(_message("чистку, не консультацію"))
    pending_after_correction = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert corrected["booking_result"]["status"] == "waiting_for_contact"
    assert corrected["booking_result"]["service_id"] == "dental_cleaning"
    assert pending_after_correction["service_id"] == "dental_cleaning"
    assert pending_after_correction["customer_name"] is None
    assert pending_after_correction["contact_phone"] is None
    assert "T14:00:00" in pending_after_correction["start_dt"]
    assert "залиште" in corrected["reply_text"].lower()
    _assert_no_flowly_leakage(corrected["reply_text"])


@pytest.mark.asyncio
async def test_dental_waiting_contact_normal_names_still_work(dental_processor):
    processor, calendar = dental_processor

    await processor.process(_message("Хочу записатись на чистку у вівторок"))
    await processor.process(_message("На 14"))
    name = await processor.process(_message("Дмитро"))
    pending_after_name = dict(processor.booking_service._get_pending_confirmation("patient-1"))
    completed = await processor.process(_message("0987121328"))

    assert name["booking_result"]["status"] == "waiting_for_contact"
    assert pending_after_name["customer_name"] == "Дмитро"
    assert pending_after_name["contact_phone"] is None
    assert completed["booking_result"]["status"] == "confirmed"
    assert completed["booking_result"]["customer_name"] == "Дмитро"
    assert completed["booking_result"]["contact_phone"] == "0987121328"
    assert calendar.created[-1]["start_dt"].hour == 14

    processor, calendar = _build_dental_processor()
    await processor.process(_message("Хочу записатись на чистку у вівторок"))
    await processor.process(_message("На 14"))
    combined = await processor.process(_message("Дмитро 0987121328"))

    assert combined["booking_result"]["status"] == "confirmed"
    assert combined["booking_result"]["customer_name"] == "Дмитро"
    assert combined["booking_result"]["contact_phone"] == "0987121328"
    assert calendar.created[-1]["start_dt"].hour == 14
    _assert_no_flowly_leakage(name["reply_text"], completed["reply_text"], combined["reply_text"])


@pytest.mark.asyncio
async def test_dental_active_booking_location_faq_preserves_booking_state(dental_processor):
    processor, calendar = dental_processor

    start = await processor.process(_message("Хотів би записатись на чистку у вівторок"))
    pending_before = processor.booking_service._get_pending_confirmation("patient-1")
    faq = await processor.process(_message("А де ви знаходитесь?"))
    pending_after = processor.booking_service._get_pending_confirmation("patient-1")
    time = await processor.process(_message("давайте о 16"))

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert pending_before["requested_date"]
    assert faq["intent"] == "booking_grounded_question"
    assert "Липська, 12" in faq["reply_text"]
    assert pending_after["state"] == "WAITING_FOR_TIME"
    assert pending_after["requested_date"] == pending_before["requested_date"]
    assert time["booking_result"]["status"] == "waiting_for_contact"
    assert calendar.checked[-1]["start_dt"].weekday() == 1
    assert calendar.checked[-1]["start_dt"].hour == 16
    _assert_no_flowly_leakage(start["reply_text"], faq["reply_text"], time["reply_text"])


async def _seed_waiting_for_time_with_stale_pricing_context(processor):
    start = await processor.process(_message("Хочу записатися на чистку у вівторок"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    processor.memory_service.update_context(
        "patient-1",
        current_service_id="dental_cleaning",
        question_context="pricing",
    )
    return start, pending


@pytest.mark.asyncio
async def test_dental_active_booking_fresh_intent_bypasses_stale_pricing_context(dental_processor):
    processor, calendar = dental_processor

    start, pending_before = await _seed_waiting_for_time_with_stale_pricing_context(processor)
    result = await processor.process(_message("Хочу записатися на чистку у понеділок о 12"))
    pending_after = processor.booking_service._get_pending_confirmation("patient-1")

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert pending_before["state"] == "WAITING_FOR_TIME"
    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "1800" not in result["reply_text"]
    assert "коштує" not in result["reply_text"].lower()
    assert calendar.checked
    assert calendar.checked[-1]["start_dt"].hour == 12
    assert pending_after["state"] == "WAITING_FOR_CONTACT"
    assert pending_after["start_dt"][11:16] == "12:00"


@pytest.mark.asyncio
async def test_dental_active_booking_fresh_saturday_9_rejects_next_week_without_calendar(dental_processor):
    processor, calendar = dental_processor

    await _seed_waiting_for_time_with_stale_pricing_context(processor)
    result = await processor.process(_message("Хочу записатися у суботу о 9"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "outside_business_hours"
    assert result["booking_result"]["start_dt"] == "2026-08-29T09:00:00+03:00"
    assert calendar.checked == []


@pytest.mark.asyncio
async def test_dental_active_booking_fresh_saturday_10_checks_calendar(dental_processor):
    processor, calendar = dental_processor

    await _seed_waiting_for_time_with_stale_pricing_context(processor)
    result = await processor.process(_message("Хочу записатися у суботу о 10"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert calendar.checked[-1]["start_dt"].isoformat() == "2026-08-29T10:00:00+03:00"


@pytest.mark.asyncio
async def test_dental_active_booking_price_faq_still_uses_stale_pricing_context(dental_processor):
    processor, calendar = dental_processor

    _start, pending_before = await _seed_waiting_for_time_with_stale_pricing_context(processor)
    result = await processor.process(_message("скільки коштує чистка?"))
    pending_after = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_grounded_question"
    assert result["booking_result"] is None
    assert "1800" in result["reply_text"]
    assert pending_after["state"] == "WAITING_FOR_TIME"
    assert pending_after["requested_date"] == pending_before["requested_date"]
    assert calendar.checked == []


@pytest.mark.asyncio
async def test_dental_active_booking_explicit_fresh_marker_starts_fresh_flow(dental_processor):
    processor, calendar = dental_processor

    await _seed_waiting_for_time_with_stale_pricing_context(processor)
    result = await processor.process(_message("запишіть мене на понеділок о 12"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "1800" not in result["reply_text"]
    assert calendar.checked[-1]["start_dt"].hour == 12


@pytest.mark.asyncio
async def test_dental_active_booking_reschedule_wording_does_not_start_fresh_flow(dental_processor):
    processor, calendar = dental_processor

    await _seed_waiting_for_time_with_stale_pricing_context(processor)
    result = await processor.process(_message("хочу перенести запис на понеділок о 12"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["intent"] != "booking_request"
    assert calendar.checked[-1]["start_dt"].hour == 12


@pytest.mark.asyncio
async def test_dental_active_booking_cancel_stays_unchanged(dental_processor):
    processor, calendar = dental_processor

    await _seed_waiting_for_time_with_stale_pricing_context(processor)
    result = await processor.process(_message("скасуйте запис"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "cancelled"
    assert result["booking_result"]["booking_state"] == "NONE"
    assert calendar.checked == []


@pytest.mark.asyncio
async def test_dental_fresh_sender_exact_booking_phrase_unchanged(dental_processor):
    processor, calendar = dental_processor

    result = await processor.process(_message("Хочу записатися на чистку у понеділок о 12"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "1800" not in result["reply_text"]
    assert calendar.checked[-1]["start_dt"].hour == 12


@pytest.mark.asyncio
async def test_dental_repeated_unresolved_fallback_has_no_product_leakage(dental_processor):
    processor, _calendar = dental_processor

    first = await processor.process(_message("яка погода?"))
    second = await processor.process(_message("Дмитро 0987121328"))
    third = await processor.process(_message("інший"))

    combined = "\n".join([first["reply_text"], second["reply_text"], third["reply_text"]]).lower()
    assert "flowly" not in combined
    assert "бот" not in combined
    assert "для яких бізнес" not in combined
    assert "як працює бот" not in combined
    _assert_no_flowly_leakage(first["reply_text"], second["reply_text"], third["reply_text"])


@pytest.mark.asyncio
async def test_dental_confirmed_booking_allows_normal_faq_and_pricing(dental_processor):
    processor, _calendar = dental_processor

    await processor.process(_message("Хочу записатись на чистку у вівторок"))
    await processor.process(_message("На 16"))
    confirmed = await processor.process(_message("Дмитро, 0987121328"))
    location = await processor.process(_message("А де ви знаходитесь?"))
    whitening = await processor.process(_message("Скільки коштує відбілювання?"))

    assert confirmed["booking_result"]["status"] == "confirmed"
    assert location["intent"] == "front_desk_contextual_answer"
    assert "Липська, 12" in location["reply_text"]
    assert whitening["intent"] == "front_desk_contextual_answer"
    assert "6500 грн" in whitening["reply_text"]
    assert "Для дзвінка підкажіть" not in location["reply_text"]
    assert "Для дзвінка підкажіть" not in whitening["reply_text"]
    _assert_no_flowly_leakage(location["reply_text"], whitening["reply_text"])


@pytest.mark.asyncio
async def test_dental_casual_medical_risk_handoff_and_normal_faq(dental_processor):
    processor, _calendar = dental_processor

    pain = await processor.process(_message("болить зуб шо робити"))
    bleeding = await processor.process(_message("кров тече сильно", sender_id="patient-2"))
    faq = await processor.process(_message("де ви знаходитесь?", sender_id="patient-3"))

    assert pain["intent"] == "diagnosis_request"
    assert "діагноз" in pain["reply_text"].lower()
    assert bleeding["intent"] == "medical_emergency"
    assert "невідкладна" in bleeding["reply_text"].lower()
    assert faq["intent"] == "front_desk_contextual_answer"
    assert "Липська, 12" in faq["reply_text"]
    _assert_no_flowly_leakage(pain["reply_text"], bleeding["reply_text"], faq["reply_text"])


@pytest.mark.asyncio
async def test_dental_booking_uses_visit_terms_calendar_availability_and_required_phone(dental_processor):
    processor, calendar = dental_processor

    start = await processor.process(_message("Хочу записатися на чистку"))
    time = await processor.process(_message("у понеділок о 12:00"))
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
