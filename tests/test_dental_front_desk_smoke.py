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


class BusyAt13ConfiguredCalendarService(RecordingConfiguredCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        return start_dt.hour != 13


class BusyAt12And13ConfiguredCalendarService(RecordingConfiguredCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        return start_dt.hour not in {12, 13}


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
async def test_dental_booking_unrelated_question_has_no_flowly_leakage(dental_processor):
    processor, _calendar = dental_processor

    await processor.process(_message("Хочу записатися на чистку у вівторок о 14"))
    result = await processor.process(_message("а де ви знаходитесь?"))

    assert result["intent"] == "booking_grounded_question"
    assert result["booking_result"] is None
    assert "Липська, 12" in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


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

    requested = await processor.process(_message("хочу записатись на чистку завтра о 12"))
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

    await processor.process(_message("хочу записатись на чистку завтра о 12"))
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

    await processor.process(_message("хочу записатись на чистку завтра о 12"))
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
