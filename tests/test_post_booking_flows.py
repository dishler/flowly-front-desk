from __future__ import annotations

import os
from datetime import datetime

import pytest

os.environ.setdefault("DEBUG", "false")

from app.application.dto.normalized_message import NormalizedMessage
from app.application.services.booking_service import BookingService
from app.application.services.calendar_service import CalendarService
from app.application.services.intent_service import IntentService
from app.application.services.knowledge_service import KnowledgeService
from app.application.services.language_service import LanguageService
from app.application.services.memory_service import MemoryService
from app.application.services.message_processor import MessageProcessor
from app.application.services.reply_service import ReplyService
from app.domain.enums import IntentType


FORBIDDEN_GENERIC_CTA = "Так, це можна налаштувати, але краще спершу зрозуміти ваш процес"


class DummyDedupService:
    def __init__(self) -> None:
        self.processed = set()

    def is_duplicate(self, message_mid: str) -> bool:
        return message_mid in self.processed

    def mark_processed(self, message_mid: str) -> None:
        self.processed.add(message_mid)


class DummyOutboundService:
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
    def __init__(self, transcript: str = "") -> None:
        self.transcript = transcript

    async def transcribe_audio(self, file_url: str) -> str:
        return self.transcript


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
        return {
            "used_ai": True,
            "reply_text": "AI fallback reply: уточніть, будь ласка, що саме потрібно.",
        }


class RecordingCalendarService(CalendarService):
    def __init__(self) -> None:
        super().__init__()
        self.deleted_event_ids = []

    def delete_event(self, event_id: str) -> None:
        self.deleted_event_ids.append(event_id)


class FailingCalendarService(CalendarService):
    def delete_event(self, event_id: str) -> None:
        raise RuntimeError("delete failed")


class DummyConfiguredCalendarClient:
    def is_configured(self) -> bool:
        return True


class DefaultTestCalendarService(CalendarService):
    def __init__(self) -> None:
        super().__init__()
        self.rescheduled_events = []

    def get_available_slots(self, language: str):
        if language == "uk":
            return [
                "завтра о 11:00",
                "завтра о 15:00",
                "післязавтра о 13:00",
            ]

        return [
            "tomorrow at 11:00",
            "tomorrow at 15:00",
            "the day after tomorrow at 13:00",
        ]

    def check_specific_time_availability(
        self,
        start_dt,
        duration_minutes: int = 30,
    ) -> bool:
        return True

    def reschedule_event(self, event_id: str, start_dt, duration_minutes: int = 30) -> None:
        self.rescheduled_events.append(
            {
                "event_id": event_id,
                "start_dt": start_dt,
                "duration_minutes": duration_minutes,
            }
        )


class RecordingCreateCalendarService(CalendarService):
    def __init__(self) -> None:
        super().__init__(google_calendar_client=DummyConfiguredCalendarClient())
        self.created_descriptions = []
        self.created_events = []
        self.rescheduled_events = []

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        return True

    def create_booking_event(
        self,
        start_dt,
        duration_minutes: int = 30,
        summary: str = "Consultation call",
        description: str = "",
        attendee_emails=None,
    ):
        self.created_descriptions.append(description)
        self.created_events.append(
            {
                "start_dt": start_dt,
                "duration_minutes": duration_minutes,
                "summary": summary,
                "description": description,
                "attendee_emails": attendee_emails or [],
            }
        )

        class CreatedEvent:
            event_id = "calendar-event-created"
            html_link = "https://calendar.example/event"
            status = "confirmed"

        return CreatedEvent()

    def reschedule_event(self, event_id: str, start_dt, duration_minutes: int = 30) -> None:
        self.rescheduled_events.append(
            {
                "event_id": event_id,
                "start_dt": start_dt,
                "duration_minutes": duration_minutes,
            }
        )


class FailingCreateCalendarService(RecordingCreateCalendarService):
    def create_booking_event(
        self,
        start_dt,
        duration_minutes: int = 30,
        summary: str = "Consultation call",
        description: str = "",
        attendee_emails=None,
    ):
        raise RuntimeError("create failed")


class BusyThenAvailableCalendarService(CalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        return not (start_dt.hour == 12 and start_dt.minute == 30)


class BusyAt13CreateCalendarService(RecordingCreateCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        return not (
            start_dt.minute == 0
            and start_dt.hour in {13, 14}
        )


class BusyAt19CreateCalendarService(RecordingCreateCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        return not (start_dt.hour == 19 and start_dt.minute == 0)


class BusyUntil1430CreateCalendarService(RecordingCreateCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        return not (
            start_dt.hour == 12 and start_dt.minute == 30
            or start_dt.hour == 13 and start_dt.minute == 30
        )


@pytest.fixture
def processor_factory():
    def build(
        transcript: str = "",
        calendar_service: CalendarService | None = None,
        ai_service=None,
    ):
        memory_service = MemoryService()
        booking_service = BookingService(
            calendar_service=calendar_service or DefaultTestCalendarService(),
            language_service=LanguageService(),
        )
        reply_service = ReplyService(
            ai_service=ai_service,
            memory_service=memory_service,
            knowledge_service=KnowledgeService("tests/fixtures/flowly_knowledge_base.json"),
        )
        outbound_service = DummyOutboundService()
        processor = MessageProcessor(
            memory_service=memory_service,
            reply_service=reply_service,
            outbound_service=outbound_service,
            dedup_service=DummyDedupService(),
            intent_service=IntentService(),
            booking_service=booking_service,
            speech_service=DummySpeechService(transcript),
        )
        return processor, booking_service

    return build


def _message(text: str = "", audio_url: str | None = None) -> NormalizedMessage:
    return NormalizedMessage(
        platform="instagram",
        sender_id="user-1",
        recipient_id="bot-1",
        message_mid="",
        user_message=text,
        audio_url=audio_url,
    )


def _mark_confirmed(booking_service: BookingService, event_id: str | None = None) -> None:
    booking_service._mark_booking_completed(
        "user-1",
        start_dt=datetime(2026, 4, 27, 12, 0),
        email="client@example.com",
        phone=None,
        calendar_event_id=event_id,
    )


@pytest.mark.parametrize(
    ("text", "expected_intent", "reply_part"),
    [
        (
            "а що саме буде на дзвінку?",
            "booking_call_explanation",
            "На дзвінку ми коротко розберемо ваш кейс",
        ),
        (
            "Давайте перенесемо на 15 в понеділок",
            "booking_reschedule",
            "15:00",
        ),
        (
            "коли є вільні слоти?",
            "booking_availability_question",
            "завтра о 12:00 або 15:00",
        ),
        (
            "Скасуйте дзвінок",
            "booking_cancel",
            "скасував ваш дзвінок",
        ),
    ],
)
async def test_confirmed_booking_text_flows(processor_factory, text, expected_intent, reply_part):
    processor, booking_service = processor_factory()
    _mark_confirmed(
        booking_service,
        event_id="calendar-event-123" if expected_intent == "booking_reschedule" else None,
    )

    result = await processor.process(_message(text=text))

    assert result["intent"] == expected_intent
    assert reply_part in result["reply_text"]
    assert result["routing_category"] != "safe_handoff"
    assert result["routing_category"] != "escalate_to_human"


@pytest.mark.parametrize(
    ("transcript", "expected_intent", "reply_part"),
    [
        (
            "а що саме буде на дзвінку?",
            "booking_call_explanation",
            "На дзвінку ми коротко розберемо ваш кейс",
        ),
        (
            "Давайте перенесемо на 15 в понеділок",
            "booking_reschedule",
            "15:00",
        ),
        (
            "коли є вільні слоти?",
            "booking_availability_question",
            "завтра о 12:00 або 15:00",
        ),
        (
            "Скасуйте дзвінок",
            "booking_cancel",
            "скасував ваш дзвінок",
        ),
    ],
)
async def test_confirmed_booking_voice_flows(processor_factory, transcript, expected_intent, reply_part):
    processor, booking_service = processor_factory(transcript)
    _mark_confirmed(
        booking_service,
        event_id="calendar-event-123" if expected_intent == "booking_reschedule" else None,
    )

    result = await processor.process(_message(audio_url="https://example.test/audio.ogg"))

    assert result["intent"] == expected_intent
    assert reply_part in result["reply_text"]
    assert result["routing_category"] != "safe_handoff"
    assert result["routing_category"] != "escalate_to_human"


async def test_cancel_confirmed_booking_deletes_calendar_event(processor_factory):
    calendar_service = RecordingCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)
    _mark_confirmed(booking_service, event_id="calendar-event-123")

    result = await processor.process(_message(text="Скасуйте дзвінок"))

    assert result["intent"] == "booking_cancel"
    assert result["reply_text"] == (
        "Добре, я скасував ваш дзвінок. Якщо буде актуально — можемо запланувати інший час."
    )
    assert calendar_service.deleted_event_ids == ["calendar-event-123"]
    assert not booking_service.has_confirmed_booking("user-1")


async def test_cancel_confirmed_booking_hands_off_when_calendar_delete_fails(processor_factory):
    processor, booking_service = processor_factory(calendar_service=FailingCalendarService())
    _mark_confirmed(booking_service, event_id="calendar-event-123")

    result = await processor.process(_message(text="Скасуйте дзвінок"))

    assert result["intent"] == "booking_cancel"
    assert result["booking_result"]["status"] == "cancel_handoff"
    assert result["reply_text"] == "Добре, передам команді, щоб дзвінок скасували без зайвих дій з вашого боку."
    assert booking_service.has_confirmed_booking("user-1")


async def test_availability_suggests_slots_and_day_followup(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="коли є вільні слоти?"))

    assert result["intent"] == "booking_availability_question"
    assert "завтра о 12:00 або 15:00" in result["reply_text"]
    assert "післязавтра о 11:00 або 16:00" in result["reply_text"]

    result = await processor.process(_message(text="завтра"))

    assert result["intent"] == "booking_flow"
    assert "завтра можемо запропонувати о 12:00 або 15:00" in result["reply_text"]
    assert result["routing_category"] != "escalate_to_human"


async def test_availability_datetime_followup_asks_for_contact(processor_factory):
    processor, _ = processor_factory()
    await processor.process(_message(text="коли є вільні слоти?"))

    result = await processor.process(_message(text="завтра о 15"))

    assert result["intent"] == "booking_flow"
    assert "о 15:00 вільний" in result["reply_text"]
    assert "ваше ім’я та номер телефону або email" in result["reply_text"]


async def test_availability_time_only_followup_uses_context(processor_factory):
    processor, _ = processor_factory()
    await processor.process(_message(text="коли є вільні слоти?"))

    result = await processor.process(_message(text="15"))

    assert result["intent"] == "booking_flow"
    assert "о 15:00 вільний" in result["reply_text"]
    assert "ваше ім’я та номер телефону або email" in result["reply_text"]


async def test_empty_voice_transcription_uses_audio_retry_reply(processor_factory):
    processor, _ = processor_factory(transcript="")

    result = await processor.process(_message(audio_url="https://example.test/audio.ogg"))

    assert result["intent"] == "unrecognized_audio"
    assert result["reply_text"] == (
        "Не вдалося розпізнати аудіо. Напишіть, будь ласка, повідомлення текстом "
        "або надішліть голосове ще раз."
    )


@pytest.mark.parametrize(
    ("text", "reply"),
    [
        ("ок", "Дякую, зафіксував."),
        ("добре", "Добре, дякую."),
        ("давай", "Добре, підкажіть, будь ласка, що саме вам зручно обговорити?"),
        (
            "можливо потім",
            "Без проблем 🙂 Якщо коротко — зазвичай це економить час на обробці заявок "
            "і допомагає не втрачати клієнтів.\n\n"
            "Якщо буде актуально — можемо швидко глянути ваш кейс і зрозуміти, "
            "чи є сенс впроваджувати.",
        ),
        (
            "я подумаю",
            "Нормально, тут не треба вирішувати одразу. Бот має сенс, якщо є багато "
            "повторюваних питань, заявки губляться або менеджери довго відповідають. "
            "Де зараз найбільше ручної переписки?",
        ),
        ("це не питання це пропозиція", "Дякую, зафіксував. Передам це команді, щоб подивилися уважно."),
    ],
)
async def test_short_contextual_replies_do_not_use_complex_fallback(processor_factory, text, reply):
    processor, _ = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["reply_text"] == reply
    assert result["routing_category"] != "escalate_to_human"


async def test_business_details_after_price_prompt_call_not_slots(processor_factory):
    processor, _ = processor_factory()

    price_result = await processor.process(_message(text="Скільки коштує бот?"))
    detail_result = await processor.process(_message(text="у мене СТО, треба відповідати клієнтам"))

    assert "старт від 200$" in price_result["reply_text"]
    assert "Можу зорієнтувати точніше" in price_result["reply_text"]
    assert "дзвін" not in price_result["reply_text"].lower()
    assert "зідзвон" not in price_result["reply_text"].lower()
    assert detail_result["intent"] == "price_followup_case_details"
    assert detail_result["reply_text"] == (
        "Зрозумів, дякую за деталі. У вашому випадку це якраз можна "
        "автоматизувати — бот може приймати звернення, уточнювати деталі "
        "і записувати клієнтів у календар.\n\n"
        "Щоб підібрати оптимальне рішення під ваш процес, краще коротко "
        "обговорити це на дзвінку. Підкажіть, будь ласка, коли вам буде зручно?"
    )
    assert "Можемо запропонувати кілька варіантів" not in detail_result["reply_text"]


async def test_ok_davaite_after_price_followup_cta_starts_booking(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="Скільки коштує бот?"))
    await processor.process(_message(text="у мене СТО, треба відповідати клієнтам"))
    result = await processor.process(_message(text="ок давайте"))

    assert result["intent"] == "booking_request"
    assert "день" in result["reply_text"] and "час" in result["reply_text"]
    assert result["routing_category"] == "consultation_cta"
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_channel_question_after_price_stays_channel_answer(processor_factory):
    processor, _ = processor_factory()

    await processor.process(_message(text="скільки коштує?"))
    result = await processor.process(_message(text="це тільки інстаграм?"))

    assert result["intent"] == "channels"
    assert "Instagram" in result["reply_text"]
    assert "Telegram" in result["reply_text"]
    assert "дзвін" not in result["reply_text"].lower()


async def test_soft_followup_decline_does_not_start_booking(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="не зараз"))

    assert result["reply_text"] == (
        "Без проблем 🙂 Якщо коротко — зазвичай це економить час на обробці заявок "
        "і допомагає не втрачати клієнтів.\n\n"
        "Якщо буде актуально — можемо швидко глянути ваш кейс і зрозуміти, "
        "чи є сенс впроваджувати."
    )
    assert result["routing_category"] == "answered_basic"
    assert result["booking_result"] is None


async def test_inactive_waiting_for_time_uses_normal_conversation(processor_factory):
    processor, booking_service = processor_factory()
    booking_service._save_pending_confirmation(
        "user-1",
        {
            "state": "WAITING_FOR_TIME",
            "language": "uk",
            "duration_minutes": 30,
            "summary": "Consultation call",
            "description": "Booked via Flowly Meta Bot",
        },
    )

    price_result = await processor.process(_message(text="вітаю, скільки коштує бот?"))

    assert price_result["intent"] == "price"
    assert "старт від 200$" in price_result["reply_text"]
    assert "Підкажіть, будь ласка, точний день і час." not in price_result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_price_question_skolko_ce_koshtue(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="Скільки це коштує?"))

    assert result["intent"] == "price"
    assert "старт від 200$" in result["reply_text"]
    assert "Можу зорієнтувати точніше" in result["reply_text"]
    assert "дзвін" not in result["reply_text"].lower()
    assert "зідзвон" not in result["reply_text"].lower()


@pytest.mark.parametrize("text", ["ціна", "скільки коштує?", "шо по ціні", "таке скільки стоїть?"])
async def test_first_price_reply_uses_soft_dm_context_request_without_call_cta(processor_factory, text):
    processor, _ = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["intent"] == "price"
    assert "старт від 200$" in result["reply_text"]
    assert "Можу зорієнтувати точніше" in result["reply_text"]
    assert "дзвінок" not in result["reply_text"].lower()
    assert "дзвінку" not in result["reply_text"].lower()
    assert "зідзвон" not in result["reply_text"].lower()
    assert "обговорити на дзвінку" not in result["reply_text"].lower()


async def test_active_waiting_for_time_stays_in_booking_for_unrelated_question(processor_factory):
    processor, booking_service = processor_factory()

    start_result = await processor.process(_message(text="давайте дзвінок"))
    price_result = await processor.process(_message(text="вітаю, скільки коштує бот?"))

    assert start_result["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert price_result["intent"] == "booking_product_question"
    assert "старт від 200$" in price_result["reply_text"]
    assert "Для дзвінка підкажіть" not in price_result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_waiting_for_time_still_accepts_normal_booking_time(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="давайте дзвінок"))
    result = await processor.process(_message(text="завтра о 15"))

    assert result["intent"] == "booking_flow"
    assert "о 15:00 вільний" in result["reply_text"]
    assert "ваше ім’я та номер телефону або email" in result["reply_text"]
    assert booking_service._get_pending_confirmation("user-1")["is_active"] is True
    assert booking_service._get_pending_confirmation("user-1")["step"] == "WAITING_FOR_CONTACT"
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_booking_with_weekday_without_time_asks_only_for_time(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="хочу записатись у вівторок"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "вівторок" in result["reply_text"].lower()
    assert "на котру годину" in result["reply_text"].lower()
    assert "який день" not in result["reply_text"].lower()
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["requested_date"]
    assert pending["requested_day_label"] == "вівторок"
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_booking_with_service_text_and_weekday_without_time_asks_only_for_time(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="хочу записатись на чистку у вівторок"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "вівторок" in result["reply_text"].lower()
    assert "на котру годину" in result["reply_text"].lower()
    assert "який день" not in result["reply_text"].lower()
    assert booking_service._get_pending_confirmation("user-1")["requested_day_label"] == "вівторок"


async def test_waiting_for_time_impatient_weekday_update_asks_only_for_time(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="давайте дзвінок"))
    result = await processor.process(_message(text="я ж сказав, на вівторок"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "вівторок" in result["reply_text"].lower()
    assert "на котру годину" in result["reply_text"].lower()
    assert "який день" not in result["reply_text"].lower()
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["requested_day_label"] == "вівторок"
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_waiting_for_time_neutral_followup_preserves_requested_date(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="хочу записатись у вівторок"))
    original_pending = booking_service._get_pending_confirmation("user-1")
    original_requested_date = original_pending["requested_date"]
    result = await processor.process(_message(text="ну я вже написав"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "на котру годину" in result["reply_text"].lower()
    assert "який день" not in result["reply_text"].lower()
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["requested_date"] == original_requested_date
    assert pending["requested_day_label"] == "вівторок"
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_waiting_for_time_combines_saved_weekday_with_time(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="хочу записатись у вівторок"))
    result = await processor.process(_message(text="о 15"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "о 15:00 вільний" in result["reply_text"]
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["step"] == "WAITING_FOR_CONTACT"
    assert pending["start_dt"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_booking_with_weekday_and_time_keeps_existing_booking_path(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="хочу записатись у вівторок о 15"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "о 15:00 вільний" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_booking_date_correction_overrides_tuesday_with_wednesday(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="хочу записатись у вівторок"))
    result = await processor.process(_message(text="ні, краще в середу"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert result["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert "серед" in result["reply_text"].lower()
    assert "який день" not in result["reply_text"].lower()
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["requested_day_label"] == "середу"
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_booking_date_correction_handles_meant_friday(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="хочу записатись у вівторок"))
    result = await processor.process(_message(text="я мав на увазі п'ятницю"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "п’ятниц" in result["reply_text"].lower()
    assert booking_service._get_pending_confirmation("user-1")["requested_day_label"] == "п’ятницю"


async def test_booking_date_correction_preserves_existing_time(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="хочу записатись у вівторок о 14"))
    result = await processor.process(_message(text="ні, краще в середу"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    pending = booking_service._get_pending_confirmation("user-1")
    start_dt = datetime.fromisoformat(pending["start_dt"])
    assert start_dt.weekday() == 2
    assert start_dt.hour == 14
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_booking_time_correction_overrides_existing_time(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="хочу записатись у вівторок о 14"))
    result = await processor.process(_message(text="не о 14, а о 16"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    pending = booking_service._get_pending_confirmation("user-1")
    start_dt = datetime.fromisoformat(pending["start_dt"])
    assert start_dt.weekday() == 1
    assert start_dt.hour == 16
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_booking_time_correction_preserves_existing_date(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="хочу записатись у середу о 14"))
    result = await processor.process(_message(text="не о 14, а о 16"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    start_dt = datetime.fromisoformat(
        booking_service._get_pending_confirmation("user-1")["start_dt"]
    )
    assert start_dt.weekday() == 2
    assert start_dt.hour == 16


async def test_booking_rejection_without_replacement_still_cancels(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="хочу записатись у вівторок"))
    result = await processor.process(_message(text="ні"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "cancelled"
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_waiting_for_time_1230_asks_for_name_and_contact(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="давайте дзвінок"))
    result = await processor.process(_message(text="завтра 12:30"))

    assert result["intent"] == "booking_flow"
    assert "12:30 вільний" in result["reply_text"]
    assert "ваше ім’я та номер телефону або email" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_service_question_does_not_trigger_stale_waiting_for_contact(processor_factory):
    processor, booking_service = processor_factory()
    booking_service._save_pending_confirmation(
        "user-1",
        {
            "state": "WAITING_FOR_CONTACT",
            "language": "uk",
            "duration_minutes": 30,
            "summary": "Consultation call",
            "description": "Booked via Flowly Meta Bot",
            "source_channel": "instagram",
            "context_summary": "old stale context",
        },
    )

    result = await processor.process(_message(text="Привіт, що це у вас за сервіс?"))

    assert result["intent"] != "booking_flow"
    assert "Дякую, ім’я зафіксував" not in result["reply_text"]
    assert "залиште" not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_active_waiting_for_contact_faq_stays_in_booking_state(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="давайте дзвінок"))
    await processor.process(_message(text="завтра о 15"))
    result = await processor.process(_message(text="Привіт, що це у вас за сервіс?"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "booking_unrelated_question"
    assert "Дякую, ім’я зафіксував" not in result["reply_text"]
    assert "Щоб продовжити підтвердження дзвінка" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_active_waiting_for_contact_still_accepts_contact(processor_factory):
    processor, _ = processor_factory()

    await processor.process(_message(text="давайте дзвінок"))
    await processor.process(_message(text="завтра о 15"))
    result = await processor.process(_message(text="Іван 0987121328"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] in {"confirmed", "manual_followup"}


async def test_booking_collects_name_and_writes_calendar_description(processor_factory):
    calendar_service = RecordingCreateCalendarService()
    processor, _ = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="Давайте дзвінок"))
    time_result = await processor.process(_message(text="Завтра о 12"))
    contact_result = await processor.process(_message(text="Іван 0987121328"))

    assert "ваше ім’я та номер телефону або email" in time_result["reply_text"]
    assert contact_result["booking_result"]["status"] == "confirmed"
    assert contact_result["booking_result"]["event_created"] is True
    assert contact_result["booking_result"]["event_id"] == "calendar-event-created"
    assert contact_result["booking_result"]["customer_name"] == "Іван"
    assert calendar_service.created_descriptions
    assert len(calendar_service.created_events) == 1
    description = calendar_service.created_descriptions[0]
    assert "Booked via Flowly Meta Bot" in description
    assert "Customer name: Іван" in description
    assert "Sender ID: user-1" in description
    assert "Source: instagram" in description
    assert "Context: Завтра о 12" in description
    assert "Contact: Phone: 0987121328" in description


async def test_booking_contact_only_asks_for_name(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="Давайте дзвінок"))
    await processor.process(_message(text="Завтра о 12"))
    result = await processor.process(_message(text="0987121328"))

    assert result["booking_result"]["status"] == "waiting_for_name"
    assert result["reply_text"] == "Дякую. А підкажіть, будь ласка, ваше ім’я?"
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_booking_name_only_asks_for_contact(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="Давайте дзвінок"))
    await processor.process(_message(text="Завтра о 12"))
    result = await processor.process(_message(text="Іван"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["reply_text"] == "Дякую, Іван. А підкажіть, будь ласка, номер телефону або email?"
    assert "ім’я зафіксував" not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_booking_with_contact_in_initial_message(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="Давайте дзвінок завтра о 12 Іван 0991234567"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "manual_followup"
    assert result["booking_result"]["reply_text"] == (
        "Супер, зафіксували ваш запит 🙌 Ми зв’яжемося з вами, щоб підтвердити час"
    )
    assert "підтвердили дзвінок" not in result["booking_result"]["reply_text"]
    assert result["booking_result"]["event_created"] is False
    assert not booking_service.has_confirmed_booking("user-1")
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_booking_with_contact_in_initial_message_creates_calendar_event_when_configured(processor_factory):
    calendar_service = RecordingCreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    result = await processor.process(_message(text="Давайте дзвінок завтра о 12 Іван 0991234567"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "confirmed"
    assert result["booking_result"]["event_created"] is True
    assert result["booking_result"]["event_id"] == "calendar-event-created"
    assert "підтвердили дзвінок" in result["booking_result"]["reply_text"]
    assert booking_service.has_confirmed_booking("user-1")
    assert len(calendar_service.created_events) == 1


async def test_booking_manual_followup_when_calendar_not_configured_does_not_confirm(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="Давайте дзвінок"))
    await processor.process(_message(text="Завтра о 12"))
    result = await processor.process(_message(text="Іван 0987121328"))

    assert result["booking_result"]["status"] == "manual_followup"
    assert result["booking_result"]["event_created"] is False
    assert result["booking_result"]["reply_text"] == (
        "Супер, зафіксували ваш запит 🙌 Ми зв’яжемося з вами, щоб підтвердити час"
    )
    assert "підтвердили дзвінок" not in result["booking_result"]["reply_text"]
    assert not booking_service.has_confirmed_booking("user-1")
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_booking_manual_followup_when_calendar_create_fails(processor_factory):
    calendar_service = FailingCreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="Давайте дзвінок"))
    await processor.process(_message(text="Завтра о 12"))
    result = await processor.process(_message(text="Іван 0987121328"))

    assert result["booking_result"]["status"] == "manual_followup"
    assert result["booking_result"]["event_created"] is False
    assert result["booking_result"]["reply_text"] == (
        "Супер, зафіксували ваш запит 🙌 Ми зв’яжемося з вами, щоб підтвердити час"
    )
    assert not booking_service.has_confirmed_booking("user-1")


async def test_booking_slot_suggestion_and_confirmation(processor_factory):
    processor, booking_service = processor_factory(calendar_service=BusyThenAvailableCalendarService())

    # Request a busy slot
    result1 = await processor.process(_message(text="давай дзвінок завтра 12:30"))
    assert result1["booking_result"]["status"] == "slot_suggested"
    assert "Як щодо" in result1["booking_result"]["reply_text"]

    # Confirm with "ок"
    result2 = await processor.process(_message(text="ок"))
    assert result2["intent"] == "booking_flow"
    assert result2["booking_result"]["status"] == "waiting_for_contact"

    # Provide contact
    result3 = await processor.process(_message(text="Іван 0991234567"))
    assert result3["booking_result"]["status"] == "manual_followup"
    assert result3["booking_result"]["event_created"] is False
    assert "зафіксували ваш запит" in result3["booking_result"]["reply_text"]
    assert not booking_service.has_confirmed_booking("user-1")


@pytest.mark.parametrize("text", ["так", "ок", "давайте"])
async def test_suggested_slot_acceptance_variants_still_work(processor_factory, text):
    calendar_service = BusyUntil1430CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="Давайте дзвінок"))
    suggested = await processor.process(_message(text="Завтра 12:30"))
    accepted = await processor.process(_message(text=text))

    assert suggested["booking_result"]["status"] == "slot_suggested"
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert "бронюємо" in accepted["reply_text"]
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["customer_name"] is None
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


@pytest.mark.parametrize("text", ["інший час", "давайте інший"])
async def test_suggested_slot_another_time_is_not_contact_name(processor_factory, text):
    calendar_service = BusyUntil1430CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="Давайте дзвінок"))
    suggested = await processor.process(_message(text="Завтра 12:30"))
    another = await processor.process(_message(text=text))

    assert suggested["booking_result"]["status"] == "slot_suggested"
    assert another["booking_result"]["status"] == "waiting_for_time"
    assert "інший час" in another["reply_text"]
    assert "номер телефону" not in another["reply_text"]
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["customer_name"] is None
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_confirmation_after_suggested_slot_is_not_customer_name(processor_factory):
    calendar_service = BusyUntil1430CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="Давайте дзвінок"))
    suggested = await processor.process(_message(text="Завтра 12:30"))
    accepted = await processor.process(_message(text="так"))

    assert suggested["booking_result"]["status"] == "slot_suggested"
    assert "завтра о 14:30" in suggested["reply_text"]
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert accepted["reply_text"] == (
        "Супер, тоді бронюємо завтра о 14:30. "
        "Залиште, будь ласка, ваше ім’я та номер телефону або email."
    )
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["customer_name"] is None


async def test_phone_only_after_suggested_slot_acceptance_asks_for_name(processor_factory):
    calendar_service = BusyUntil1430CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="Давайте дзвінок"))
    await processor.process(_message(text="Завтра 12:30"))
    await processor.process(_message(text="так"))
    result = await processor.process(_message(text="0987121328"))

    assert result["booking_result"]["status"] == "waiting_for_name"
    assert result["reply_text"] == "Дякую. А підкажіть, будь ласка, ваше ім’я?"
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["customer_name"] is None
    assert pending["contact_phone"] == "0987121328"


async def test_suggested_slot_acceptance_phone_then_name_confirms_with_calendar(processor_factory):
    calendar_service = BusyUntil1430CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="Давайте дзвінок"))
    await processor.process(_message(text="Завтра 12:30"))
    await processor.process(_message(text="так"))
    await processor.process(_message(text="0987121328"))
    result = await processor.process(_message(text="Іван"))

    assert result["booking_result"]["status"] == "confirmed"
    assert result["booking_result"]["event_created"] is True
    assert result["booking_result"]["event_id"] == "calendar-event-created"
    assert result["reply_text"] == (
        "Супер, Іван, підтвердили дзвінок на завтра о 14:30 🙌 "
        "Зв’яжемося з вами у цей час."
    )
    assert booking_service.has_confirmed_booking("user-1")
    assert len(calendar_service.created_events) == 1


async def test_typo_confirmation_after_suggested_slot_is_not_customer_name(processor_factory):
    calendar_service = BusyUntil1430CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="Давайте дзвінок"))
    await processor.process(_message(text="Завтра 12:30"))
    accepted = await processor.process(_message(text="піідходить"))

    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["customer_name"] is None


async def test_compound_confirmation_after_suggested_slot_is_not_customer_name(processor_factory):
    calendar_service = BusyAt13CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="гаразд мені цікаво"))
    suggested = await processor.process(_message(text="давай завтра о 13"))
    accepted = await processor.process(_message(text="так давай"))

    assert suggested["booking_result"]["status"] == "slot_suggested"
    assert "завтра о 15:00" in suggested["reply_text"]
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert "ім’я зафіксував" not in accepted["reply_text"]
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["customer_name"] is None
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_contact_with_question_and_explicit_name_confirms_booking(processor_factory):
    calendar_service = BusyAt13CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="гаразд мені цікаво"))
    await processor.process(_message(text="давай завтра о 13"))
    await processor.process(_message(text="так давай"))
    result = await processor.process(_message(text="Яке імя? Мене звати Дмитро 0987121328"))

    assert result["booking_result"]["status"] == "confirmed"
    assert result["booking_result"]["customer_name"] == "Дмитро"
    assert result["booking_result"]["contact_phone"] == "0987121328"
    assert "Дмитро" in result["reply_text"]
    assert "завтра о 15:00" in result["reply_text"]
    assert booking_service.has_confirmed_booking("user-1")


async def test_booking_status_question_while_waiting_for_contact_asks_for_contact(processor_factory):
    calendar_service = BusyAt13CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="гаразд мені цікаво"))
    await processor.process(_message(text="давай завтра о 13"))
    await processor.process(_message(text="так давай"))
    result = await processor.process(_message(text="поставив дзвінок?"))

    assert result["booking_result"]["status"] == "booking_pending_contact_status_question"
    assert "Ще ні" in result["reply_text"]
    assert "ваше ім’я та номер телефону або email" in result["reply_text"]
    assert "AI-бот для месенджерів" not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_booking_status_question_after_confirmation_does_not_start_new_booking(processor_factory):
    calendar_service = BusyAt13CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="гаразд мені цікаво"))
    await processor.process(_message(text="давай завтра о 13"))
    await processor.process(_message(text="так давай"))
    await processor.process(_message(text="Яке імя? Мене звати Дмитро 0987121328"))
    result = await processor.process(_message(text="поставив дзвінок?"))

    assert result["intent"] == "booking_status_confirmed"
    assert "дзвінок підтверджено" in result["reply_text"]
    assert "завтра о 15:00" in result["reply_text"]
    assert "точний день і час" not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_product_question_during_waiting_for_contact_gets_answer_and_keeps_booking(processor_factory):
    calendar_service = BusyAt13CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="гаразд мені цікаво"))
    await processor.process(_message(text="давай завтра о 13"))
    await processor.process(_message(text="так давай"))
    result = await processor.process(_message(text="цікаво, як довго займає впроваадження?"))

    assert result["intent"] == "booking_product_question"
    assert "7-10 днів" in result["reply_text"]
    assert "AI-бот для месенджерів" not in result["reply_text"]
    assert "ваше ім’я та номер телефону або email" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["customer_name"] is None


async def test_interest_word_during_waiting_for_contact_is_not_saved_as_name(processor_factory):
    calendar_service = BusyAt13CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="гаразд мені цікаво"))
    await processor.process(_message(text="давай завтра о 13"))
    await processor.process(_message(text="так давай"))
    result = await processor.process(_message(text="цікаво"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "ім’я зафіксував" not in result["reply_text"]
    pending = booking_service._get_pending_confirmation("user-1")
    assert pending["customer_name"] is None
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_name_only_during_booking_asks_for_contact_with_name(processor_factory):
    calendar_service = RecordingCreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="давайте кол"))
    await processor.process(_message(text="завтра 12:00"))
    result = await processor.process(_message(text="Дмитро"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["reply_text"] == "Дякую, Дмитро. А підкажіть, будь ласка, номер телефону або email?"
    assert "ім’я зафіксував" not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_availability_question_while_waiting_for_time_returns_slots(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="давайте кол"))
    result = await processor.process(_message(text="коли є слоти?"))

    assert result["intent"] == "booking_availability_question"
    assert "завтра" in result["reply_text"]
    assert "AI-бот для месенджерів" not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("що бот буде питати в клієнта?", "ім’я"),
        ("а ціни на ремонт він може рахувати?", "Точну вартість ремонту"),
        ("це дорого", "мінімального сценарію"),
    ],
)
async def test_sales_edge_questions_get_specific_replies(processor_factory, text, expected):
    processor, _ = processor_factory()

    result = await processor.process(_message(text=text))

    assert expected in result["reply_text"]
    assert "уточнити деталі" not in result["reply_text"]


async def test_price_objection_has_no_aggressive_call_cta(processor_factory):
    processor, booking_service = processor_factory()

    objection = await processor.process(_message(text="це дорого"))
    accepted = await processor.process(_message(text="давайте"))

    assert "мінімального сценарію" in objection["reply_text"]
    assert "дзвін" not in objection["reply_text"].lower()
    assert accepted["intent"] == "contextual_short_reply"
    assert "який у вас бізнес" in accepted["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_price_objection_nu_ok_keeps_human_context_followup(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="що це за сервіс?"))
    await processor.process(_message(text="поясни без дзвінка"))
    await processor.process(_message(text="а скільки коштує?"))
    await processor.process(_message(text="дорого"))
    result = await processor.process(_message(text="ну ок"))

    assert result["intent"] == "contextual_short_reply"
    assert "який у вас бізнес" in result["reply_text"]
    assert result["routing_category"] != "safe_handoff"
    assert result["routing_category"] != "escalate_to_human"
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_price_objection_rejection_does_not_start_booking(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="це дорого"))
    result = await processor.process(_message(text="не треба"))

    assert result["intent"] == "rejection"
    assert "Зрозумів, дякую" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_niche_reply_does_not_promise_chat_scenario_and_scenario_acceptance_starts_booking(
    processor_factory,
):
    processor, booking_service = processor_factory()

    niche = await processor.process(_message(text="в мене сто"))
    accepted = await processor.process(_message(text="так даавай сценаарій під наш сервіс"))

    assert "прикинути сценарій" not in niche["reply_text"]
    assert "сценарій у чаті" in niche["reply_text"]
    assert "спеціалістом на дзвінку" in niche["reply_text"]
    assert accepted["intent"] == "booking_request"
    assert "спеціалістом" in accepted["reply_text"]
    assert "день" in accepted["reply_text"] and "час" in accepted["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_channel_buying_signal_gets_sales_reply_not_channel_only(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="цікавить бот для телеграм і інстаграм"))

    assert result["intent"] == "buying_signal"
    assert "можемо допомогти" in result["reply_text"].lower()
    assert "Зараз ми фокусуємось" not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_exact_final_dialogue_flow_from_auto_service_to_confirmed_call(processor_factory):
    calendar_service = BusyAt19CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="хай, що ви пропонуєте?"))
    niche = await processor.process(_message(text="в мене сто"))
    scenario_acceptance = await processor.process(_message(text="так даавай сценаарій під наш сервіс"))
    channel_interest = await processor.process(_message(text="цікавить бот для телеграм і інстаграм"))
    price = await processor.process(_message(text="гаразд скільки коштує"))
    objection = await processor.process(_message(text="дорого"))
    accepted = await processor.process(_message(text="ну ок"))
    requested = await processor.process(_message(text="завтра о 19"))
    slot_accepted = await processor.process(_message(text="давай"))
    confirmed = await processor.process(_message(text="Діма, 0987121322"))

    assert "прикинути сценарій" not in niche["reply_text"]
    assert "спеціалістом" in scenario_acceptance["reply_text"]
    assert "день" in scenario_acceptance["reply_text"] and "час" in scenario_acceptance["reply_text"]
    assert channel_interest["intent"] == "booking_product_question"
    assert "Для дзвінка підкажіть" in channel_interest["reply_text"]
    assert price["intent"] == "booking_product_question"
    assert "старт від 200$" in price["reply_text"]
    assert "мінімального сценарію" in objection["reply_text"]
    assert "спеціалістом" in accepted["reply_text"]
    assert "день" in accepted["reply_text"] and "час" in accepted["reply_text"]
    assert requested["booking_result"]["status"] == "slot_suggested"
    assert "завтра о 20:00" in requested["reply_text"]
    assert slot_accepted["booking_result"]["status"] == "waiting_for_contact"
    assert confirmed["booking_result"]["status"] == "confirmed"
    assert confirmed["booking_result"]["customer_name"] == "Діма"
    assert confirmed["booking_result"]["contact_phone"] == "0987121322"
    assert "Супер, Діма" in confirmed["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("що бот буде питати в клієнта?", "ім’я"),
        ("а ціни на ремонт він може рахувати?", "Точну вартість ремонту"),
        ("а якщо в нас 3 філії і різні адміністратори?", "передавати заявку потрібному адміністратору"),
    ],
)
async def test_sales_edge_questions_after_long_dialogue_still_get_specific_replies(
    processor_factory,
    text,
    expected,
):
    processor, _ = processor_factory()

    for setup_text in [
        "Привіт",
        "чим ви займаєтесь?",
        "з якими напрямками працюєте?",
        "для СТО підходить?",
        "так підкажи, ми сто",
        "ми стоматологія",
    ]:
        await processor.process(_message(text=setup_text))

    result = await processor.process(_message(text=text))

    assert expected in result["reply_text"]
    assert "уточнити деталі" not in result["reply_text"]
    assert "як працює бот, для яких бізнесів" not in result["reply_text"]


async def test_confirmed_booking_datetime_followup_reschedules(processor_factory):
    calendar_service = RecordingCreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="давайте кол"))
    await processor.process(_message(text="завтра 12:00"))
    await processor.process(_message(text="Дмитро 0987121329"))
    await processor.process(_message(text="хочу перенести дзвінок"))
    result = await processor.process(_message(text="післязавтра 15:00"))

    assert result["intent"] == "booking_reschedule"
    assert result["booking_result"]["status"] == "rescheduled"
    assert "перенесли на післязавтра о 15:00" in result["reply_text"]
    assert calendar_service.rescheduled_events
    assert calendar_service.rescheduled_events[0]["event_id"] == "calendar-event-created"
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_confirmed_booking_fresh_booking_intent_starts_new_booking_without_reschedule(
    processor_factory,
):
    calendar_service = RecordingCreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="давайте кол"))
    await processor.process(_message(text="завтра 14:00"))
    await processor.process(_message(text="Дмитро 0987121329"))
    existing_created_count = len(calendar_service.created_events)

    result = await processor.process(_message(text="хочу записатись на чистку завтра о 12"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "перенесли" not in result["reply_text"]
    assert "залиште" in result["reply_text"].lower()
    assert len(calendar_service.created_events) == existing_created_count
    assert calendar_service.rescheduled_events == []
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_confirmed_booking_explicit_reschedule_still_updates_existing_booking(
    processor_factory,
):
    calendar_service = RecordingCreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="давайте кол"))
    await processor.process(_message(text="завтра 14:00"))
    await processor.process(_message(text="Дмитро 0987121329"))
    existing_created_count = len(calendar_service.created_events)

    result = await processor.process(_message(text="хочу перенести запис на завтра о 12"))

    assert result["intent"] == "booking_reschedule"
    assert result["booking_result"]["status"] == "rescheduled"
    assert "перенесли на завтра о 12:00" in result["reply_text"]
    assert len(calendar_service.created_events) == existing_created_count
    assert calendar_service.rescheduled_events
    assert calendar_service.rescheduled_events[0]["event_id"] == "calendar-event-created"
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_confirmed_booking_fresh_booking_after_faq_messages(processor_factory):
    calendar_service = RecordingCreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="давайте кол"))
    await processor.process(_message(text="завтра 14:00"))
    await processor.process(_message(text="Дмитро 0987121329"))
    status = await processor.process(_message(text="поставив дзвінок?"))
    faq = await processor.process(_message(text="а скільки коштує?"))
    existing_created_count = len(calendar_service.created_events)

    result = await processor.process(_message(text="хочу записатись на чистку завтра о 12"))

    assert status["intent"] == "booking_status_confirmed"
    assert faq["booking_result"] is None
    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "перенесли" not in result["reply_text"]
    assert len(calendar_service.created_events) == existing_created_count
    assert calendar_service.rescheduled_events == []
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_language_request_does_not_answer_in_russian(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="а можно на русском?"))

    assert result["intent"] == "language_request"
    assert result["reply_text"] == "Можу відповідати українською або англійською."


async def test_harsh_ukrainian_slang_gets_frustrated_recovery(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="шо за херня"))

    assert result["intent"] == "frustrated"
    assert "відповідь була не зовсім по суті" in result["reply_text"]


async def test_email_with_name_after_booking_product_question_confirms(processor_factory):
    calendar_service = BusyAt13CreateCalendarService()
    processor, booking_service = processor_factory(calendar_service=calendar_service)

    await processor.process(_message(text="гаразд мені цікаво"))
    await processor.process(_message(text="давай завтра о 13"))
    await processor.process(_message(text="так давай"))
    await processor.process(_message(text="цікаво, як довго займає впроваадження?"))
    await processor.process(_message(text="цікаво"))
    result = await processor.process(_message(text="Дмитро dishler@gmail.com"))

    assert result["booking_result"]["status"] == "confirmed"
    assert result["booking_result"]["customer_name"] == "Дмитро"
    assert result["booking_result"]["contact_email"] == "dishler@gmail.com"
    assert "Дмитро" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_service_question_for_whom(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="А для кого це?"))

    assert result["intent"] == "industries"
    assert result["routing_category"] == "answered_basic"
    assert "Для кого це?" not in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]
    assert "сервісних бізнесів" in result["reply_text"]


async def test_service_question_what_does_bot_do(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="Що конкретно робить бот?"))

    assert result["intent"] == "service_description"
    assert result["routing_category"] == "answered_basic"
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]
    assert ("працює" in result["reply_text"] or "AI-бот" in result["reply_text"] or "Як це" in result["reply_text"])


async def test_service_question_what_does_your_bot_do_returns_explanation_not_cta(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="Що робить ваш бот?"))

    assert result["intent"] == "service_description"
    assert result["routing_category"] == "answered_basic"
    assert "Ми налаштовуємо AI-бота для Instagram/Facebook/Telegram/WhatsApp/Viber." in result["reply_text"]
    assert "типові повідомлення" in result["reply_text"]
    assert "що варто автоматизувати першим" in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_service_question_what_is_included(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="Що входить у сервіс?"))

    assert result["intent"] == "service_description"
    assert result["routing_category"] == "answered_basic"
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]
    assert ("входить" in result["reply_text"] or "аудит" in result["reply_text"])


async def test_offer_question_routes_to_service_description_not_fallback(processor_factory):
    ai_service = RecordingAIService()
    processor, _ = processor_factory(ai_service=ai_service)

    result = await processor.process(_message(text="хай, що ви пропонуєте?"))

    assert result["intent"] == "service_description"
    assert "AI-бота" in result["reply_text"]
    assert "уточніть" not in result["reply_text"].lower()
    assert not ai_service.calls


@pytest.mark.parametrize(
    "text",
    [
        "привіт чим займається ваша компанія",
        "привіт що ви робите",
        "доброго дня, хочу зрозуміти що за бот",
        "можете коротко пояснити ваш сервіс?",
    ],
)
async def test_common_first_service_questions_do_not_use_ai_fallback(processor_factory, text):
    ai_service = RecordingAIService()
    processor, _ = processor_factory(ai_service=ai_service)

    result = await processor.process(_message(text=text))

    assert result["intent"] in {"service_description", "general_question"}
    assert "AI fallback" not in result["reply_text"]
    assert "уточніть" not in result["reply_text"].lower()
    assert ("AI-бот" in result["reply_text"] or "AI-ботів" in result["reply_text"])
    assert not ai_service.calls


async def test_greeting_then_service_question_does_not_repeat_intro(processor_factory):
    processor, _ = processor_factory()

    greeting = await processor.process(_message(text="Привіт"))
    service = await processor.process(_message(text="Чим ви займаєтесь?"))

    assert "Привіт!" in greeting["reply_text"]
    assert service["intent"] == "service_followup"
    assert "По суті, ми автоматизуємо переписки" in service["reply_text"]
    assert "Привіт!" not in service["reply_text"]
    assert service["reply_text"] != greeting["reply_text"]


async def test_manual_intro_details_channel_and_explain_flow_stays_contextual(processor_factory):
    processor, _ = processor_factory()

    await processor.process(_message(text="Привіт"))
    service = await processor.process(_message(text="Чим ви займаєтесь?"))
    details = await processor.process(_message(text="Цікаво, розкжи детальніше"))
    channel = await processor.process(_message(text="Instagram"))
    explain = await processor.process(_message(text="поясни ти ж написав"))

    assert service["intent"] == "service_followup"
    assert details["intent"] == "more_details"
    assert "можна без дзвінка" not in details["reply_text"]
    assert "першу лінію переписки" in details["reply_text"]
    assert "збирає контакт або бажаний час" in details["reply_text"]
    assert "який сценарій краще запускати першим" in details["reply_text"]
    assert channel["intent"] == "channels"
    assert "Працюємо з Instagram" in channel["reply_text"]
    assert "де вже є найбільше звернень" in channel["reply_text"]
    assert "Зараз ми фокусуємось" not in channel["reply_text"]
    assert explain["intent"] == "more_details"
    assert explain["routing_category"] != "safe_handoff"
    assert explain["routing_category"] != "escalate_to_human"


async def test_price_instagram_short_replies_and_bot_question_stay_contextual(processor_factory):
    processor, booking_service = processor_factory()

    price = await processor.process(_message(text="ціна"))
    channel = await processor.process(_message(text="інстаграм"))
    yes = await processor.process(_message(text="так"))
    uhuh = await processor.process(_message(text="угу"))
    identity = await processor.process(_message(text="ти бот?"))

    assert price["intent"] == "price"
    assert "старт від 200$" in price["reply_text"]
    assert channel["intent"] == "channel_context_followup"
    assert "починати саме з Instagram" in channel["reply_text"]
    assert "Що найчастіше пишуть клієнти" in channel["reply_text"]
    assert yes["intent"] == "contextual_affirmation"
    assert "для Instagram логічно почати" in yes["reply_text"]
    assert uhuh["intent"] == "contextual_affirmation"
    assert "найпростішого Instagram-сценарію" in uhuh["reply_text"]
    assert identity["intent"] == "bot_identity_question"
    assert "AI-асистент Flowly" in identity["reply_text"]
    assert "Хочу правильно зрозуміти" not in channel["reply_text"]
    assert "Тут краще коротко уточнити" not in yes["reply_text"]
    assert "Тут краще коротко уточнити" not in uhuh["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_business_channels_context_flow_moves_to_booking_without_channel_faq(processor_factory):
    processor, booking_service = processor_factory()

    start = await processor.process(_message(text="Супер"))
    context = await processor.process(_message(text="для сто інстаграм фейсбук"))
    continuation = await processor.process(_message(text="і що?"))
    impatient = await processor.process(_message(text="ну я ж написав що далі?"))
    interest = await processor.process(_message(text="цікаво"))
    booking = await processor.process(_message(text="давай кол"))
    slot = await processor.process(_message(text="завтра о 19"))

    assert start["intent"] == "contextual_short_reply"
    assert context["intent"] == "business_channel_context"
    assert "для СТО в Instagram/Facebook" in context["reply_text"]
    assert "ціни, запис, уточнення авто" in context["reply_text"]
    assert "Зараз ми фокусуємось" not in context["reply_text"]
    assert continuation["intent"] == "business_channel_followup"
    assert "Instagram/Facebook" in continuation["reply_text"]
    assert "уточнює авто" in continuation["reply_text"]
    assert "структуровану заявку" in continuation["reply_text"]
    assert "Хочу правильно зрозуміти" not in continuation["reply_text"]
    assert impatient["intent"] == "business_channel_followup"
    assert "Так, бачу: СТО, Instagram і Facebook" in impatient["reply_text"]
    assert "Хочу правильно зрозуміти" not in impatient["reply_text"]
    assert interest["intent"] == "business_channel_interest"
    assert "нормальний кейс для запуску" in interest["reply_text"]
    assert "Instagram/Facebook" in interest["reply_text"]
    assert "куди передавати заявку" in interest["reply_text"]
    assert booking["intent"] == "booking_request"
    assert booking["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert slot["intent"] == "booking_flow"
    assert "19:00" in slot["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_CONTACT"


async def test_repeated_fallback_uses_context_instead_of_looping(processor_factory):
    processor, _ = processor_factory()

    await processor.process(_message(text="ціна"))
    await processor.process(_message(text="інстаграм"))
    first_unclear = await processor.process(_message(text="???"))
    second_unclear = await processor.process(_message(text="???"))

    assert "Хочу правильно зрозуміти" in first_unclear["reply_text"]
    assert "Хочу правильно зрозуміти" not in second_unclear["reply_text"]
    assert "як це працює саме для Instagram" in second_unclear["reply_text"]


@pytest.mark.parametrize(
    "text",
    [
        "привіт мені цікаве впровадження бота",
        "потрібен бот для інстаграму",
    ],
)
async def test_buying_signal_gets_sales_reply_not_channel_only_reply(processor_factory, text):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["intent"] == "buying_signal"
    assert "можемо допомогти" in result["reply_text"].lower()
    assert "простого сценарію" in result["reply_text"]
    assert "Зараз ми фокусуємось" not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_how_it_works_returns_process_reply_not_repeated_service_description(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="як це працює?"))

    assert result["intent"] == "service_description"
    assert "Спочатку" in result["reply_text"]
    assert "Потім" in result["reply_text"]
    assert result["reply_text"] != (
        "Ми налаштовуємо AI-бота для Instagram/Facebook/Telegram/WhatsApp/Viber. "
        "Він відповідає на типові повідомлення, збирає заявки, кваліфікує клієнтів "
        "і допомагає доводити їх до запису або дзвінка."
    )


async def test_typo_implementation_time_question_returns_launch_timeline(processor_factory):
    processor, _ = processor_factory()

    await processor.process(_message(text="хай, що ви пропонуєте?"))
    await processor.process(_message(text="що ви робите?"))
    await processor.process(_message(text="як це працює?"))
    result = await processor.process(_message(text="скіільки часу займає впровадження?"))

    assert result["intent"] == "general_question"
    assert "7-10 днів" in result["reply_text"]
    assert "уточнити" not in result["reply_text"].lower()


async def test_interest_signal_returns_soft_call_cta(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="гаразд цікаво"))

    assert result["intent"] == "interest_signal"
    assert result["routing_category"] == "consultation_soft_cta"
    assert not result["reply_text"].startswith("Привіт!")
    assert "зручно коротко зідзвонитись" in result["reply_text"].lower()
    assert result["booking_result"] is None


async def test_complex_crm_first_message_does_not_get_forced_greeting(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="а ви можете підключити CRM і нестандартні поля?"))

    assert result["intent"] == "general_question"
    assert not result["reply_text"].startswith("Привіт!")
    assert "CRM" in result["reply_text"]


async def test_guarantee_question_gets_specific_reply_not_generic_fallback(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="а гарантії є?"))

    assert result["intent"] == "general_question"
    assert "Гарантовані цифри" in result["reply_text"]
    assert "уточнити" not in result["reply_text"].lower()


async def test_interest_signal_does_not_use_generic_cta(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="звучить цікаво"))

    assert result["intent"] == "interest_signal"
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]
    assert "зручно коротко зідзвонитись" in result["reply_text"].lower()


async def test_interest_signal_acceptance_starts_booking(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="гаразд цікаво"))
    result = await processor.process(_message(text="так"))

    assert result["intent"] == "booking_request"
    assert "день" in result["reply_text"] and "час" in result["reply_text"]
    assert result["booking_result"] is not None
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_service_intro_then_interest_does_not_start_booking_immediately(processor_factory):
    processor, booking_service = processor_factory()

    intro = await processor.process(_message(text="привіт, що ви робите?"))
    interest = await processor.process(_message(text="Цікаво"))
    niche = await processor.process(_message(text="а в мене салон краси, як це працюватиме для нас?"))

    assert interest["intent"] == "interest_signal"
    assert "зручно коротко" in interest["reply_text"].lower()
    assert interest["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"
    assert niche["intent"] == "niche_fit"
    assert "для салону краси" in niche["reply_text"]
    assert "Підкажіть, будь ласка, точний день і час" not in niche["reply_text"]


async def test_live_interest_details_and_business_context_dm_flow(processor_factory):
    processor, booking_service = processor_factory()

    intro = await processor.process(_message(text="Hi, розкажіть що ви робите"))
    actual = await processor.process(_message(text="так актуально"))

    assert intro["intent"] in {"service_description", "interest_signal"}
    assert actual["intent"] == "booking_request"
    assert "день" in actual["reply_text"] and "час" in actual["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_typo_language_and_instagram_volume_followup_is_contextual(processor_factory):
    processor, _ = processor_factory()

    service = await processor.process(_message(text="шо ви робте?"))
    niche = await processor.process(_message(text="а для сто підійде?"))
    volume = await processor.process(_message(text="ну типу в нас багато пишут в інсту"))

    assert "AI-бота" in service["reply_text"]
    assert "для автосервісу" in niche["reply_text"]
    assert volume["intent"] == "business_context_followup"
    assert "багато звернень саме в Instagram" in volume["reply_text"]
    assert "що найчастіше питають" in volume["reply_text"]


async def test_not_ready_for_call_gets_no_pressure_explanation(processor_factory):
    processor, booking_service = processor_factory()

    explanation = await processor.process(_message(text="а можете просто пояснити без дзвінка?"))
    thinking = await processor.process(_message(text="я поки думаю"))

    assert explanation["intent"] == "more_details"
    assert "можна без дзвінка" in explanation["reply_text"]
    assert "Підкажіть, будь ласка, точний день і час" not in explanation["reply_text"]
    assert thinking["intent"] == "hesitation"
    assert "не треба вирішувати одразу" in thinking["reply_text"]
    assert "дзвін" not in thinking["reply_text"].lower()
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_no_call_and_text_explanation_variants_do_not_start_booking(processor_factory):
    processor, booking_service = processor_factory()

    for text in [
        "я хочу спершу зрозуміти",
        "не хочу дзвінок поки",
        "можете в тексті пояснити?",
    ]:
        result = await processor.process(_message(text=text))
        assert result["intent"] == "more_details"
        assert "можна без дзвінка" in result["reply_text"]
        assert result["booking_result"] is None
        assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_safe_fallback_varies_for_short_unclear_replies(processor_factory):
    processor, booking_service = processor_factory()

    unclear = await processor.process(_message(text="?????"))
    acknowledged = await processor.process(_message(text="Зрозуміло"))
    weak = await processor.process(_message(text="ну це таке"))
    agree = await processor.process(_message(text="ага"))
    relevant = await processor.process(_message(text="актуально"))
    maybe = await processor.process(_message(text="можливо"))

    assert "Хочу правильно зрозуміти" in unclear["reply_text"]
    assert acknowledged["intent"] == "contextual_short_reply"
    assert "можу коротко зорієнтувати" in acknowledged["reply_text"]
    assert "Хочу правильно зрозуміти" not in acknowledged["reply_text"]
    assert weak["intent"] == "contextual_short_reply"
    assert "не дуже переконливо" in weak["reply_text"]
    assert agree["intent"] == "contextual_short_reply"
    assert "Можу коротко зорієнтувати" in agree["reply_text"]
    assert relevant["intent"] == "contextual_short_reply"
    assert "Для якого бізнесу" in relevant["reply_text"]
    assert maybe["intent"] == "contextual_short_reply"
    assert "без поспіху" in maybe["reply_text"]
    assert "дзвін" not in weak["reply_text"].lower()
    assert "дзвін" not in agree["reply_text"].lower()
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_greeting_followup_with_auto_service_context_gets_niche_reply(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="Привіт"))
    result = await processor.process(_message(text="так підкажи. ми сто."))

    assert result["intent"] == "business_context_followup"
    assert "для автосервісу" in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_soft_call_cta_acceptance_with_typo_starts_booking(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="Привіт"))
    await processor.process(_message(text="так підкажи. ми сто."))
    result = await processor.process(_message(text="так окк"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "спеціалістом" in result["reply_text"]
    assert "день" in result["reply_text"] and "час" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_typo_call_request_with_col_starts_booking_time_prompt(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="гааразд давайте кол"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "спеціалістом" in result["reply_text"]
    assert "день" in result["reply_text"] and "час" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


@pytest.mark.parametrize("text", ["хочу консультацію", "забронювати час"])
async def test_explicit_consultation_and_booking_words_start_booking(processor_factory, text):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "спеціалістом" in result["reply_text"]
    assert "день" in result["reply_text"] and "час" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_stuck_key_call_request_with_col_starts_booking_time_prompt(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="давайте коол"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "спеціалістом" in result["reply_text"]
    assert "день" in result["reply_text"] and "час" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_booking_pattern_does_not_treat_ok_inside_dzvinok_as_action(processor_factory):
    processor, _ = processor_factory()

    assert processor._looks_like_booking_message("скільки триває дзвінок?") is False


async def test_use_cases_reply_contains_dental_clinic(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="А є якісь кейси?"))

    assert result["intent"] == "use_cases"
    assert "стоматологічна клініка" in result["reply_text"]


async def test_use_cases_reply_avoids_generic_and_fake_claims(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="Покажіть кейси"))

    assert result["intent"] == "use_cases"
    assert "це можна налаштувати" not in result["reply_text"]
    assert "вже впроваджували" not in result["reply_text"]


async def test_interest_signal_does_not_start_booking(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="ок цікаво"))

    assert result["intent"] == "interest_signal"
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_unknown_question_uses_ai_fallback(processor_factory):
    ai_service = RecordingAIService()
    processor, _ = processor_factory(ai_service=ai_service)

    result = await processor.process(_message(text="Чи можна зробити щось нестандартне під мою команду?"))

    assert result["intent"] == "general_question"
    assert result["reply_text"].endswith("AI fallback reply: уточніть, будь ласка, що саме потрібно.")
    assert ai_service.calls
    assert "Ти менеджер Flowly Agency" in ai_service.calls[0]["system_instruction"]
    assert "емоційно інтелектуальним" in ai_service.calls[0]["system_instruction"]
    assert "перефразуй" in ai_service.calls[0]["system_instruction"]
    assert "Не продавай дзвінок на кожне повідомлення" in ai_service.calls[0]["system_instruction"]


async def test_industries_reply_contains_service_businesses_and_auto_service(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="з якими напрямками працюєте?"))

    assert result["intent"] == "industries"
    assert "сервісних бізнесів" in result["reply_text"]
    assert "СТО" in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_auto_service_fit_question_gets_specific_reply(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="для СТО підходить?"))

    assert result["intent"] == "niche_fit"
    assert "для автосервісу" in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_auto_service_context_followup_gets_specific_reply(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="так підкажи, ми автосервіс"))

    assert result["intent"] == "niche_fit"
    assert "для автосервісу" in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_after_hours_question_gets_24_7_value_reply(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="а якщо клієнт пише вночі?"))

    assert result["intent"] == "after_hours_question"
    assert "вночі" in result["reply_text"]
    assert "заявк" in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_rejection_does_not_offer_call(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="ні"))

    assert result["intent"] == "rejection"
    assert result["reply_text"] == (
        "Зрозумів, дякую. Якщо пізніше буде актуально автоматизувати відповіді "
        "в месенджерах — можете просто написати сюди."
    )
    assert "дзвінок" not in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_second_rejection_is_short_and_does_not_repeat_cta(processor_factory):
    processor, _ = processor_factory()

    await processor.process(_message(text="ні"))
    result = await processor.process(_message(text="ні"))

    assert result["intent"] == "rejection"
    assert result["reply_text"] == "Добре, зрозумів."
    assert "дзвінок" not in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_frustrated_user_gets_recovery_reply(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="ти дебіл?"))

    assert result["intent"] == "frustrated"
    assert result["reply_text"] == (
        "Розумію, відповідь була не зовсім по суті. Можу коротко пояснити конкретно: "
        "що робить бот, для яких бізнесів підходить або скільки коштує."
    )
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


@pytest.mark.parametrize(
    "text",
    [
        "я не зрозумів останню відповідь",
        "ні",
        "з якими напрямками працюєте?",
        "ти дебіл?",
        "А є якісь кейси?",
    ],
)
async def test_forbidden_generic_cta_is_not_returned_for_key_routes(processor_factory, text):
    processor, _ = processor_factory()

    result = await processor.process(_message(text=text))

    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_manual_negative_dialogue_has_no_forbidden_generic_cta(processor_factory):
    processor, _ = processor_factory()
    transcript = []

    for text in [
        "чим ви займаєтесь?",
        "з якими напрямками працюєте?",
        "ні",
        "ні",
        "ти дебіл?",
    ]:
        result = await processor.process(_message(text=text))
        transcript.append((text, result["reply_text"]))

    assert "Ми налаштовуємо AI-бота для Instagram/Facebook/Telegram/WhatsApp/Viber." in transcript[0][1]
    assert "типові повідомлення" in transcript[0][1]
    assert "що варто автоматизувати першим" in transcript[0][1]
    assert "Найкраще бот підходить для сервісних бізнесів" in transcript[1][1]
    assert transcript[2][1].startswith("Зрозумів, дякую.")
    assert transcript[3][1] == "Добре, зрозумів."
    assert transcript[4][1].startswith("Розумію, відповідь була не зовсім по суті.")
    for _, reply in transcript:
        assert FORBIDDEN_GENERIC_CTA not in reply


async def test_price_still_returns_from_200(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="Скільки це коштує?"))

    assert result["intent"] == "price"
    assert "старт від 200$" in result["reply_text"]


async def test_russian_input_does_not_produce_russian_output(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="сколько стоит?"))

    assert result["intent"] == "price"
    assert "сколько" not in result["reply_text"].lower()
    assert "стоит" not in result["reply_text"].lower()


async def test_soft_call_cta_accepts_tak_pidkazhy_as_booking_start(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="ми сто"))
    result = await processor.process(_message(text="так підкажи"))

    assert result["intent"] == "booking_request"
    assert "спеціалістом" in result["reply_text"]
    assert "день" in result["reply_text"] and "час" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_repair_price_wording_gets_specific_reply(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="а бот може рахувати ціну ремонту?"))

    assert "Точну вартість ремонту" in result["reply_text"]
    assert "не має вигадувати" in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_question_marks_only_get_safe_ukrainian_clarification(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="?????"))

    assert "Хочу правильно зрозуміти" in result["reply_text"]
    assert "We set up" not in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_branch_routing_reply_does_not_use_forbidden_setup_phrase(processor_factory):
    processor, _ = processor_factory()

    result = await processor.process(_message(text="а якщо у нас 3 філії?"))

    assert "логікою маршрутизації" in result["reply_text"]
    assert "Так, це можна налаштувати" not in result["reply_text"]
    assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_price_and_call_typos_after_niche_reply_do_not_fallback(processor_factory):
    processor, booking_service = processor_factory()

    niche = await processor.process(_message(text="для стоматології як це працює?"))
    price = await processor.process(_message(text="що по ціні?"))
    accepted = await processor.process(_message(text="так давай"))
    typo_call = await processor.process(_message(text="можемо зідзвонитис"))
    call = await processor.process(_message(text="давай кол"))
    availability = await processor.process(_message(text="завтра коли вільно?"))

    assert "для стоматологій" in niche["reply_text"]
    assert price["intent"] == "price"
    assert "старт від 200$" in price["reply_text"]
    assert "дзвін" not in price["reply_text"].lower()
    assert accepted["intent"] == "contextual_short_reply"
    assert "який у вас бізнес" in accepted["reply_text"]
    assert typo_call["intent"] == "booking_request"
    assert "спеціалістом" in typo_call["reply_text"]
    assert "день" in typo_call["reply_text"] and "час" in typo_call["reply_text"]
    assert call["intent"] == "booking_flow"
    assert "спеціалістом" in call["reply_text"]
    assert "день" in call["reply_text"] and "час" in call["reply_text"]
    assert availability["intent"] == "booking_availability_question"
    assert "варіант" in availability["reply_text"]
    assert "завтра" in availability["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"
    for result in [price, accepted, typo_call, call, availability]:
        assert FORBIDDEN_GENERIC_CTA not in result["reply_text"]


async def test_booking_state_does_not_push_time_when_lead_still_has_questions(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="давай кол"))
    service = await processor.process(_message(text="хай, що робите?"))
    niche = await processor.process(
        _message(text="мене цікавить спочатку чи підійде мені такий бот для сто і що він може робить?")
    )
    price = await processor.process(_message(text="ще пиитання яка вартість"))
    postponed = await processor.process(_message(text="давайте я піізніше напишу бо ще не знаю"))

    assert service["intent"] == "booking_product_question"
    assert "AI-бот" in service["reply_text"]
    assert "Для дзвінка підкажіть" not in service["reply_text"]
    assert niche["intent"] == "booking_product_question"
    assert "для автосервісу" in niche["reply_text"]
    assert "Для дзвінка підкажіть" not in niche["reply_text"]
    assert price["intent"] == "booking_product_question"
    assert "старт від 200$" in price["reply_text"]
    assert "Для дзвінка підкажіть" not in price["reply_text"]
    assert postponed["intent"] == "booking_flow"
    assert postponed["booking_result"]["status"] == "cancelled"
    assert "Підкажіть, будь ласка, точний день і час" not in postponed["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


@pytest.mark.parametrize(
    ("text", "expected_fragment"),
    [
        ("бот може записувати клієнтів?", "може допомагати із записом"),
        ("записує до майстра?", "майстра"),
        ("календар можна підключити?", "календар можна підключити"),
        ("може відповідати пацієнтам?", "пацієнтам"),
        ("а записувати на прийом теж?", "пацієнтам"),
    ],
)
async def test_feature_questions_do_not_start_booking(processor_factory, text, expected_fragment):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["intent"] == "capability_question"
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"
    assert expected_fragment in result["reply_text"]
    assert "Підкажіть, будь ласка, точний день і час" not in result["reply_text"]


async def test_feature_question_during_booking_does_not_push_exact_time(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="давайте кол"))
    result = await processor.process(_message(text="бот сам записує клієнтів до майстра?"))

    assert result["intent"] == "capability_question"
    assert "може допомагати із записом" in result["reply_text"]
    assert "Підкажіть, будь ласка, точний день і час" not in result["reply_text"]
    assert "Для дзвінка підкажіть" not in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


@pytest.mark.parametrize(
    "text",
    [
        "без дзвінка, поясніть тут",
        "не хочу дзвінок поки",
        "можете в тексті пояснити?",
        "давайте тут",
    ],
)
async def test_no_call_phrases_answer_in_chat_without_booking(processor_factory, text):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["intent"] == "more_details"
    assert "можна без дзвінка" in result["reply_text"]
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"


@pytest.mark.parametrize(
    "text",
    [
        "записує до майстра?",
        "може записувати клієнтів?",
        "календар можна підключити?",
        "може відповідати пацієнтам?",
    ],
)
async def test_feature_words_are_not_booking_intent(processor_factory, text):
    processor, _ = processor_factory()

    intent = processor.intent_service.detect_intent(text)

    assert intent != IntentType.BOOKING_REQUEST


@pytest.mark.parametrize(
    ("text", "expected_fragment", "expected_routing"),
    [
        ("а в телеграмі працює?", "Telegram", "answered_basic"),
        ("а у вайбер можна?", "Viber", "answered_basic"),
        ("заявки менеджеру передає?", "передавати заявки менеджеру", "consultation_soft_cta"),
        ("CRM можна підключити?", "CRM можна підключити", "consultation_soft_cta"),
        ("а якщо людина пише криво?", "перепитати", "consultation_soft_cta"),
    ],
)
async def test_flowly_capability_questions_balance_answer_and_cta(
    processor_factory,
    text,
    expected_fragment,
    expected_routing,
):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["intent"] == "capability_question"
    assert expected_fragment in result["reply_text"]
    assert result["routing_category"] == expected_routing
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"
    assert "Підкажіть, будь ласка, точний день і час" not in result["reply_text"]


async def test_after_hours_flowly_question_has_helpful_answer_and_soft_cta(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="а якщо клієнт пише вночі?"))

    assert result["intent"] == "after_hours_question"
    assert "бот може одразу відповісти" in result["reply_text"]
    assert "спеціалістом" in result["reply_text"]
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_flowly_price_and_objection_still_work_with_soft_sales(processor_factory):
    processor, booking_service = processor_factory()

    price = await processor.process(_message(text="скільки коштує?"))
    objection = await processor.process(_message(text="дорогувато"))

    assert price["intent"] == "price"
    assert "старт від 200$" in price["reply_text"]
    assert objection["intent"] == "general_question"
    assert "мінімального сценарію" in objection["reply_text"]
    assert "дзвін" not in objection["reply_text"].lower()
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_no_call_explain_here_does_not_start_booking(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="без дзвінка поясни тут"))

    assert result["intent"] == "more_details"
    assert "можна без дзвінка" in result["reply_text"]
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_explicit_consultation_request_starts_booking(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="давай консультацію"))

    assert result["intent"] == "booking_request"
    assert "спеціалістом" in result["reply_text"]
    assert "день" in result["reply_text"] and "час" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_multi_turn_warm_lead_is_guided_to_consultation_without_pressure(processor_factory):
    processor, booking_service = processor_factory()

    telegram = await processor.process(_message(text="а в телеграмі працює?"))
    crm = await processor.process(_message(text="CRM можна підключити?"))
    price = await processor.process(_message(text="скільки коштує?"))
    objection = await processor.process(_message(text="дорогувато"))
    maybe = await processor.process(_message(text="ну може"))

    assert telegram["intent"] == "capability_question"
    assert telegram["routing_category"] == "answered_basic"
    assert "Telegram" in telegram["reply_text"]
    assert "коротко розібрати ваш процес" not in telegram["reply_text"]

    assert crm["intent"] == "capability_question"
    assert crm["routing_category"] == "consultation_soft_cta"
    assert "CRM можна підключити" in crm["reply_text"]
    assert "спеціалістом" in crm["reply_text"]
    assert "мінімальний сценарій" in crm["reply_text"]

    assert price["intent"] == "price"
    assert "старт від 200$" in price["reply_text"]

    assert objection["intent"] == "general_question"
    assert "мінімального сценарію" in objection["reply_text"]
    assert "дзвін" not in objection["reply_text"].lower()

    assert maybe["intent"] == "contextual_short_reply"
    assert "без тиску" in maybe["reply_text"]
    assert "Підкажіть, будь ласка, точний день і час" not in maybe["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_second_simple_capability_question_gets_soft_cta(processor_factory):
    processor, booking_service = processor_factory()

    first = await processor.process(_message(text="а в телеграмі працює?"))
    second = await processor.process(_message(text="а у вайбер можна?"))

    assert first["routing_category"] == "answered_basic"
    assert second["intent"] == "capability_question"
    assert second["routing_category"] == "consultation_soft_cta"
    assert "Viber" in second["reply_text"]
    assert "як це у вас виглядатиме" in second["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("черговий чатбот?", "не просто кнопковий чатбот"),
        ("шаблони продаєте?", "не в тому, щоб продати набір шаблонів"),
        ("бот буде тупити?", "не має вигадувати відповіді"),
    ],
)
async def test_skepticism_gets_human_answer_without_booking(processor_factory, text, expected):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["intent"] == "skepticism"
    assert expected in result["reply_text"]
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"


@pytest.mark.parametrize(
    ("text", "expected_intent", "expected"),
    [
        ("поясни простіше", "contextual_short_reply", "бот відповідає замість менеджера"),
        ("не знаю чи треба", "hesitation", "Де зараз найбільше ручної переписки"),
        ("не впевнений", "hesitation", "не треба вирішувати одразу"),
    ],
)
async def test_indecision_and_simple_explanation_stay_helpful_without_booking(
    processor_factory,
    text,
    expected_intent,
    expected,
):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["intent"] == expected_intent
    assert expected in result["reply_text"]
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_explicit_discuss_intent_starts_booking_but_crm_does_not(processor_factory):
    processor, booking_service = processor_factory()

    crm = await processor.process(_message(text="CRM підключається?"))
    state_after_crm = booking_service.get_booking_state("user-1").value
    discuss = await processor.process(_message(text="хочу обговорити"))

    assert crm["intent"] == "capability_question"
    assert "CRM можна підключити" in crm["reply_text"]
    assert state_after_crm == "NONE"
    assert discuss["intent"] == "booking_request"
    assert "спеціалістом" in discuss["reply_text"]
    assert "день" in discuss["reply_text"] and "час" in discuss["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_chaotic_dm_short_forms_do_not_fallback(processor_factory):
    processor, booking_service = processor_factory()

    telegram = await processor.process(_message(text="а телега?"))
    start = await processor.process(_message(text="шо треба від мене"))
    buying = await processor.process(_message(text="ок спробуєм"))

    assert telegram["intent"] == "capability_question"
    assert "Telegram" in telegram["reply_text"]
    assert start["intent"] == "start_requirements"
    assert "Для старту потрібно" in start["reply_text"]
    assert buying["intent"] == "buying_signal"
    assert "можемо допомогти" in buying["reply_text"].lower()
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_skeptic_competitor_question_gets_specific_reply(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="ну і чим ви кращі?"))

    assert result["intent"] == "skepticism"
    assert "сценарії під ваш процес" in result["reply_text"]
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"


@pytest.mark.parametrize(
    "text",
    [
        "мені це не зайде",
        "ви просто продаєте повітря",
        "таких ботів купа",
    ],
)
async def test_aggressive_objection_does_not_fallback_or_start_booking(processor_factory, text):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text=text))

    assert result["intent"] == "aggressive_objection"
    assert "Розумію скепсис" in result["reply_text"]
    assert "повторювані звернення" in result["reply_text"]
    assert "Тут краще коротко уточнити деталі" not in result["reply_text"]
    assert result["booking_result"] is None
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_multi_turn_complex_interest_gets_proactive_specialist_cta(processor_factory):
    processor, booking_service = processor_factory()

    telegram = await processor.process(_message(text="а в телеграмі працює?"))
    crm = await processor.process(_message(text="CRM можна підключити?"))
    night = await processor.process(_message(text="а якщо клієнти пишуть вночі?"))

    assert telegram["intent"] == "capability_question"
    assert crm["intent"] == "capability_question"
    assert "спеціалістом" in crm["reply_text"]
    assert night["intent"] == "after_hours_question"
    assert "спеціалістом" in night["reply_text"]
    assert "не відповідати загально" in night["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_direct_call_request_uses_natural_specialist_time_prompt(processor_factory):
    processor, booking_service = processor_factory()

    result = await processor.process(_message(text="давай дзвінок"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "спеціалістом" in result["reply_text"]
    assert "який день" in result["reply_text"]
    assert "приблизний час" in result["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_no_call_after_proactive_specialist_cta_is_respected(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="а в телеграмі працює?"))
    cta = await processor.process(_message(text="CRM можна підключити?"))
    no_call = await processor.process(_message(text="без дзвінка поясни"))

    assert "спеціалістом" in cta["reply_text"]
    assert no_call["intent"] == "more_details"
    assert "можна без дзвінка" in no_call["reply_text"]
    assert "спеціалістом" not in no_call["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_price_objection_after_proactive_cta_does_not_force_call(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="а в телеграмі працює?"))
    await processor.process(_message(text="CRM можна підключити?"))
    objection = await processor.process(_message(text="дорого"))

    assert objection["intent"] == "general_question"
    assert "мінімального сценарію" in objection["reply_text"]
    assert "дзвін" not in objection["reply_text"].lower()
    assert booking_service.get_booking_state("user-1").value == "NONE"


async def test_call_request_after_no_call_accepts_new_intent(processor_factory):
    processor, booking_service = processor_factory()

    no_call = await processor.process(_message(text="без дзвінка поясни"))
    call = await processor.process(_message(text="давай дзвінок"))

    assert no_call["intent"] == "more_details"
    assert call["intent"] == "booking_request"
    assert "спеціалістом" in call["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "WAITING_FOR_TIME"


async def test_no_call_after_telegram_crm_night_sequence_stays_in_chat(processor_factory):
    processor, booking_service = processor_factory()

    await processor.process(_message(text="а в телеграмі працює?"))
    await processor.process(_message(text="CRM можна підключити?"))
    night = await processor.process(_message(text="а якщо клієнти пишуть вночі?"))
    no_call = await processor.process(_message(text="без дзвінка"))

    assert "спеціалістом" in night["reply_text"]
    assert no_call["intent"] == "more_details"
    assert "можна без дзвінка" in no_call["reply_text"]
    assert "спеціалістом" not in no_call["reply_text"]
    assert booking_service.get_booking_state("user-1").value == "NONE"
