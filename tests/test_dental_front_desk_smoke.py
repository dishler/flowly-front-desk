from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.application.dto.normalized_message import NormalizedMessage
from app.application.services import booking_service as booking_service_module
from app.application.services import business_hours as business_hours_module
from app.application.services.booking_service import BookingService
from app.application.services.dedup_service import DedupService
from app.application.services.front_desk_config_service import FrontDeskConfigService
from app.application.services.intent_service import IntentService
from app.application.services.knowledge_service import KnowledgeService
from app.application.services.language_service import LanguageService
from app.application.services.memory_service import MemoryService
from app.application.services.message_processor import MessageProcessor
from app.application.services.reply_service import ReplyService
from app.domain.enums import BookingState


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


_REAL_DATETIME = datetime


class FixedDentalSmokeDatetimeMeta(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, _REAL_DATETIME)


class FixedDentalSmokeDatetime(_REAL_DATETIME, metaclass=FixedDentalSmokeDatetimeMeta):
    @classmethod
    def now(cls, tz=None):
        fixed = _REAL_DATETIME(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        return fixed if tz is None else fixed.astimezone(tz)


@pytest.fixture(autouse=True)
def freeze_dental_smoke_booking_clock(monkeypatch):
    monkeypatch.setattr(booking_service_module, "datetime", FixedDentalSmokeDatetime)


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


class SlotBusyOnFinalRecheckCalendarService(SelectiveConfiguredCalendarService):
    def __init__(self) -> None:
        super().__init__([
            _kyiv_dt(2026, 8, 24, 12),
            _kyiv_dt(2026, 8, 24, 13),
        ])
        self._seen_12 = 0

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        if start_dt == _kyiv_dt(2026, 8, 24, 12):
            self._seen_12 += 1
            return self._seen_12 == 1
        return (start_dt.date().isoformat(), start_dt.hour, start_dt.minute) in self.available_slots


class SlotBusyOnFinalRecheckNoAlternativeCalendarService(SelectiveConfiguredCalendarService):
    def __init__(self) -> None:
        super().__init__([_kyiv_dt(2026, 8, 24, 12)])
        self._seen_12 = 0

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        if start_dt == _kyiv_dt(2026, 8, 24, 12):
            self._seen_12 += 1
            return self._seen_12 == 1
        return False


class UniqueEventConfiguredCalendarService(SelectiveConfiguredCalendarService):
    def __init__(self, available_slots, *, created_slots_become_busy: bool = False) -> None:
        super().__init__(available_slots)
        self.created_slots_become_busy = created_slots_become_busy

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        if self.created_slots_become_busy and any(
            created["start_dt"] == start_dt for created in self.created
        ):
            return False
        return (start_dt.date().isoformat(), start_dt.hour, start_dt.minute) in self.available_slots

    def create_booking_event(
        self,
        start_dt,
        duration_minutes: int = 30,
        summary: str = "",
        description: str = "",
        attendee_emails=None,
    ):
        event_id = f"dental-event-{len(self.created) + 1}"
        self.created.append(
            {
                "event_id": event_id,
                "start_dt": start_dt,
                "duration_minutes": duration_minutes,
                "summary": summary,
                "description": description,
                "attendee_emails": attendee_emails or [],
            }
        )

        class CreatedEvent:
            status = "confirmed"

        CreatedEvent.event_id = event_id
        CreatedEvent.html_link = f"https://calendar.example/{event_id}"
        return CreatedEvent()


class FailingConfiguredCalendarService(RecordingConfiguredCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        raise RuntimeError("calendar unavailable")


class FailingAt16ConfiguredCalendarService(RecordingConfiguredCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        if start_dt.hour == 16:
            raise RuntimeError("calendar unavailable")
        return True


@pytest.fixture
def dental_processor():
    return _build_dental_processor()


def _build_dental_processor(ai_service=None, calendar_service=None, dedup_service=None):
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
        dedup_service=dedup_service or DummyDedupService(),
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


def _message_with_mid(text: str, message_mid: str, sender_id: str = "patient-1") -> NormalizedMessage:
    return NormalizedMessage(
        platform="instagram",
        sender_id=sender_id,
        recipient_id="clinic",
        message_mid=message_mid,
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
@pytest.mark.parametrize("text", ["прив", "доброго", "ітаю"])
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
async def test_dental_combined_knowledge_questions_answer_each_grounded_part():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("де ви, чи працюєте в суботу, скільки імплант"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Липська, 12" in result["reply_text"]
    assert "10:00-16:00" in result["reply_text"]
    assert "16000 грн" in result["reply_text"]
    assert context["current_service_id"] == "dental_implant"
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_ungrounded_logistics_gets_safe_ack_and_keeps_service():
    """Service-first, ungrounded logistics clause second: the logistics
    question ("скажіть є де машину поставити?") has no grounded answer in
    the KB, so it must get a short, non-invented acknowledgement instead of
    being silently dropped -- while the recognized service (dental_consultation)
    is still saved and the booking flow still starts."""
    processor, calendar = _build_dental_processor()

    result = await processor.process(
        _message("хочу на огляд, скажіть є де машину поставити біля вас?")
    )
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert "перевірити з адміністратором" in result["reply_text"]
    assert "який день і приблизний час" in result["reply_text"]
    assert context["current_service_id"] == "dental_consultation"
    assert calendar.checked == []
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_ungrounded_ack_does_not_invent_facts():
    """The ungrounded-logistics acknowledgement itself must never claim
    concrete facts (address, price, hours) it doesn't have."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(
        _message("хочу на огляд, скажіть є де машину поставити біля вас?")
    )

    for forbidden in ["парковка", "Липська", "грн", "09:00", "10:00"]:
        assert forbidden not in result["reply_text"]


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_grounded_logistics_answers_both_parts():
    """Service-first, grounded logistics clause second: the schedule
    question must be answered from the KB, and the booking flow must still
    start for the named service."""
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("хочу на консультацію, а коли ви працюєте?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert "09:00-19:00" in result["reply_text"]
    assert "який день і приблизний час" in result["reply_text"]
    assert context["current_service_id"] == "dental_consultation"
    assert calendar.checked == []
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_reverse_order_grounded():
    """Logistics-first, service second: "до котрої працюєте, хочу
    записатись на огляд" must ground the schedule question AND still
    recognize and start booking for the consultation service, even though
    the service name comes after the FAQ clause."""
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("до котрої працюєте, хочу записатись на огляд"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert "09:00-19:00" in result["reply_text"]
    assert "який день і приблизний час" in result["reply_text"]
    assert context["current_service_id"] == "dental_consultation"
    assert calendar.checked == []
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_reverse_order_ungrounded():
    """Logistics-first, service second, but the logistics clause has no
    grounded answer: must get the safe acknowledgement (not silence, not an
    invented fact), and the service must still be recognized and booked."""
    processor, calendar = _build_dental_processor()

    result = await processor.process(
        _message("скажіть є де машину поставити, хочу записатись на огляд")
    )
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert "перевірити з адміністратором" in result["reply_text"]
    assert "який день і приблизний час" in result["reply_text"]
    assert context["current_service_id"] == "dental_consultation"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_bare_reaction_clause_repro_c9():
    """Repro C9 (a P2 regression from the bounded multi-intent fix itself):
    "Шкода. тоді хочу просто на огляд записатись" -- "Шкода." is just a
    reaction to whatever came before, not a logistics/FAQ question, and
    must not get the "перевірити з адміністратором" acknowledgement
    tacked onto the booking reply."""
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Шкода. тоді хочу просто на огляд записатись"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert "перевірити з адміністратором" not in result["reply_text"]
    assert "\n\n" not in result["reply_text"]
    assert context["current_service_id"] == "dental_consultation"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "добре, хочу на консультацію",
        "ясно. хочу записатись на чистку",
        "ок, запишіть мене на видалення зуба",
        "гаразд, хочу на огляд",
        "зрозуміло. тоді хочу на брекети",
        "окей, запишіть на відбілювання",
        "звісно, хочу на огляд",
    ],
)
async def test_dental_bounded_multi_intent_bare_reaction_clause_variants_unaffected(text):
    """Every bare reaction word the fix targets must leave the adjacent
    booking request completely clean -- no acknowledgement artifact."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "booking_request"
    assert "перевірити з адміністратором" not in result["reply_text"]
    assert "\n\n" not in result["reply_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "service_id"),
    [
        ("добрий день, хочу записатися на чистку", "dental_cleaning"),
        ("привіт, можна записатися на консультацію?", "dental_consultation"),
        ("вітаю, хочу записатися на відбілювання", "teeth_whitening"),
    ],
)
async def test_dental_bounded_multi_intent_greeting_clause_does_not_add_admin_ack(
    text, service_id
):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] in {"waiting_for_time", "waiting_for_datetime"}
    assert "перевірити з адміністратором" not in result["reply_text"]
    assert "адміністратор" not in result["reply_text"].lower()
    assert context["current_service_id"] == service_id
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["привіт", "добрий день", "вітаю"])
async def test_dental_bounded_multi_intent_greeting_only_controls_unchanged(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "general_question"
    assert "Smile Dental Clinic" in result["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_grounded_fact_plus_booking_still_answered():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("де ви знаходитесь, хочу на чистку"))

    assert result["intent"] == "booking_request"
    assert "Липська, 12" in result["reply_text"]
    assert "який день і приблизний час" in result["reply_text"]
    assert processor.memory_service.get_context("patient-1")["current_service_id"] == "dental_cleaning"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_substantive_prefix_still_gets_ack():
    processor, calendar = _build_dental_processor()

    result = await processor.process(
        _message("скажіть чи є знижки для пенсіонерів, хочу записатись на чистку")
    )

    assert result["intent"] == "booking_request"
    assert "перевірити з адміністратором" in result["reply_text"]
    assert "який день і приблизний час" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_explicit_admin_request_still_handoff():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("привіт, дайте живу людину будь ласка"))

    assert result["intent"] == "human_handoff_request"
    assert "AI-асистент" in result["reply_text"]
    assert "адміністратор" in result["reply_text"].lower()
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_real_ungrounded_faq_still_gets_ack_after_reaction_fix():
    """A genuine ungrounded FAQ clause (not a bare reaction word) must
    still get the safe acknowledgement -- the reaction-word filter must
    not swallow real, if unanswerable, questions."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(
        _message("хочу на огляд, скажіть чи є знижки для пенсіонерів")
    )

    assert result["intent"] == "booking_request"
    assert "перевірити з адміністратором" in result["reply_text"]


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_real_grounded_faq_still_answered_after_reaction_fix():
    """A genuine grounded FAQ clause must still be answered -- the
    reaction-word filter must not affect real logistics questions."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("хочу на консультацію, а коли ви працюєте?"))

    assert result["intent"] == "booking_request"
    assert "09:00-19:00" in result["reply_text"]


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_service_context_survives_next_turn():
    """After a service+ungrounded-logistics opener, the very next turn
    (just a day/time) must still be treated as continuing the booking for
    the already-recognized service, not lose it and re-ask."""
    processor, calendar = _build_dental_processor()

    await processor.process(
        _message("хочу на огляд, скажіть є де машину поставити біля вас?")
    )
    result = await processor.process(_message("у вівторок о 10"))

    assert result["intent"] == "booking_flow"
    assert (result.get("booking_result") or {}).get("status") == "waiting_for_contact"
    assert "вільний" in result["reply_text"]
    assert len(calendar.checked) == 1
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "хочу на огляд",
        "хочу записатись на чистку зубів",
    ],
)
async def test_dental_bounded_multi_intent_single_clause_service_only_is_unaffected(text):
    """A single, ordinary booking clause (the overwhelming common case)
    must produce exactly the plain booking prompt -- no acknowledgement
    text prepended, since there is no second clause to react to."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "booking_request"
    assert "перевірити з адміністратором" not in result["reply_text"]
    assert "\n\n" not in result["reply_text"]


@pytest.mark.asyncio
async def test_dental_bounded_multi_intent_single_clause_faq_only_is_unaffected():
    """A bare FAQ question with no service/booking intent at all must keep
    answering exactly as before -- no booking prompt appended."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("а коли ви працюєте?"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert "09:00-19:00" in result["reply_text"]
    assert "який день і приблизний час" not in result["reply_text"]
    assert "\n\n" not in result["reply_text"]


@pytest.mark.asyncio
async def test_dental_price_answer_then_nearest_availability_followup_keeps_service_context():
    """Repro C1: "хочу на консультацію, скільки це коштує?" answers with
    price and does not start booking (a curiosity question, by design).
    But the very next turn asking for the nearest available day must still
    remember the consultation service from that price answer and continue
    booking for it -- not fall back to the generic "what do you want"
    clarify prompt."""
    processor, calendar = _build_dental_processor()

    price_result = await processor.process(_message("хочу на консультацію, скільки це коштує?"))
    assert price_result["intent"] == "front_desk_contextual_answer"
    assert "700 грн" in price_result["reply_text"]

    followup_result = await processor.process(_message("ясно, а найближчий вільний день який?"))

    assert followup_result["intent"] != "front_desk_safe_fallback"
    assert "Уточніть, будь ласка, що саме вас цікавить" not in followup_result["reply_text"]
    context = processor.memory_service.get_context("patient-1")
    assert context["current_service_id"] == "dental_consultation"


@pytest.mark.asyncio
async def test_dental_price_and_grounded_faq_in_one_message_both_answered():
    """Repro C15: a single message naming a service, asking its price, AND
    asking an unrelated grounded FAQ question (financing) must answer both
    -- not just the price."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(
        _message("ясна кровоточать, скільки коштує лікування, і чи є розстрочка?")
    )

    assert result["intent"] == "front_desk_contextual_answer"
    assert "700 грн" in result["reply_text"]
    assert "розтермінування" in result["reply_text"] or "розстрочка" in result["reply_text"]


@pytest.mark.asyncio
async def test_dental_price_question_with_ungrounded_second_question_does_not_invent():
    """A second question that has no grounded FAQ answer (e.g. a medical
    safety question) must never get an invented answer -- only the price
    is answered, and nothing is fabricated for the rest."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(
        _message(
            "хочу дізнатись про відбілювання, скільки коштує, "
            "і чи можна робити при вагітності?"
        )
    )

    assert "4500 грн" in result["reply_text"]
    for forbidden in ["вагітн", "можна", "не рекомендується", "безпечно"]:
        assert forbidden not in result["reply_text"].lower()


@pytest.mark.asyncio
async def test_dental_price_only_question_unaffected_by_faq_composition_fix():
    """A plain price question with no second FAQ present must produce
    exactly the same single-fact answer as before -- no blank-line
    artifact, nothing appended."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("скільки коштує чистка?"))

    assert result["reply_text"] == "Професійна гігієна коштує від 1800 грн. Точна вартість залежить від обсягу роботи після огляду."


@pytest.mark.asyncio
async def test_dental_nearest_availability_without_prior_service_context_stays_safe_fallback():
    """A cold-start "nearest available day" question with no prior service
    context must not hallucinate a service -- it should still fall back to
    the generic clarifying prompt, exactly as before this fix."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("а найближчий вільний день який?"))

    assert result["intent"] == "front_desk_safe_fallback"


@pytest.mark.asyncio
async def test_dental_typo_tolerant_service_recognition_repro_c2():
    """Repro C2: "хочю записатись на чиску зубів" (typo'd "чистку") must
    still be recognized as the cleaning service and start booking for it,
    not fall back to "яку послугу хочете?"."""
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("хочю записатись на чиску зубів"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    context = processor.memory_service.get_context("patient-1")
    assert context.get("current_service_id") == "dental_cleaning"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_service_id"),
    [
        ("хочу на чистуку зубів", "dental_cleaning"),
        ("де в вас консультайція", "dental_consultation"),
        ("хочу впаравити прикус", "orthodontic_consultation"),
        ("цкавлять брекети", "braces"),
        ("хочу елаінери", "aligners"),
        ("хочу седацію", "dental_sedation"),
        ("хочу коронуку", "dental_crowns"),
    ],
)
async def test_dental_typo_tolerant_service_recognition_unseen_variants(text, expected_service_id):
    """Several unseen one-character typo variants across different
    services must all still resolve to their real service."""
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")

    result = knowledge_service.find_confident_service(text)

    assert result is not None
    assert result["id"] == expected_service_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "у мене зуби зовсім чисті, не турбує",
        "чиста квартира по сусідству",
        "зуби чисті, дякую",
        "він дуже чистий",
        "один зуб став темніший за інші",
        "зуб трохи потемнів з часом",
    ],
)
async def test_dental_typo_tolerant_matching_does_not_falsely_match_real_unrelated_words(text):
    """Adversarial: words that are one character away from a service stem
    but are themselves ordinary, unrelated Ukrainian words (an adjective
    root like "чист"/"чиста" vs the noun "чистка"; a verb form like "став"
    vs the imperative "встав") must never be treated as naming a service --
    a wrong match is worse than no match."""
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")

    result = knowledge_service.find_confident_service(text)

    assert result is None


@pytest.mark.asyncio
async def test_dental_typo_tolerant_matching_never_overrides_exact_match():
    """When exact/morphological matching already finds a service, the typo
    fallback must never run at all and must never change the result --
    exact matching stays strictly prioritized."""
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")

    exact_text = "хочу записатись на чистку зубів"
    result = knowledge_service.find_confident_service(exact_text)

    assert result is not None
    assert result["id"] == "dental_cleaning"


@pytest.mark.asyncio
async def test_dental_per_jaw_faq_answers_correctly_in_braces_context():
    """Repro: "це за одну щелепу?" in a genuine braces context must return
    the real per-jaw pricing FAQ."""
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")
    braces = knowledge_service.get_service_by_id("braces")
    normalized = knowledge_service._normalize_question_for_match("це за одну щелепу?")

    answer = knowledge_service._find_contextual_faq_answer(braces, normalized, "uk")

    assert answer is not None
    assert "за одну щелепу" in answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service_id",
    [
        "aligners",
        "dental_cleaning",
        "dental_consultation",
        "dental_diagnostics",
        "pediatric_cleaning",
        "teeth_whitening",
        "orthodontic_consultation",
        "pediatric_dentistry",
    ],
)
async def test_dental_per_jaw_faq_never_leaks_into_unrelated_service_context(service_id):
    """Repro: the braces-specific "per jaw" FAQ must never be returned for
    a service it has nothing to do with -- confirmed leak class caused by
    generic words ("для", "зуб*", "щелеп*") counting as meaningful
    service-specific overlap."""
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")
    service = knowledge_service.get_service_by_id(service_id)
    normalized = knowledge_service._normalize_question_for_match("це за одну щелепу?")

    answer = knowledge_service._find_contextual_faq_answer(service, normalized, "uk")

    assert answer is None


@pytest.mark.asyncio
async def test_dental_per_jaw_faq_leak_fixed_end_to_end_for_aligners():
    """End-to-end repro of the exact reported production conversation:
    asking the per-jaw question right after an aligners price answer must
    not get the braces-specific per-jaw claim."""
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують елайнери?"))
    result = await processor.process(_message("це за одну щелепу?"))

    assert "за одну щелепу" not in result["reply_text"]


@pytest.mark.asyncio
async def test_dental_per_jaw_faq_still_works_end_to_end_for_braces():
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message("це за одну щелепу?"))

    assert "за одну щелепу" in result["reply_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_id", "query", "expected_fragment"),
    [
        ("dental_cleaning", "Скільки коштує чистка?", "1800 грн"),
        ("veneers", "треба обточувати зуб під вініри?", "клінічної ситуації"),
        ("wisdom_tooth_extraction", "треба видаляти всі зуби мудрості?", "індивідуально"),
        ("dental_implant", "давно немає зуба, чи можна імплант?", "рентген-діагностики"),
        ("pediatric_cleaning", "скільки коштує дитяча чистка зубів?", "1200 грн"),
    ],
)
async def test_dental_legitimate_contextual_faq_answers_still_work(service_id, query, expected_fragment):
    """Existing, genuinely-relevant contextual FAQ matches must survive
    the generic-word exclusion -- this isn't a blanket tightening, only
    the specific generic tokens are excluded from counting as
    service-specific overlap."""
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")
    service = knowledge_service.get_service_by_id(service_id)
    normalized = knowledge_service._normalize_question_for_match(query)

    answer = knowledge_service._find_contextual_faq_answer(service, normalized, "uk")

    assert answer is not None
    if expected_fragment:
        assert expected_fragment in answer


@pytest.mark.asyncio
async def test_dental_price_unit_loads_correctly_for_braces_price_options():
    """braces price_options must carry the explicit per-jaw unit for both
    materials, loaded as plain dict data with no schema/loader changes."""
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")
    braces = knowledge_service.get_service_by_id("braces")

    options_by_label = {opt["label"]: opt for opt in braces["price_options"]}

    assert options_by_label["Металеві брекети"]["price_unit"] == "per_jaw"
    assert options_by_label["Керамічні брекети"]["price_unit"] == "per_jaw"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_id", "expected_unit"),
    [
        ("aligners", "per_course"),
        ("veneers", "per_tooth"),
        ("dental_sedation", "per_hour"),
    ],
)
async def test_dental_price_unit_loads_correctly_for_flat_price_services(service_id, expected_unit):
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")
    service = knowledge_service.get_service_by_id(service_id)

    assert service.get("price_unit") == expected_unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service_id",
    [
        "dental_cleaning",
        "pediatric_cleaning",
        "teeth_whitening",
        "tooth_extraction",
        "wisdom_tooth_extraction",
        "dental_implant",
        "dental_consultation",
        "orthodontic_consultation",
        "prosthetics_consultation",
        "prosthodontics",
        "dental_crowns",
        "caries_treatment",
        "root_canal_treatment",
        "gum_treatment",
        "pediatric_dentistry",
    ],
)
async def test_dental_price_unit_not_inferred_for_unspecified_services(service_id):
    """Only the five explicitly-approved entries get a price_unit -- every
    other service, including ones with multiple price_options like
    dental_crowns, must not have a unit invented for it."""
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")
    service = knowledge_service.get_service_by_id(service_id)

    assert service.get("price_unit") is None
    for option in service.get("price_options") or []:
        assert option.get("price_unit") is None


@pytest.mark.asyncio
async def test_dental_pricing_replies_unchanged_by_price_unit_addition():
    """The price_unit field is not read by any reply-building code yet --
    adding it must not change a single existing reply's wording."""
    processor, _calendar = _build_dental_processor()

    braces_result = await processor.process(_message("Скільки коштують металеві брекети?"))
    assert braces_result["reply_text"] == (
        "Вартість брекетів: Металеві брекети — від 19000 грн; Керамічні брекети — від 30000 грн."
    )

    aligners_result = await processor.process(_message("Скільки коштують елайнери?", sender_id="patient-2"))
    assert aligners_result["reply_text"] == "Елайнери коштують від 60000 грн за курс лікування."

    sedation_result = await processor.process(_message("скільки коштує седація?", sender_id="patient-3"))
    assert sedation_result["reply_text"] == "Седація коштує від 6000 грн за першу годину."


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
    assert "4500 грн" not in result["reply_text"]
    assert "Для дзвінка підкажіть" not in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_short_how_much_service_questions_are_specific(dental_processor):
    processor, _calendar = dental_processor

    cleaning = await processor.process(_message("скільки чистка?"))
    whitening = await processor.process(_message("а відбілювання?"))

    assert "1800 грн" in cleaning["reply_text"]
    assert "4500 грн" in whitening["reply_text"]
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

    assert "4500" in price["reply_text"]
    assert objection["intent"] == "front_desk_safe_fallback"
    assert "старт від 200" not in objection["reply_text"]
    assert "мінімального сценарію" not in objection["reply_text"]
    _assert_no_flowly_leakage(price["reply_text"], objection["reply_text"])


@pytest.mark.asyncio
async def test_dental_booking_intent_still_enters_booking(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("хочу записатись"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_service"
    assert result["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "можна мене записати",
        "можете мене записати",
        "запишіть мене",
        "запиши мене",
        "хочу щоб мене записали",
    ],
)
async def test_dental_book_me_intent_forms_enter_booking(text):
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_service"
    assert result["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_booking_want_cleaning_enters_booking():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("хочу на чистку"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_cleaning"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "хочу зуб видалити",
        "зуб видалити хочу",
        "привіт, мені треба зуб видалити",
    ],
)
async def test_dental_tooth_extraction_booking_tolerates_natural_word_order(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert context["current_service_id"] == "tooth_extraction"
    assert pending["current_service_id"] == "dental_consultation"
    assert "Чим можемо допомогти" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_early_contact_with_date_is_reused_after_time_selection():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    first = await processor.process(_message("Хочу на чистку у четвер, Дмитро, 0987121328"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert first["booking_result"]["status"] == "waiting_for_time"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["requested_date"] == "2026-08-27"
    assert pending["requested_day_label"] == "четвер"
    assert pending["customer_name"] == "Дмитро"
    assert pending["contact_phone"] == "0987121328"

    confirmed = await processor.process(_message("14"))
    completed = processor.booking_service._get_completed_booking("patient-1")

    assert confirmed["booking_result"]["status"] == "confirmed"
    assert "Супер, Дмитро, підтвердили ваш візит" in confirmed["reply_text"]
    assert "Чекатимемо на вас" in confirmed["reply_text"]
    assert "Зв’яжемося" not in confirmed["reply_text"]
    assert completed["customer_name"] == "Дмитро"
    assert completed["phone"] == "0987121328"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 27, 14), "duration_minutes": 30}
    ]
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 14)
    assert "Service: Професійна гігієна зубів" in calendar.created[0]["description"]
    assert "Customer name: Дмитро" in calendar.created[0]["description"]
    assert "Phone: 0987121328" in calendar.created[0]["description"]


@pytest.mark.asyncio
async def test_dental_early_name_only_with_date_is_not_preserved_without_contact():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер, Дмитро"))
    time = await processor.process(_message("14"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert time["booking_result"]["status"] == "waiting_for_contact"
    assert pending["state"] == "WAITING_FOR_CONTACT"
    assert pending["customer_name"] is None
    assert pending["contact_phone"] is None
    assert pending["start_dt"] == "2026-08-27T14:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_early_phone_with_date_waits_for_name_after_time():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер, 0987121328"))
    time = await processor.process(_message("14"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert time["booking_result"]["status"] == "waiting_for_contact"
    assert pending["state"] == "WAITING_FOR_CONTACT"
    assert pending["customer_name"] is None
    assert pending["contact_phone"] == "0987121328"
    assert pending["start_dt"] == "2026-08-27T14:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_no_early_contact_with_date_stays_unchanged():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер"))
    time = await processor.process(_message("14"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert time["booking_result"]["status"] == "waiting_for_contact"
    assert pending["customer_name"] is None
    assert pending["contact_phone"] is None
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Хочу на чистку у четвер, після 15",
        "Хочу на чистку у четвер, якщо можна",
        "Хочу на чистку у четвер, будь ласка",
        "Хочу на чистку у четвер, бажано зранку",
        "Хочу на чистку у четвер, не знаю котра година",
        "Хочу на чистку у четвер, є інший час?",
        "Хочу на чистку у четвер, я зайнятий",
        "Хочу на чистку у четвер, не можу зараз",
        "Хочу на чистку у четвер, сьогодні мені 30",
        "Хочу на чистку у четвер, мені 14 років",
    ],
)
async def test_dental_early_trailing_prose_is_not_preserved_as_contact(text):
    processor, _calendar = _build_dental_processor()

    await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert pending is not None
    assert pending["customer_name"] is None
    assert pending["contact_phone"] is None
    assert pending["contact_email"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Хочу на чистку у четвер, 14",
        "Хочу на чистку у четвер, о 14",
        "Хочу на чистку у четвер, на 15:30",
        "Хочу на чистку у четвер, 15 30",
        "Хочу на чистку у четвер, 11 підійде",
        "Хочу на чистку у четвер, давайте 16",
        "Хочу на чистку у четвер, о 14 не можу",
        "Хочу на чистку у четвер, о 14 зайнятий",
    ],
)
async def test_dental_early_time_text_is_not_preserved_as_contact(text):
    processor, _calendar = _build_dental_processor()

    await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    if pending is not None:
        assert pending["customer_name"] is None
        assert pending["contact_phone"] is None
        assert pending["contact_email"] is None


@pytest.mark.asyncio
async def test_dental_service_booking_cleaning_tuesday_preserves_date_and_service():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("чистка у вівторок"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert result["booking_result"]["requested_date"] is not None
    assert pending["requested_day_label"] == "вівторок"
    assert pending["current_service_id"] == "dental_cleaning"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_booking_cleaning_tomorrow_enters_booking():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("можна на чистку завтра?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["requested_date"] is not None
    assert "Завтра, у неділю, клініка не працює" in result["reply_text"]
    assert "Найближчий робочий день" in result["reply_text"]
    assert pending["requested_date"] == "2026-08-24"
    assert pending["current_service_id"] == "dental_cleaning"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_booking_whitening_enters_booking():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("запишіть на відбілювання"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "teeth_whitening"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_booking_exact_datetime_uses_calendar_verification():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("на чистку у вівторок о 14"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert len(calendar.checked) == 1
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["start_dt"] == result["booking_result"]["start_dt"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_booking_time_window_uses_verified_slots():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("чистка в середу після 15"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert len(calendar.checked) > 0
    assert result["booking_result"]["suggested_slots"]
    assert pending["current_service_id"] == "dental_cleaning"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, expected_date, expected_label",
    [
        ("на пн", "2026-08-24", "понеділок"),
        ("на вт", "2026-08-25", "вівторок"),
        ("на ср", "2026-08-26", "середу"),
        ("на чт", "2026-08-27", "четвер"),
        ("на пт", "2026-08-28", "п’ятницю"),
        ("на сб", "2026-08-29", "суботу"),
        ("на нд", "2026-08-23", "неділю"),
        ("понеділок", "2026-08-24", "понеділок"),
        ("вівторок", "2026-08-25", "вівторок"),
        ("середа", "2026-08-26", "середу"),
        ("четвер", "2026-08-27", "четвер"),
        ("п'ятниця", "2026-08-28", "п’ятницю"),
        ("пятниця", "2026-08-28", "п’ятницю"),
        ("п’ятниця", "2026-08-28", "п’ятницю"),
        ("субота", "2026-08-29", "суботу"),
        ("неділя", "2026-08-23", "неділю"),
        ("неділя?", "2026-08-23", "неділю"),
        ("пт.", "2026-08-28", "п’ятницю"),
        ("пт?", "2026-08-28", "п’ятницю"),
        ("пт, після 15", "2026-08-28", "п’ятницю"),
    ],
)
async def test_dental_booking_weekday_parser_accepts_bounded_ukrainian_forms(
    dental_processor,
    text,
    expected_date,
    expected_label,
):
    processor, _calendar = dental_processor

    parsed = processor.booking_service._extract_requested_date(text)

    assert parsed["date"].isoformat() == expected_date
    assert parsed["day_label"] == expected_label


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["аптека", "пташка", "срібло", "втрачено", "сбоку"])
async def test_dental_booking_weekday_parser_rejects_substring_false_positives(
    dental_processor,
    text,
):
    processor, _calendar = dental_processor

    assert processor.booking_service._extract_requested_date(text) is None
    assert processor.booking_service._parse_requested_datetime(f"{text} о 14") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_date", "expected_time"),
    [
        ("3 вересня", "2026-09-03", None),
        ("3 вересня о 16", "2026-09-03", "16:00"),
        ("на 3 вересня", "2026-09-03", None),
        ("на 3 вересня о 16", "2026-09-03", "16:00"),
        ("03.09", "2026-09-03", None),
        ("03.09 о 16", "2026-09-03", "16:00"),
        ("3-го вересня", "2026-09-03", None),
    ],
)
async def test_dental_absolute_ukrainian_calendar_dates_parse(
    dental_processor,
    text,
    expected_date,
    expected_time,
):
    processor, _calendar = dental_processor

    parsed_date = processor.booking_service._extract_requested_date(text)
    parsed_dt = processor.booking_service._parse_requested_datetime(text)

    assert parsed_date["date"].isoformat() == expected_date
    if expected_time is None:
        assert parsed_dt is None
    else:
        assert parsed_dt.date().isoformat() == expected_date
        assert parsed_dt.strftime("%H:%M") == expected_time


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_date"),
    [
        ("5 січня", "2027-01-05"),
        ("12 лютого", "2027-02-12"),
        ("7 березня", "2027-03-07"),
        ("10 квітня", "2027-04-10"),
        ("8 травня", "2027-05-08"),
        ("4 червня", "2027-06-04"),
        ("9 липня", "2027-07-09"),
        ("11 серпня", "2027-08-11"),
        ("3 вересня", "2026-09-03"),
        ("6 жовтня", "2026-10-06"),
        ("14 листопада", "2026-11-14"),
        ("20 грудня", "2026-12-20"),
    ],
)
async def test_dental_absolute_ukrainian_calendar_dates_support_all_months(
    dental_processor,
    text,
    expected_date,
):
    processor, _calendar = dental_processor

    parsed = processor.booking_service._extract_requested_date(text)

    assert parsed["date"].isoformat() == expected_date


@pytest.mark.asyncio
async def test_dental_absolute_calendar_date_year_boundary(monkeypatch):
    class YearBoundaryDatetime(_REAL_DATETIME, metaclass=FixedDentalSmokeDatetimeMeta):
        @classmethod
        def now(cls, tz=None):
            fixed = _REAL_DATETIME(2026, 12, 30, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", YearBoundaryDatetime)
    processor, _calendar = _build_dental_processor()

    upcoming = processor.booking_service._extract_requested_date("31 грудня")
    rolled = processor.booking_service._extract_requested_date("20 грудня")

    assert upcoming["date"].isoformat() == "2026-12-31"
    assert rolled["date"].isoformat() == "2027-12-20"


@pytest.mark.asyncio
async def test_dental_booking_request_accepts_friday_abbreviation_date():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("хочу записатись на пт"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_service"
    assert result["booking_result"]["requested_date"] == "2026-08-28"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-28"
    assert pending["requested_day_label"] == "п’ятницю"


@pytest.mark.asyncio
async def test_dental_service_booking_preserves_service_with_friday_abbreviation():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("хочу на чистку в пт"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert result["booking_result"]["requested_date"] == "2026-08-28"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["requested_date"] == "2026-08-28"


@pytest.mark.asyncio
async def test_dental_service_time_window_uses_friday_abbreviation():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 28, 15),
        _kyiv_dt(2026, 8, 28, 15, 30),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("чистка пт після 15"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["requested_date"] == "2026-08-28"
    assert result["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-28T15:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-28T15:30:00+03:00"},
    ]
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["requested_date"] == "2026-08-28"


@pytest.mark.asyncio
async def test_dental_waiting_for_time_accepts_apostropheless_friday():
    processor, _calendar = _build_dental_processor()

    start = await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message("пятниця"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert result["booking_result"]["requested_date"] == "2026-08-28"
    assert pending["requested_date"] == "2026-08-28"
    assert pending["requested_day_label"] == "п’ятницю"


@pytest.mark.asyncio
async def test_dental_exact_time_uses_friday_abbreviation_calendar_validation():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 28, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = processor.booking_service.start_booking_flow(
        "patient-1",
        "чистка в пт о 14",
        source_channel="instagram",
        current_service_id="dental_cleaning",
        current_service_name="Професійна чистка зубів",
    )

    assert result["status"] == "waiting_for_contact"
    assert result["start_dt"] == "2026-08-28T14:00:00+03:00"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 28, 14), "duration_minutes": 30}
    ]


@pytest.mark.asyncio
async def test_dental_exact_time_friday_abbreviation_keeps_calendar_fail_closed():
    calendar = FailingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = processor.booking_service.start_booking_flow(
        "patient-1",
        "чистка в пт о 14",
        source_channel="instagram",
        current_service_id="dental_cleaning",
        current_service_name="Професійна чистка зубів",
    )

    assert result["status"] == "availability_check_failed"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 28, 14), "duration_minutes": 30}
    ]


@pytest.mark.asyncio
async def test_dental_service_context_survives_multiturn_booking():
    processor, calendar = _build_dental_processor()

    start = await processor.process(_message("хочу на чистку"))
    date = await processor.process(_message("у четвер"))
    window = await processor.process(_message("після 15"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert date["booking_result"]["status"] == "waiting_for_time"
    assert window["booking_result"]["status"] == "time_window_slots_suggested"
    assert len(calendar.checked) > 0
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["requested_day_label"] == "четвер"
    _assert_no_flowly_leakage(start["reply_text"], date["reply_text"], window["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_price_followup_during_booking_preserves_booking_context():
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    price = await processor.process(_message("а скільки вона коштує?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert price["intent"] == "booking_grounded_question"
    assert "1800 грн" in price["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_cleaning"
    _assert_no_flowly_leakage(price["reply_text"])


@pytest.mark.asyncio
async def test_dental_additional_wife_during_waiting_for_time_preserves_current_booking():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    before = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    checks_before = len(calendar.checked)
    creates_before = len(calendar.created)

    result = await processor.process(_message("а ще дружину можна записати?"))
    after = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "additional_person_booking_deferred"
    assert "окремо" in result["reply_text"].lower()
    assert "котра година" in result["reply_text"].lower()
    assert after == before
    assert len(calendar.checked) == checks_before
    assert len(calendar.created) == creates_before
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_additional_child_during_waiting_for_time_does_not_switch_to_pediatric():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    before = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    checks_before = len(calendar.checked)
    creates_before = len(calendar.created)

    result = await processor.process(_message("а дитину теж можна записати?"))
    after = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "additional_person_booking_deferred"
    assert after == before
    assert after["current_service_id"] == "dental_cleaning"
    assert after["requested_date"] == "2026-08-26"
    assert len(calendar.checked) == checks_before
    assert len(calendar.created) == creates_before
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_additional_child_during_waiting_for_contact_preserves_slot_and_service():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    await processor.process(_message("о 15"))
    before = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    checks_before = len(calendar.checked)
    creates_before = len(calendar.created)

    result = await processor.process(_message("а дитину теж можна записати?"))
    after = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "additional_person_booking_deferred"
    assert "ім’я та номер телефону" in result["reply_text"]
    assert after == before
    assert after["state"] == "WAITING_FOR_CONTACT"
    assert after["current_service_id"] == "dental_cleaning"
    assert after["start_dt"] == "2026-08-26T15:00:00+03:00"
    assert len(calendar.checked) == checks_before
    assert len(calendar.created) == creates_before
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_additional_person_guard_does_not_hijack_service_correction():
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))

    result = await processor.process(_message("ні, я не на чистку, а на відбілювання"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_flow"
    assert pending["current_service_id"] == "teeth_whitening"
    assert pending["requested_date"] == "2026-08-26"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_additional_person_guard_does_not_hijack_date_correction():
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))

    result = await processor.process(_message("а краще в четвер"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_flow"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["requested_date"] == "2026-08-27"
    assert pending["requested_day_label"] == "четвер"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_fresh_pediatric_booking_is_not_additional_person_guarded():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("хочу записати дитину на чистку"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_request"
    assert pending["current_service_id"] == "pediatric_dentistry"
    assert pending["current_service_name"] == "Дитяча чистка зубів"
    assert result["intent"] != "additional_person_booking_deferred"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_price_faq_does_not_enter_booking(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("скільки коштує чистка"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert result["booking_result"] is None
    assert "1800 грн" in result["reply_text"]
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_description_faq_does_not_enter_booking(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("що входить у чистку"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert result["booking_result"] is None
    assert "Комплексна чистка зубів" in result["reply_text"]
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_bare_service_mention_does_not_enter_booking(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("чистка"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert result["booking_result"] is None
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_availability_faq_does_not_enter_booking(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("є чистка?"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert result["booking_result"] is None
    assert "Професійна гігієна" in result["reply_text"]
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "хочу полікувати зуби",
        "треба лікування",
        "болить зуб",
        "хочу до стоматолога",
        "треба глянути зуб",
    ],
)
async def test_dental_ambiguous_requests_do_not_confidently_select_cleaning(text):
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}
    context = processor.memory_service.get_context("patient-1")

    assert "Професійна гігієна" not in result["reply_text"]
    assert "1800 грн" not in result["reply_text"]
    assert pending.get("current_service_id") != "dental_cleaning"
    assert context.get("current_service_id") != "dental_cleaning"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_ambiguous_treatment_with_friday_preserves_date_without_cleaning():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("хочу полікувати зуби в п'ятницю"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_service"
    assert result["booking_result"]["requested_date"] == "2026-08-28"
    assert pending["requested_day_label"] == "п’ятницю"
    assert pending.get("current_service_id") is None
    assert "Професійна гігієна" not in result["reply_text"]
    assert "1800 грн" not in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_pain_symptom_does_not_become_service_summary_but_diagnosis_safety_still_works():
    processor, _calendar = _build_dental_processor()

    pain = await processor.process(_message("болить зуб"))
    diagnosis = await processor.process(_message("болить зуб шо робити", sender_id="patient-2"))
    emergency = await processor.process(_message("кров тече", sender_id="patient-3"))

    assert pain["intent"] == "pain_acknowledgement"
    assert "Лікування карієсу" not in pain["reply_text"]
    assert "Професійна гігієна" not in pain["reply_text"]
    assert diagnosis["intent"] == "diagnosis_request"
    assert "діагноз" in diagnosis["reply_text"].lower()
    assert emergency["intent"] == "medical_emergency"
    assert "невідкладна" in emergency["reply_text"].lower()
    _assert_no_flowly_leakage(pain["reply_text"], diagnosis["reply_text"], emergency["reply_text"])


@pytest.mark.asyncio
async def test_dental_cleaning_natural_alias_still_enters_booking():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("хочу почистити зуби"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "dental_cleaning"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_filling_alias_maps_to_caries_treatment_when_grounded():
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message("хочу поставити пломбу"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "caries_treatment"
    assert pending["current_service_id"] != "dental_cleaning"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_pediatric_question_remains_grounded_after_confidence_tightening(dental_processor):
    processor, _calendar = dental_processor

    result = await processor.process(_message("є дитячий стоматолог"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Дитяча стоматологія" in result["reply_text"]
    assert "700 грн" in result["reply_text"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_price", "expected_service_id"),
    [
        ("дитині 5 років, скільки коштує лікування карієсу?", "1800 грн", "pediatric_caries_treatment"),
        ("скільки коштує лікування карієсу дитині?", "1800 грн", "pediatric_caries_treatment"),
        ("скільки лікування карієсу дитині?", "1800 грн", "pediatric_caries_treatment"),
        ("скільки коштує чистка дитині?", "1200 грн", "pediatric_cleaning"),
    ],
)
async def test_dental_pediatric_service_prices_use_pediatric_topics(
    text,
    expected_price,
    expected_service_id,
):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert expected_price in result["reply_text"]
    assert context["current_service_id"] == expected_service_id
    assert result["booking_result"] is None
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_pending_name", "expected_topic_id"),
    [
        ("дитині 5 років, треба полікувати карієс", "Лікування карієсу у дітей", "pediatric_caries_treatment"),
        ("хочу записати дитину на лікування карієсу", "Лікування карієсу у дітей", "pediatric_caries_treatment"),
        ("хочу на дитячу чистку", "Дитяча чистка зубів", "pediatric_cleaning"),
        ("хочу записати дитину на чистку", "Дитяча чистка зубів", "pediatric_cleaning"),
        ("хочу записати доньку на чистку, їй 5 років", "Дитяча чистка зубів", "pediatric_cleaning"),
    ],
)
async def test_dental_pediatric_booking_topics_redirect_to_pediatric_dentistry(
    text,
    expected_pending_name,
    expected_topic_id,
):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "pediatric_dentistry"
    assert pending["current_service_name"] == expected_pending_name
    assert context["current_service_id"] == expected_topic_id
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "дитині 5 років",
        "доньці 5 років",
    ],
)
async def test_dental_pediatric_marker_alone_does_not_fabricate_specific_treatment(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    context = processor.memory_service.get_context("patient-1")

    assert context.get("current_service_id") not in {
        "pediatric_caries_treatment",
        "pediatric_cleaning",
    }
    assert result["booking_result"] is None
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_child_age_acceptance_question_resolves_pediatric_context_only():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("синові 4 роки, приймаєте таких малих?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert context["current_service_id"] == "pediatric_dentistry"
    assert "Дитяча стоматологія" in result["reply_text"]
    assert "від 3 років" in result["reply_text"] or "спокійному темпі" in result["reply_text"]
    assert "Лікування карієсу у дітей" not in result["reply_text"]
    assert "Дитяча чистка" not in result["reply_text"]
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "дитина 7 років, дуже боїться лікарів, чи можна до вас",
        "доньці 6 років, чи можна до вас?",
        "син 8 років боїться лікарів, приймаєте дітей?",
    ],
)
async def test_dental_child_age_and_care_context_resolves_generic_pediatric_only(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert context["current_service_id"] == "pediatric_dentistry"
    assert "Дитяча стоматологія" in result["reply_text"]
    assert "від 3 років" in result["reply_text"] or "спокійному темпі" in result["reply_text"]
    assert "Лікування карієсу у дітей" not in result["reply_text"]
    assert "Дитяча чистка" not in result["reply_text"]
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_specific_pediatric_service_beats_generic_child_context():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("дитина 7 років, треба полікувати карієс"))
    context = processor.memory_service.get_context("patient-1")
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_request"
    assert context["current_service_id"] == "pediatric_caries_treatment"
    assert pending["current_service_id"] == "pediatric_dentistry"
    assert pending["current_service_name"] == "Лікування карієсу у дітей"
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "мені 34 роки, чи можна до вас?",
        "у нас 7 людей, чи можна до вас?",
    ],
)
async def test_dental_adult_age_and_plain_numbers_do_not_resolve_pediatric_context(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    context = processor.memory_service.get_context("patient-1")

    assert context.get("current_service_id") != "pediatric_dentistry"
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_child_age_without_action_does_not_fabricate_pediatric_context():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("синові 4 роки"))
    context = processor.memory_service.get_context("patient-1")

    assert context.get("current_service_id") is None
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_child_booking_without_specific_service_uses_pediatric_consultation():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("хочу записати дитину"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "pediatric_dentistry"
    assert context["current_service_id"] == "pediatric_dentistry"
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_price", "expected_service_id"),
    [
        ("скільки коштує лікування карієсу?", "2200 грн", "caries_treatment"),
        ("скільки коштує чистка?", "1800 грн", "dental_cleaning"),
    ],
)
async def test_dental_adult_service_prices_stay_on_adult_topics(
    text,
    expected_price,
    expected_service_id,
):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert expected_price in result["reply_text"]
    assert context["current_service_id"] == expected_service_id
    assert result["booking_result"] is None
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_service_id", "expected_service_name"),
    [
        ("хочу на чистку", "dental_cleaning", "Професійна гігієна зубів"),
        ("хочу лікувати карієс", "caries_treatment", "Лікування карієсу"),
    ],
)
async def test_dental_adult_booking_phrases_stay_on_adult_topics(
    text,
    expected_service_id,
    expected_service_name,
):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == expected_service_id
    assert pending["current_service_name"] == expected_service_name
    assert context["current_service_id"] == expected_service_id
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_veneers_contextual_followups_use_remembered_topic():
    processor, calendar = _build_dental_processor()

    service = await processor.process(_message("чи робите вініри?"))
    price = await processor.process(_message("скільки один?"))
    trimming = await processor.process(_message("а треба обточувати?"))
    context = processor.memory_service.get_context("patient-1")

    assert "встановлюють керамічні вініри" in service["reply_text"]
    assert "12000 грн" in price["reply_text"]
    assert "ортопед" in trimming["reply_text"]
    assert context["current_service_id"] == "veneers"
    assert calendar.created == []
    _assert_no_flowly_leakage(service["reply_text"], price["reply_text"], trimming["reply_text"])


@pytest.mark.asyncio
async def test_dental_veneers_contextual_faq_requires_named_detail_overlap():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("чи робите вініри?"))
    result = await processor.process(_message("можна за один день?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_safe_fallback"
    assert "Це можливо" not in result["reply_text"]
    assert "два передні зуби" not in result["reply_text"]
    assert result["booking_result"] is None
    assert context["current_service_id"] == "veneers"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_crown_contextual_followups_use_options_and_safe_faq():
    processor, calendar = _build_dental_processor()

    options = await processor.process(_message("які коронки є?"))
    zirconia = await processor.process(_message("а цирконієва скільки?"))
    best = await processor.process(_message("яка краща?"))
    context = processor.memory_service.get_context("patient-1")

    assert "Металокерамічна коронка" in options["reply_text"]
    assert "Цирконієва коронка" in options["reply_text"]
    assert "9000 грн" in zirconia["reply_text"]
    assert "Не обираємо матеріал дистанційно" in best["reply_text"]
    assert context["current_service_id"] == "dental_crowns"
    assert calendar.created == []
    _assert_no_flowly_leakage(options["reply_text"], zirconia["reply_text"], best["reply_text"])


@pytest.mark.asyncio
async def test_dental_implant_contextual_followups_do_not_switch_to_crowns():
    processor, calendar = _build_dental_processor()

    price = await processor.process(_message("скільки коштує імплант?"))
    crown = await processor.process(_message("коронка входить?"))
    suitability = await processor.process(_message("давно немає зуба, можна поставити?"))
    context = processor.memory_service.get_context("patient-1")

    assert "16000 грн" in price["reply_text"]
    assert "розраховується окремо" in crown["reply_text"]
    assert "рентген-діагностики" in suitability["reply_text"]
    assert context["current_service_id"] == "dental_implant"
    assert calendar.created == []
    _assert_no_flowly_leakage(price["reply_text"], crown["reply_text"], suitability["reply_text"])


@pytest.mark.asyncio
async def test_dental_braces_contextual_followups_do_not_switch_to_crowns():
    processor, calendar = _build_dental_processor()

    service = await processor.process(_message("брекети ставите?"))
    metal = await processor.process(_message("а металеві скільки?"))
    duration = await processor.process(_message("скільки носити?"))
    context = processor.memory_service.get_context("patient-1")

    assert "ортодонтичне лікування брекетами" in service["reply_text"]
    assert "19000 грн" in metal["reply_text"]
    assert "за щелепу" not in metal["reply_text"]
    assert "Термін лікування залежить" in duration["reply_text"]
    assert context["current_service_id"] == "braces"
    assert calendar.created == []
    _assert_no_flowly_leakage(service["reply_text"], metal["reply_text"], duration["reply_text"])


@pytest.mark.asyncio
async def test_dental_supported_service_question_answers_directly_and_pronoun_followup_stays_contextual():
    processor, calendar = _build_dental_processor()

    service = await processor.process(_message("підкажіть, а ви ставите брекети?"))
    followup = await processor.process(_message("а ви робите це?"))
    context = processor.memory_service.get_context("patient-1")

    assert service["intent"] == "front_desk_contextual_answer"
    assert service["reply_text"].startswith("Так, у нас можна пройти ортодонтичне лікування брекетами.")
    assert "консультація ортодонта" in service["reply_text"]
    assert followup["intent"] == "front_desk_contextual_answer"
    assert followup["reply_text"].startswith("Так, у нас можна пройти ортодонтичне лікування брекетами.")
    assert "Що саме ви маєте на увазі" not in followup["reply_text"]
    assert context["current_service_id"] == "braces"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_supported_service_question_tolerates_polite_typo():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("ітаю"))
    result = await processor.process(_message("Підажіть, ви ставите брекети?"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert result["reply_text"].startswith("Так, у нас можна пройти ортодонтичне лікування брекетами.")
    assert "у базі немає підтвердження" not in result["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_inflected_colloquial_service_wording_resolves_braces():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("підкажіть по брекетам"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert context["current_service_id"] == "braces"
    assert "брекет" in result["reply_text"].lower()
    assert "у базі немає підтвердження" not in result["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_inflection_fix_does_not_fabricate_service_from_generic_words():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("підкажіть по варіантам"))
    context = processor.memory_service.get_context("patient-1")

    assert context.get("current_service_id") is None
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question, expected_phrase",
    [
        ("ви робите чистку?", "можна зробити професійну гігієну"),
        ("ви лікуєте карієс?", "лікують карієс"),
        ("ви ставите вініри?", "встановлюють керамічні вініри"),
        ("ви ставите коронки?", "ставлять коронки"),
        ("ви робите імпланти?", "встановлюють зубні імпланти"),
        ("ви лікуєте канали?", "лікують кореневі канали"),
        ("ви видаляєте зуби мудрості?", "видаляють зуби мудрості"),
    ],
)
async def test_dental_supported_service_availability_replies_are_direct_and_natural(question, expected_phrase):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(question))

    assert result["intent"] == "front_desk_contextual_answer"
    assert result["reply_text"].startswith(("Так,", "Седація"))
    assert expected_phrase in result["reply_text"]
    assert "у базі немає підтвердження" not in result["reply_text"].lower()
    assert "Що саме ви маєте на увазі" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_jaw_price_unit_followup_does_not_hijack_to_xray():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("ви ставите брекети?"))
    await processor.process(_message("яка вартість?"))
    result = await processor.process(_message("за щелепу то за всі зуби чи за верхню і нижню?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "за одну щелепу" in result["reply_text"]
    assert "не за всі зуби одразу" in result["reply_text"]
    assert "Рентген-діагностика" not in result["reply_text"]
    assert "у базі немає підтвердження" not in result["reply_text"].lower()
    assert context["current_service_id"] == "braces"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_root_canal_contextual_followups_stay_safe():
    processor, calendar = _build_dental_processor()

    service = await processor.process(_message("лікуєте канали?"))
    microscope = await processor.process(_message("під мікроскопом?"))
    diagnosis = await processor.process(_message("це точно пульпіт?"))
    context = processor.memory_service.get_context("patient-1")

    assert "лікують кореневі канали" in service["reply_text"]
    assert "мікроскопом" in microscope["reply_text"]
    assert diagnosis["intent"] == "diagnosis_request"
    assert "Не можу поставити діагноз" in diagnosis["reply_text"]
    assert context["current_service_id"] == "root_canal_treatment"
    assert calendar.created == []
    _assert_no_flowly_leakage(service["reply_text"], microscope["reply_text"], diagnosis["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second", "expected_topic", "expected_booking_service"),
    [
        ("скільки коштує імплант?", "а які коронки у вас є?", "dental_crowns", "prosthetics_consultation"),
        ("брекети ставите?", "а вініри робите?", "veneers", "prosthetics_consultation"),
        ("вініри робите?", "хочу на чистку", "dental_cleaning", "dental_cleaning"),
    ],
)
async def test_dental_contextual_followups_allow_explicit_topic_switches(
    first,
    second,
    expected_topic,
    expected_booking_service,
):
    processor, calendar = _build_dental_processor()

    await processor.process(_message(first))
    result = await processor.process(_message(second))
    context = processor.memory_service.get_context("patient-1")
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert context["current_service_id"] == expected_topic
    assert (
        pending.get("current_service_id")
        or processor._effective_booking_service_id(context["current_service_id"])
    ) == expected_booking_service
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_implant_relation_followups_do_not_hijack_to_crowns_or_extraction():
    processor, calendar = _build_dental_processor()

    price = await processor.process(_message("Скільки у вас коштує імплант?"))
    crown = await processor.process(_message("Це разом з коронкою чи окремо?"))
    duration = await processor.process(_message("А скільки часу взагалі займає весь процес?"))
    removed = await processor.process(_message("У мене зуб вже видалений"))
    context = processor.memory_service.get_context("patient-1")

    assert "16000 грн" in price["reply_text"]
    assert "Коронка" in crown["reply_text"]
    assert "окрем" in crown["reply_text"]
    assert "Імплантація" in duration["reply_text"]
    assert "Імплантація" in removed["reply_text"]
    assert context["current_service_id"] == "dental_implant"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_aligners_comparison_followups_do_not_hijack_to_braces():
    processor, calendar = _build_dental_processor()

    service = await processor.process(_message("підкажіть елайнери робите?"))
    comparison = await processor.process(_message("це типу замість брекетів так?"))
    price = await processor.process(_message("а по ціні сильно дорожче?"))
    context = processor.memory_service.get_context("patient-1")

    assert "ортодонтичне лікування елайнерами" in service["reply_text"]
    assert "Елайнери" in comparison["reply_text"]
    assert "60000 грн" in price["reply_text"]
    assert context["current_service_id"] == "aligners"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_price_then_aligners_cheaper_comparison_answers_aligners_price():
    """Repro: after a брекети price answer, "а елайнери дешевші?" must be
    understood as a comparison naming aligners -- and answered with
    aligners' own grounded price, not the previous braces summary/price."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message("А елайнери дешевші?"))
    context = processor.memory_service.get_context("patient-1")

    assert "60000 грн" in result["reply_text"]
    assert "Брекети встановлюють" not in result["reply_text"]
    assert context["current_service_id"] == "aligners"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_price_then_ceramic_more_expensive_narrows_to_ceramic_option():
    """"А керамічні дорожчі?" names a price *option* of the same
    already-discussed service (brackets), not a different service -- must
    narrow to just the ceramic price, not repeat the full brackets price
    list or fall back to a generic summary."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message("А керамічні дорожчі?"))

    assert "30000 грн" in result["reply_text"]
    assert "19000 грн" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_price_then_aligners_how_much_answers_aligners_price():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message("А елайнери скільки?"))
    context = processor.memory_service.get_context("patient-1")

    assert "60000 грн" in result["reply_text"]
    assert context["current_service_id"] == "aligners"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_price_then_what_is_cheaper_repeats_grounded_known_price():
    """With no alternative named, "а що дешевше?" must not invent a
    comparison verdict -- restating the already-known grounded price is
    the safe, non-inventing answer."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message("А що дешевше?"))

    assert "19000 грн" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_price_then_implant_cheaper_uses_implant_grounded_price():
    """Adversarial: the comparison target can be any other real service,
    not just aligners -- must never be hardcoded to one phrase/service."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message("А імпланти дешевші?"))
    context = processor.memory_service.get_context("patient-1")

    assert "16000 грн" in result["reply_text"]
    assert "Прямо порівняти" not in result["reply_text"]
    assert context["current_service_id"] == "dental_implant"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_price_then_aligners_cheaper_states_not_directly_comparable():
    """Braces (per_jaw) vs aligners (per_course) use different price units
    -- must never render a yes/no cheaper/more-expensive verdict, only the
    explicit "not directly comparable" framing with both sides' own,
    verbatim grounded price text (no derived amount, no derived unit
    phrase)."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message("А елайнери дешевші?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["reply_text"] == (
        "Прямо порівняти ці ціни некоректно: Металеві брекети — від 19000 грн. "
        "Елайнери коштують від 60000 грн за курс лікування."
    )
    assert context["current_service_id"] == "aligners"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_price_then_aligners_more_expensive_states_not_directly_comparable():
    """Same differing-unit pair, asked with "дорожчі" instead of "дешевші"
    -- the verdict must still be withheld (unit mismatch, not direction of
    the question, is what makes this incomparable), and the message must
    be identical regardless of which direction was asked."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message("А елайнери дорожчі?"))

    assert result["reply_text"] == (
        "Прямо порівняти ці ціни некоректно: Металеві брекети — від 19000 грн. "
        "Елайнери коштують від 60000 грн за курс лікування."
    )
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_option_comparison_beats_stale_aligners_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштують металеві брекети?"))
    await processor.process(_message("а це дорожче за елайнери?"))
    result = await processor.process(_message("а керамічні дешевше за металеві?"))
    context = processor.memory_service.get_context("patient-1")

    assert "Металеві брекети — від 19000 грн" in result["reply_text"]
    assert "Керамічні брекети — від 30000 грн" in result["reply_text"]
    assert "Елайнери коштують" not in result["reply_text"]
    assert context["current_service_id"] == "braces"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_price_then_aligners_question_still_switches_to_aligners():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштують металеві брекети?"))
    result = await processor.process(_message("А елайнери?"))
    context = processor.memory_service.get_context("patient-1")

    assert "Елайнери коштують від 60000 грн" in result["reply_text"]
    assert context["current_service_id"] == "aligners"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_unrelated_stale_context_cannot_hijack_braces_option_comparison():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштують елайнери?"))
    result = await processor.process(_message("а керамічні дешевше за металеві?"))
    context = processor.memory_service.get_context("patient-1")

    assert "Металеві брекети — від 19000 грн" in result["reply_text"]
    assert "Керамічні брекети — від 30000 грн" in result["reply_text"]
    assert "Елайнери коштують" not in result["reply_text"]
    assert context["current_service_id"] == "braces"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_aligners_price_then_braces_cheaper_swaps_labels_correctly():
    """Reverse starting point of the differing-unit pair: must not be
    hardcoded to a fixed braces-then-aligners direction -- the previously
    discussed service (aligners) and the newly named one (braces) must
    swap places correctly in the rendered comparison."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки коштують елайнери?"))
    result = await processor.process(_message("А брекети дешевші?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["reply_text"] == (
        "Прямо порівняти ці ціни некоректно: Елайнери коштують від 60000 грн за курс "
        "лікування. Металеві брекети — від 19000 грн"
    )
    assert context["current_service_id"] == "braces"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_price_comparison_same_unit_returns_no_verdict():
    """When both compared services share the same structured price_unit,
    no comparison message is produced at all (for now) -- the safe
    existing unit-less behavior applies instead. No two real KB services
    currently share a price_unit across different service ids, so this
    exercises the check directly against synthetic same-unit entries,
    with no price amounts involved."""
    knowledge_service = KnowledgeService("app/data/knowledge_base.json")
    service_a = {"id": "svc_a", "name": "Сервіс А", "price_note": "від 1000 грн", "price_unit": "per_tooth"}
    service_b = {"id": "svc_b", "name": "Сервіс Б", "price_note": "від 2000 грн", "price_unit": "per_tooth"}

    assert knowledge_service._build_price_comparison_reply(service_b, service_a) is None


@pytest.mark.asyncio
async def test_dental_faq_comparison_during_waiting_for_time_preserves_booking_state():
    """The differing-unit comparative verdict must be answerable mid-booking
    (WAITING_FOR_TIME) without disturbing the already-collected service or
    date -- the very next genuine time reply must still resume the
    original booking for the original service."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатись на чистку зубів"))
    await processor.process(_message("у понеділок"))
    await processor.process(_message("Скільки коштують металеві брекети?"))
    comparison_result = await processor.process(_message("А елайнери дешевші?"))
    mid_pending = processor.booking_service._get_pending_confirmation("patient-1") or {}
    await processor.process(_message("о 15"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert comparison_result["reply_text"] == (
        "Прямо порівняти ці ціни некоректно: Металеві брекети — від 19000 грн. "
        "Елайнери коштують від 60000 грн за курс лікування."
    )
    assert mid_pending.get("current_service_id") == "dental_cleaning"
    assert pending.get("current_service_id") == "dental_cleaning"
    assert pending.get("start_dt") is not None
    assert len(calendar.checked) == 1
    assert calendar.checked[0]["start_dt"].hour == 15
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_fragment"),
    [
        ("А елайнери?", "60000 грн"),
        ("А імпланти?", "16000 грн"),
        ("А відбілювання?", "4500 грн"),
        ("А зуб мудрості?", "3500 грн"),
        ("Елайнери?", "60000 грн"),
        ("А елайнери дешевші?", "60000 грн"),
        ("а скок елайнери?", "60000 грн"),
    ],
)
async def test_dental_bare_service_question_during_active_booking_answers_faq_not_switch(text, expected_fragment):
    """A bare "X?" naming a different service while a booking is already
    in progress (WAITING_FOR_TIME) must be answered as an FAQ/comparison
    follow-up -- not swallowed into a booking-payload/switch attempt that
    falls back to the generic clarify prompt."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатись на консультацію"))
    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message(text))

    assert expected_fragment in result["reply_text"]
    assert "Хочу правильно зорієнтувати" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["краще елайнери", "хочу на елайнери", "давайте елайнери", "ні, елайнери"],
)
async def test_dental_explicit_service_switch_during_active_booking_still_works(text):
    """Explicit/likely switch phrasing (no bare "?") during an active
    booking must keep resuming the booking flow for the new service --
    completely unaffected by the bare-question bypass, since none of
    these end in "?"."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатись на консультацію"))
    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message(text))

    assert "Хочу правильно зорієнтувати" not in result["reply_text"]
    assert "який день і приблизний час" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["а може краще елайнери?", "можна елайнери?", "давайте елайнери?"],
)
async def test_dental_ambiguous_question_plus_switch_wording_stays_unchanged(text):
    """Known, deliberately unresolved ambiguity: a "?"-ending message that
    ALSO carries a correction/action/suggestion cue ("краще", "можна",
    "давайте") is excluded from the bare-question bypass by design, so it
    keeps whatever behavior it already had before this patch (currently
    the generic clarify fallback) -- this locks that choice in rather than
    silently drifting if the bypass is ever broadened."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатись на консультацію"))
    await processor.process(_message("Скільки коштують металеві брекети?"))
    result = await processor.process(_message(text))

    assert "Хочу правильно зорієнтувати" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_faq_interruption_during_active_booking_preserves_pending_date_and_service():
    """A bare-question FAQ interruption mid-booking must not erase the
    already-collected day -- the very next genuine time reply must still
    check availability for the originally requested service and date."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатись на чистку зубів"))
    await processor.process(_message("у понеділок"))
    faq_result = await processor.process(_message("А елайнери?"))
    continuation = await processor.process(_message("о 15"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert "Хочу правильно зорієнтувати" not in faq_result["reply_text"]
    assert pending.get("current_service_id") == "dental_cleaning"
    assert pending.get("start_dt") is not None
    assert len(calendar.checked) == 1
    assert calendar.checked[0]["start_dt"].hour == 15
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_faq_interruptions_after_confirmed_booking_cause_no_calendar_mutation():
    """FAQ/comparison questions asked after a booking is already confirmed
    must never touch the Calendar -- no create, update, or delete."""
    calendar = ContactUpdateTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor, event_id="calendar-event-999")

    for text in ["А елайнери?", "А імпланти?", "а скок елайнери?", "А елайнери дешевші?"]:
        await processor.process(_message(text))

    assert calendar.created == []
    assert calendar.contact_updates == []


@pytest.mark.asyncio
async def test_dental_explicit_supported_service_switch_beats_previous_topic():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("ви ставите брекети?"))
    result = await processor.process(_message("а елайнери?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert context["current_service_id"] == "aligners"
    assert "Елайнери" in result["reply_text"]
    assert "Брекети" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_explicit_service_correction_beats_previous_topic_and_starts_booking():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує відбілювання?"))
    result = await processor.process(_message("ні, я переплутала, мені чистка"))
    context = processor.memory_service.get_context("patient-1")
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert context["current_service_id"] == "dental_cleaning"
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_contextual_consultation_price_uses_specialty_booking_service():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Добрий день, а брекети у вас ставлять?"))
    consultation = await processor.process(_message("Скільки коштує така консультація?"))
    context = processor.memory_service.get_context("patient-1")

    assert "Консультація ортодонта" in consultation["reply_text"]
    assert "700 грн" in consultation["reply_text"]
    assert context["current_service_id"] == "orthodontic_consultation"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_veneers_contextual_front_teeth_followup_preserves_topic():
    processor, calendar = _build_dental_processor()

    price = await processor.process(_message("Скільки приблизно коштує вінір?"))
    front_teeth = await processor.process(_message("А якщо на передні зуби?"))
    context = processor.memory_service.get_context("patient-1")

    assert "12000 грн" in price["reply_text"]
    assert "кількість вінірів" in front_teeth["reply_text"]
    assert context["current_service_id"] == "veneers"
    assert calendar.checked == []
    assert calendar.created == []
    _assert_no_flowly_leakage(price["reply_text"], front_teeth["reply_text"])


@pytest.mark.asyncio
async def test_dental_unclear_patient_need_is_triaged_to_consultation_not_greeting():
    processor, calendar = _build_dental_processor()

    unclear = await processor.process(_message("Привіт, я навіть не знаю до кого мені треба"))
    symptom = await processor.process(_message("Один зуб став темніший за інші"))
    booking = await processor.process(_message("Я просто хочу щоб лікар подивився і сказав що з ним"))
    context = processor.memory_service.get_context("patient-1")

    assert unclear["intent"] == "dental_triage"
    assert "що турбує" in unclear["reply_text"]
    assert "огляді стоматолога" in symptom["reply_text"]
    assert "діагноз" in symptom["reply_text"]
    assert booking["booking_result"]["status"] == "waiting_for_time"
    assert context["current_service_id"] == "dental_consultation"
    assert calendar.created == []
    _assert_no_flowly_leakage(unclear["reply_text"], symptom["reply_text"], booking["reply_text"])


@pytest.mark.asyncio
async def test_dental_whiter_teeth_clarifies_then_resolves_whitening_not_cleaning():
    processor, calendar = _build_dental_processor()

    clarification = await processor.process(_message("Привіт, хочу зробити зуби білішими"))
    whitening = await processor.process(_message("Мені не чистка напевно, а саме відбілювання"))
    consultation = await processor.process(_message("Перед ним треба консультація?"))
    context = processor.memory_service.get_context("patient-1")

    assert "чистку чи відбілювання" in clarification["reply_text"]
    assert "Відбілювання зубів" in whitening["reply_text"]
    assert "4500 грн" in whitening["reply_text"]
    assert whitening.get("booking_result") is None
    assert "перед процедурою рекомендований огляд" in consultation["reply_text"]
    assert context["current_service_id"] == "teeth_whitening"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_wisdom_tooth_symptom_detail_preserves_extraction_context():
    processor, calendar = _build_dental_processor()

    booking = await processor.process(_message("Добрий день, у мене зуб мудрості заважає, хочу видалити"))
    partial = await processor.process(_message("Він ще не повністю виліз"))
    context = processor.memory_service.get_context("patient-1")

    assert booking["booking_result"]["status"] == "waiting_for_time"
    assert "у базі немає підтвердження" not in booking["reply_text"]
    assert "рентген" in partial["reply_text"] or "КТ" in partial["reply_text"]
    assert context["current_service_id"] == "wisdom_tooth_extraction"
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_service_id", "expected_reply"),
    [
        ("видалення зуба", "tooth_extraction", "1800 грн"),
        ("видалення зуба мудрості", "wisdom_tooth_extraction", "3500 грн"),
        ("скільки коштує видалення зуба мудрості", "wisdom_tooth_extraction", "3500 грн"),
        ("зуб мудрості видалити хочу", "wisdom_tooth_extraction", None),
    ],
)
async def test_dental_extraction_specificity_prefers_distinguishing_detail(
    text,
    expected_service_id,
    expected_reply,
):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    context = processor.memory_service.get_context("patient-1")

    assert context["current_service_id"] == expected_service_id
    if expected_reply is not None:
        assert expected_reply in result["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_good_evening_greeting_does_not_become_evening_time_preference():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Добрий вечір, хочу завтра показати зуб лікарю"))
    context = processor.memory_service.get_context("patient-1")

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "вечірніх слотів" not in result["reply_text"].lower()
    assert "Завтра, у неділю, клініка не працює" in result["reply_text"]
    assert "Найближчий робочий день" in result["reply_text"]
    assert context["current_service_id"] == "dental_consultation"
    assert calendar.checked
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "чистка у вівторок дорожча?",
        "у вівторок чистка скільки коштує?",
        "чистку робите у суботу?",
        "у неділю є чистка?",
    ],
)
async def test_dental_service_date_faq_does_not_enter_booking(text):
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "front_desk_contextual_answer"
    assert result["booking_result"] is None
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_booking_service_survives_different_service_faq_interruption():
    processor, _calendar = _build_dental_processor()

    start = await processor.process(_message("хочу на чистку"))
    faq = await processor.process(_message("а скільки коштує відбілювання?"))
    resume = await processor.process(_message("давай 16:00"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert faq["intent"] == "booking_grounded_question"
    assert "4500 грн" in faq["reply_text"]
    assert resume["intent"] == "booking_flow"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["current_service_name"] == "Професійна гігієна зубів"
    _assert_no_flowly_leakage(start["reply_text"], faq["reply_text"], resume["reply_text"])


@pytest.mark.asyncio
async def test_dental_service_booking_calendar_failure_fails_closed():
    calendar = FailingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("на чистку у вівторок о 14"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "availability_check_failed"
    assert len(calendar.checked) == 1
    assert "вільний" not in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_active_booking_faq_interruption_preserves_booking_context(dental_processor):
    processor, _calendar = dental_processor

    await processor.process(_message("хочу записатись"))
    faq = await processor.process(_message("скільки коштує відбілювання?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert faq["intent"] == "booking_grounded_question"
    assert "4500" in faq["reply_text"]
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
async def test_dental_bot_identity_question_answers_honestly_without_sales_copy():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("це бот чи людина?"))

    assert result["intent"] == "bot_identity_question"
    assert "AI-асистент Smile Dental Clinic" in result["reply_text"]
    assert "типові питання" in result["reply_text"]
    assert "адміністратора" in result["reply_text"]
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_human_handoff_request_is_acknowledged_without_fake_transfer():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("не хочу з ботом говорити, дайте живу людину будь ласка"))

    assert result["intent"] == "human_handoff_request"
    assert result["routing_category"] == "safe_handoff"
    assert "AI-асистент" in result["reply_text"]
    assert "адміністратор" in result["reply_text"]
    assert "повернеться" in result["reply_text"]
    assert "передав" not in result["reply_text"].lower()
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "хочу поговорити з живою людиною",
        "можна поговорити з людиною?",
        "з'єднайте з адміністратором",
        "мені потрібен оператор",
        "хочу поговорити не з ботом",
    ],
)
async def test_dental_human_handoff_natural_phrase_variants(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "human_handoff_request"
    assert result["routing_category"] == "safe_handoff"
    assert "AI-асистент" in result["reply_text"]
    assert "адміністратор" in result["reply_text"]
    assert result["booking_result"] is None
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "покличте адміністратора",
        "хочу поговорити з адміністратором",
        "можна живого оператора?",
        "передайте мене адміністратору",
    ],
)
async def test_dental_human_handoff_existing_phrase_variants_still_work(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "human_handoff_request"
    assert result["routing_category"] == "safe_handoff"
    assert "AI-асистент" in result["reply_text"]
    assert "адміністратор" in result["reply_text"]
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_human_handoff_during_active_booking_preserves_pending_state():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    before_pending = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    checks_before = len(calendar.checked)

    result = await processor.process(_message("можна поговорити з людиною?"))
    after_pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "human_handoff_request"
    assert result["routing_category"] == "safe_handoff"
    assert after_pending == before_pending
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.WAITING_FOR_TIME
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "ви бот?",
        "це бот?",
        "адміністратор працює сьогодні?",
        "людина",
        "оператор",
        "адміністратор",
        "привіт",
        "хочу записатися на чистку",
    ],
)
async def test_dental_human_handoff_false_positive_controls(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] != "human_handoff_request"
    if text == "хочу записатися на чистку":
        assert result["booking_result"] is not None
        assert processor.booking_service.get_booking_state("patient-1") == BookingState.WAITING_FOR_TIME
    else:
        assert calendar.created == []
    assert calendar.checked == []


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
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_exact_time_booking_rolls_saturday_9_to_next_week_and_rejects(dental_processor):
    processor, calendar = dental_processor

    result = await processor.process(_message("Хочу записатися на чистку у суботу о 9"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "outside_business_hours"
    assert result["booking_result"]["start_dt"] == "2026-08-29T09:00:00+03:00"
    assert calendar.checked == []
    assert "клініка не працює" in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
async def test_dental_exact_time_booking_rolls_saturday_10_to_next_week_and_checks_calendar(dental_processor):
    processor, calendar = dental_processor

    result = await processor.process(_message("Хочу записатися на чистку у суботу о 10"))

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


def _assert_waiting_for_time_context_preserved(processor):
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"
    assert pending["requested_day_label"] == "четвер"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["current_service_name"]
    assert "start_dt" not in pending
    assert "suggested_slots" not in pending
    assert processor.booking_service._get_completed_booking("patient-1") is None
    return pending


@pytest.mark.asyncio
async def test_dental_invalid_exact_time_preserves_active_booking_context():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("хочу на чистку у четвер"))
    invalid = await processor.process(_message("о 22"))
    pending = _assert_waiting_for_time_context_preserved(processor)

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert invalid["booking_result"]["status"] == "outside_business_hours"
    assert invalid["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert invalid["booking_result"]["requested_date"] == "2026-08-27"
    assert invalid["booking_result"]["start_dt"] == "2026-08-27T22:00:00+03:00"
    assert calendar.checked == []
    assert calendar.created == []
    assert pending["context_summary"] == "хочу на чистку у четвер"


@pytest.mark.asyncio
async def test_dental_invalid_before_opening_preserves_active_booking_context():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    invalid = await processor.process(_message("о 8"))

    assert invalid["booking_result"]["status"] == "outside_business_hours"
    _assert_waiting_for_time_context_preserved(processor)
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_exact_time_crossing_closing_preserves_active_booking_context():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    invalid = await processor.process(_message("о 18:45"))

    assert invalid["booking_result"]["status"] == "outside_business_hours"
    assert invalid["booking_result"]["start_dt"] == "2026-08-27T18:45:00+03:00"
    _assert_waiting_for_time_context_preserved(processor)
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_closed_day_service_request_offers_next_working_day_context():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 24, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("хочу на чистку у неділю"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "У неділю клініка не працює" in result["reply_text"]
    assert "Найближчий робочий день" in result["reply_text"]
    assert "16:00" in result["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-24"
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.checked
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_valid_exact_time_after_invalid_time_uses_preserved_context():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    await processor.process(_message("о 22"))
    valid = await processor.process(_message("о 16"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert valid["booking_result"]["status"] == "waiting_for_contact"
    assert valid["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 27, 16), "duration_minutes": 30}
    ]
    assert calendar.created == []
    assert pending["state"] == "WAITING_FOR_CONTACT"
    assert pending["current_service_id"] == "dental_cleaning"


@pytest.mark.asyncio
async def test_dental_natural_time_retries_after_invalid_time_keep_context():
    supported_phrases = [
        "а 16?",
        "а о 16?",
        "тоді 16",
        "тоді о 16",
        "краще 16",
        "краще о 16",
        "давайте 16",
        "давай 16",
        "можна 16?",
        "можна о 16?",
        "16",
        "о 16",
        "на 16",
        "16:00",
    ]

    for phrase in supported_phrases:
        calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 16)])
        processor, calendar = _build_dental_processor(calendar_service=calendar)

        await processor.process(_message("хочу на чистку у четвер"))
        await processor.process(_message("о 22"))
        result = await processor.process(_message(phrase))

        assert result["booking_result"]["status"] == "waiting_for_contact"
        assert result["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"
        assert calendar.checked == [
            {"start_dt": _kyiv_dt(2026, 8, 27, 16), "duration_minutes": 30}
        ]


@pytest.mark.asyncio
async def test_dental_busy_retry_after_invalid_time_uses_existing_busy_slot_behavior():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 17),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    await processor.process(_message("о 22"))
    busy = await processor.process(_message("о 16"))

    assert busy["booking_result"]["status"] == "slot_suggested"
    assert busy["booking_result"]["start_dt"] == "2026-08-27T17:00:00+03:00"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 27, 16), "duration_minutes": 30},
        {"start_dt": _kyiv_dt(2026, 8, 27, 17), "duration_minutes": 30},
    ]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_calendar_exception_retry_after_invalid_time_fails_closed():
    calendar = FailingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    await processor.process(_message("о 22"))
    result = await processor.process(_message("о 16"))

    assert result["booking_result"]["status"] == "availability_check_failed"
    assert result["booking_result"]["event_created"] is False
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 27, 16), "duration_minutes": 30}
    ]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_invalid_time_then_contact_completion_creates_only_valid_event():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    invalid = await processor.process(_message("о 22"))
    valid = await processor.process(_message("о 16"))
    name = await processor.process(_message("Дмитро"))
    phone = await processor.process(_message("0987121328"))

    assert invalid["booking_result"]["status"] == "outside_business_hours"
    assert valid["booking_result"]["status"] == "waiting_for_contact"
    assert name["booking_result"]["status"] == "waiting_for_contact"
    assert name["booking_result"]["booking_state"] == "WAITING_FOR_CONTACT"
    assert phone["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 16)
    assert calendar.created[0]["duration_minutes"] == 30
    assert calendar.created[0]["summary"] == "Візит booking"
    assert calendar.created[0]["attendee_emails"] == []
    assert "22:00" not in calendar.created[0]["description"]


@pytest.mark.asyncio
async def test_dental_availability_question_after_invalid_time_stays_scoped_to_pending_date():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 10),
        _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    await processor.process(_message("о 22"))
    availability = await processor.process(_message("коли є вільно?"))

    assert availability["booking_result"]["status"] == "time_window_slots_suggested"
    assert availability["booking_result"]["requested_date"] == "2026-08-27"
    assert {slot["start_dt"] for slot in availability["booking_result"]["suggested_slots"]} == {
        "2026-08-27T10:00:00+03:00",
        "2026-08-27T16:00:00+03:00",
    }
    assert all(check["start_dt"].date().isoformat() == "2026-08-27" for check in calendar.checked)
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_daypart_multiturn_uses_pending_date_and_rejects_closed_tomorrow():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 23, 10)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("запис на чистку завтра"))
    morning = await processor.process(_message("зранку"))

    assert start["booking_result"]["status"] == "outside_business_hours"
    assert start["booking_result"]["requested_date"] == "2026-08-23"
    assert morning["booking_result"]["status"] == "outside_business_hours"
    assert "клініка не працює" in morning["reply_text"].lower()
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_daypart_one_turn_tomorrow_morning_rejects_closed_day():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 23, 10)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися на чистку завтра зранку"))

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

    result = await processor.process(_message("Хочу записатися на чистку у понеділок зранку"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    selected = await processor.process(_message("10"))
    pending_after_select = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "daypart_slots_suggested"
    assert "У понеділок зранку" in result["reply_text"]
    assert "можу запропонувати" in result["reply_text"]
    assert "Який час вам зручніший" in result["reply_text"]
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

    result = await processor.process(_message("Хочу записатися на чистку у вівторок після 15"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "У вівторок після 15:00" in result["reply_text"]
    assert "можу запропонувати" in result["reply_text"]
    assert "Який час вам зручніший" in result["reply_text"]
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

    result = await processor.process(_message("Хочу записатися на чистку у вівторок до 14"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "У вівторок до 14:00" in result["reply_text"]
    assert "можу запропонувати" in result["reply_text"]
    assert "Який час вам зручніший" in result["reply_text"]
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

    result = await processor.process(_message("Хочу записатися на чистку у вівторок з 13 до 16"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "У вівторок з 13:00 до 16:00" in result["reply_text"]
    assert "можу запропонувати" in result["reply_text"]
    assert "Який час вам зручніший" in result["reply_text"]
    assert {slot["start_dt"] for slot in result["booking_result"]["suggested_slots"]} == {
        "2026-08-25T13:00:00+03:00",
        "2026-08-25T15:30:00+03:00",
    }


@pytest.mark.asyncio
async def test_dental_time_window_multiturn_preserves_pending_date():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 25, 15, 30)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("запис на чистку вівторок"))
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

    initial = await processor.process(_message("Хочу записатися на чистку у вівторок після 15"))
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

    initial = await processor.process(_message("Хочу записатися на чистку у четвер до 15"))
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

    await processor.process(_message("Хочу записатися на чистку у четвер до 15"))
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
    assert "4500" not in refined["reply_text"]
    assert "відбілювання" not in refined["reply_text"].lower()
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "на котрі години є вільні слоти?",
        "які години вільні?",
        "коли є вільно?",
        "що є вільне?",
        "я підлаштуюсь, що є?",
        "коли можна у четвер?",
        "що є на четвер?",
        "які є вільні години у четвер?",
    ],
)
async def test_dental_active_booking_availability_questions_use_known_date(text):
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 10),
        _kyiv_dt(2026, 8, 27, 11),
        _kyiv_dt(2026, 8, 27, 15),
        _kyiv_dt(2026, 8, 28, 9),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("Хочу на чистку у четвер"))
    initial_checks = len(calendar.checked)
    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert start["booking_result"]["requested_date"] == "2026-08-27"
    assert result["intent"] == "booking_availability_question"
    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["requested_date"] == "2026-08-27"
    assert result["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T10:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T11:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T15:00:00+03:00"},
    ]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"
    assert "Графік роботи" not in result["reply_text"]
    assert len(calendar.checked) > initial_checks
    assert {
        item["start_dt"].date().isoformat()
        for item in calendar.checked[initial_checks:]
    } == {"2026-08-27"}


@pytest.mark.asyncio
async def test_dental_active_booking_availability_question_can_override_known_date():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 10),
        _kyiv_dt(2026, 8, 28, 9),
        _kyiv_dt(2026, 8, 28, 10),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер"))
    initial_checks = len(calendar.checked)
    result = await processor.process(_message("а що є у п'ятницю?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_availability_question"
    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["requested_date"] == "2026-08-28"
    assert result["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-28T09:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-28T10:00:00+03:00"},
    ]
    assert pending["requested_date"] == "2026-08-28"
    assert {
        item["start_dt"].date().isoformat()
        for item in calendar.checked[initial_checks:]
    } == {"2026-08-28"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, expected_fragment",
    [
        ("який у вас графік роботи?", "Графік роботи"),
        ("до котрої ви працюєте?", "Графік роботи"),
        ("в неділю ви працюєте?", "Графік роботи"),
        ("скільки коштує відбілювання?", "4500 грн"),
        ("скільки коштує чистка?", "1800 грн"),
    ],
)
async def test_dental_active_booking_availability_priority_keeps_faq_boundaries(text, expected_fragment):
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 10),
        _kyiv_dt(2026, 8, 27, 11),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер"))
    initial_checks = len(calendar.checked)
    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] == "booking_grounded_question"
    assert result["booking_result"] is None
    assert expected_fragment in result["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"
    assert len(calendar.checked) == initial_checks


@pytest.mark.asyncio
async def test_dental_active_booking_daypart_and_time_window_still_use_existing_verified_logic():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 10),
        _kyiv_dt(2026, 8, 27, 11),
        _kyiv_dt(2026, 8, 27, 16, 30),
        _kyiv_dt(2026, 8, 27, 17),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер"))
    morning = await processor.process(_message("чи є щось зранку?"))
    later = await processor.process(_message("а після 16?"))

    assert morning["booking_result"]["status"] == "daypart_slots_suggested"
    assert morning["booking_result"]["requested_date"] == "2026-08-27"
    assert morning["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T10:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T11:00:00+03:00"},
    ]
    assert later["booking_result"]["status"] == "time_window_slots_suggested"
    assert later["booking_result"]["requested_date"] == "2026-08-27"
    assert later["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T16:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T17:00:00+03:00"},
    ]


@pytest.mark.asyncio
async def test_dental_active_booking_time_window_keeps_legitimate_price_faq():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 10),
        _kyiv_dt(2026, 8, 27, 10, 30),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на чистку у четвер до 15"))
    faq = await processor.process(_message("скільки коштує відбілювання?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert faq["intent"] == "booking_grounded_question"
    assert faq["booking_result"] is None
    assert "4500" in faq["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"


@pytest.mark.asyncio
async def test_dental_time_window_no_verified_slots_are_not_invented():
    calendar = SelectiveConfiguredCalendarService([])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися на чистку у вівторок після 15"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["suggested_slots"] == []
    assert "не бачу вільних слотів" in result["reply_text"].lower()


@pytest.mark.asyncio
async def test_dental_time_window_keeps_exact_time_booking_unchanged():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 25, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися на чистку у вівторок о 14"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-08-25T14:00:00+03:00"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 25, 14), "duration_minutes": 30}
    ]


@pytest.mark.asyncio
async def test_dental_absolute_date_one_turn_service_date_time_reaches_availability():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 9, 3, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу записатися на чистку 3 вересня о 16"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-09-03T16:00:00+03:00"
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 9, 3, 16), "duration_minutes": 30}
    ]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_absolute_date_preserved_until_time_followup():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 9, 3, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    date_only = await processor.process(_message("Хочу записатися на чистку 3 вересня"))
    time = await processor.process(_message("о 16"))

    assert date_only["booking_result"]["status"] == "waiting_for_time"
    assert date_only["booking_result"]["requested_date"] == "2026-09-03"
    assert time["booking_result"]["status"] == "waiting_for_contact"
    assert time["booking_result"]["start_dt"] == "2026-09-03T16:00:00+03:00"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 9, 3, 16), "duration_minutes": 30}
    ]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_absolute_date_composes_service_date_time_contact_multiturn():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 9, 3, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("Хочу записатися"))
    service = await processor.process(_message("на чистку"))
    date_only = await processor.process(_message("3 вересня"))
    time = await processor.process(_message("о 16"))
    confirmed = await processor.process(_message("Олена 0981112233"))

    assert start["booking_result"]["status"] == "waiting_for_service"
    assert service["booking_result"]["status"] == "waiting_for_time"
    assert date_only["booking_result"]["status"] == "waiting_for_time"
    assert date_only["booking_result"]["requested_date"] == "2026-09-03"
    assert time["booking_result"]["status"] == "waiting_for_contact"
    assert confirmed["booking_result"]["status"] == "confirmed"
    assert confirmed["booking_result"]["customer_name"] == "Олена"
    assert confirmed["booking_result"]["contact_phone"] == "0981112233"
    assert calendar.created[-1]["start_dt"] == _kyiv_dt(2026, 9, 3, 16)


@pytest.mark.asyncio
async def test_dental_absolute_date_busy_alternative_requires_explicit_acceptance():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 9, 3, 17)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    busy = await processor.process(_message("Хочу записатися на чистку 3 вересня о 16"))
    assert busy["booking_result"]["status"] == "slot_suggested"
    assert busy["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert busy["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-09-03T17:00:00+03:00"}
    ]
    assert calendar.created == []

    contact_only = await processor.process(_message("Олена 0981112233"))
    assert contact_only["booking_result"]["status"] == "waiting_for_time"
    assert calendar.created == []

    accepted = await processor.process(_message("так, о 17 підходить"))
    assert accepted["booking_result"]["status"] == "confirmed"
    assert calendar.created[-1]["start_dt"] == _kyiv_dt(2026, 9, 3, 17)
    assert len(calendar.created) == 1


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
    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert calendar.created == []
    assert "ім" not in result["reply_text"].lower()
    assert "номер телефону" not in result["reply_text"].lower()
    assert "вівторок" in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "15:30",
        "15: 30",
        "15 30",
        "о 15:30",
        "о 15: 30",
        "о 15 30",
        "на 15:30",
        "на 15 30",
        "давайте 15:30",
        "давайте 15: 30",
        "давайте 15 30",
        "давай 15 30",
        "тоді 15:30",
        "тоді 15 30",
        "15 30 підійде",
        "15: 30 норм",
    ],
)
async def test_dental_suggested_slot_selection_accepts_natural_offered_formats(text):
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15),
        _kyiv_dt(2026, 8, 27, 15, 30),
        _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    initial = await processor.process(_message("Хочу записатися на чистку у четвер після 14"))
    initial_checks = len(calendar.checked)
    parsed = processor.booking_service._extract_suggested_slot_selection_time(text)
    selected = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert initial["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T15:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T15:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T16:00:00+03:00"},
    ]
    assert parsed == time(15, 30)
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-27T15:30:00+03:00"
    assert pending["state"] == "WAITING_FOR_CONTACT"
    assert pending["start_dt"] == "2026-08-27T15:30:00+03:00"
    assert pending["customer_name"] is None
    assert pending.get("contact_phone") is None
    assert len(calendar.checked) == initial_checks + 1
    assert calendar.checked[-1] == {
        "start_dt": _kyiv_dt(2026, 8, 27, 15, 30),
        "duration_minutes": 30,
    }


@pytest.mark.asyncio
async def test_dental_suggested_slot_selection_accepts_ordinal_option_phrase():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15),
        _kyiv_dt(2026, 8, 27, 15, 30),
        _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    initial = await processor.process(_message("Хочу записатися на чистку у четвер після 14"))
    initial_checks = len(calendar.checked)
    selected = await processor.process(_message("другий варіант підійде"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert initial["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T15:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T15:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T16:00:00+03:00"},
    ]
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-27T15:30:00+03:00"
    assert pending["state"] == "WAITING_FOR_CONTACT"
    assert pending["start_dt"] == "2026-08-27T15:30:00+03:00"
    assert pending["customer_name"] is None
    assert len(calendar.checked) == initial_checks + 1
    assert calendar.checked[-1] == {
        "start_dt": _kyiv_dt(2026, 8, 27, 15, 30),
        "duration_minutes": 30,
    }
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, expected_parsed",
    [
        ("14:30", time(14, 30)),
        ("14 30", time(14, 30)),
        ("давайте 14 30", time(14, 30)),
        ("17", time(17)),
        ("17:00", time(17)),
        ("17 00", time(17)),
    ],
)
async def test_dental_suggested_slot_selection_rejects_unoffered_formats(text, expected_parsed):
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15),
        _kyiv_dt(2026, 8, 27, 15, 30),
        _kyiv_dt(2026, 8, 27, 16),
        _kyiv_dt(2026, 8, 27, 17),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    initial = await processor.process(_message("Хочу записатися на чистку у четвер після 14"))
    initial_checks = len(calendar.checked)
    parsed = processor.booking_service._extract_suggested_slot_selection_time(text)
    rejected = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert initial["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T15:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T15:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T16:00:00+03:00"},
    ]
    assert parsed == expected_parsed
    assert len(calendar.checked) > initial_checks
    assert calendar.created == []
    if expected_parsed == time(17):
        assert rejected["booking_result"]["status"] == "waiting_for_contact"
        assert rejected["booking_result"]["start_dt"] == "2026-08-27T17:00:00+03:00"
    else:
        assert rejected["booking_result"]["status"] in {"slot_suggested", "unavailable"}
        assert pending is None or pending["state"] == "WAITING_FOR_TIME"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["15", "давайте 15", "15 буде супер"])
async def test_dental_suggested_slot_selection_keeps_hour_only_offered_selection(text):
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15),
        _kyiv_dt(2026, 8, 27, 15, 30),
        _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на чистку у четвер після 14"))
    parsed = processor.booking_service._extract_suggested_slot_selection_time(text)
    selected = await processor.process(_message(text))

    assert parsed == time(15)
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-27T15:00:00+03:00"


@pytest.mark.asyncio
async def test_dental_suggested_slot_selection_accepts_super_suffix_for_offered_hour():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 17),
        _kyiv_dt(2026, 8, 25, 17, 30),
        _kyiv_dt(2026, 8, 25, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на консультацію ортодонта"))
    await processor.process(_message("Давайте на вівторок"))
    await processor.process(_message("А щось пізніше, ввечері?"))
    selected = await processor.process(_message("18 буде супер"))

    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-25T18:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_fresh_exact_time_booking_does_not_parse_spaced_time_globally():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 25, 15, 30)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    parsed = processor.booking_service._parse_requested_datetime("Хочу на чистку у вівторок 15 30")
    result = await processor.process(_message("Хочу на чистку у вівторок 15 30"))

    assert parsed == _kyiv_dt(2026, 8, 25, 15, 30)
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-08-25T15:30:00+03:00"
    assert calendar.checked[0] == {"start_dt": _kyiv_dt(2026, 8, 25, 15, 30), "duration_minutes": 30}


@pytest.mark.parametrize(
    "text",
    [
        "Хочу на чистку у четвер, мені 14 років",
        "Хочу на чистку у четвер, дитині 12 років",
        "Хочу на чистку у четвер, нас буде 16",
        "Хочу на чистку у четвер, буду через 15 хвилин",
        "Хочу на чистку у четвер, через 20 хв",
        "Хочу на чистку у четвер, номер 14",
        "Хочу на чистку у четвер, кабінет 12",
    ],
)
def test_dental_requested_datetime_does_not_treat_unrelated_numbers_as_time(text):
    processor, _calendar = _build_dental_processor()

    assert processor.booking_service._parse_requested_datetime(text) is None


@pytest.mark.asyncio
async def test_dental_age_after_weekday_never_creates_unrequested_calendar_time():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    first = await processor.process(_message("Хочу на чистку у четвер, мені 14 років"))
    contact = await processor.process(_message("Дмитро 0987121328"))

    assert first["booking_result"]["status"] == "waiting_for_time"
    assert contact["booking_result"]["status"] == "waiting_for_time"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Дмитро 0987121328 запишіть мене на чистку в четвер о 15",
        "Дмитро, 0987121328, хочу на чистку в четвер о 15",
        "Хочу на чистку в четвер о 15, Дмитро, 0987121328",
    ],
)
async def test_dental_one_turn_booking_extracts_contact_without_booking_prose_name(text):
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message(text))

    assert result["booking_result"]["status"] == "confirmed"
    assert result["booking_result"]["customer_name"] == "Дмитро"
    assert result["booking_result"]["contact_phone"] == "0987121328"
    assert len(calendar.created) == 1
    assert "Customer name: Дмитро" in calendar.created[0]["description"]
    assert "Customer name: Дмитро хочу" not in calendar.created[0]["description"]
    assert "Customer name: Дмитро запишіть" not in calendar.created[0]["description"]


@pytest.mark.asyncio
async def test_dental_one_turn_booking_with_phone_only_does_not_invent_name_from_prose():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("0987121328 хочу на чистку в четвер о 15"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert pending["customer_name"] is None
    assert pending["contact_phone"] == "0987121328"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_semantic_retry_with_different_mid_does_not_create_duplicate_event():
    calendar = UniqueEventConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 28, 15), _kyiv_dt(2026, 8, 28, 16)],
        created_slots_become_busy=True,
    )
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    first = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )
    completed_before_retry = dict(
        processor.booking_service._get_completed_booking("patient-1")
    )
    retry = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-2")
    )
    completed_after_retry = processor.booking_service._get_completed_booking("patient-1")

    assert first["booking_result"]["status"] == "confirmed"
    assert first["booking_result"]["event_id"] == "dental-event-1"
    assert retry["booking_result"]["status"] == "already_confirmed"
    assert retry["booking_result"]["event_created"] is False
    assert retry["booking_result"]["idempotent"] is True
    assert retry["booking_result"]["event_id"] == "dental-event-1"
    assert len(calendar.created) == 1
    assert completed_after_retry == completed_before_retry
    assert completed_after_retry["calendar_event_id"] == "dental-event-1"
    assert completed_after_retry["current_service_id"] == "dental_cleaning"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 28, 15), "duration_minutes": 30}
    ]
    assert "у вас уже є підтверджений" in retry["reply_text"].lower()
    assert "підтвердили" not in retry["reply_text"].lower()


@pytest.mark.asyncio
async def test_dental_same_mid_retry_still_uses_provider_dedup():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 28, 15)])
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    first = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )
    retry = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )

    assert first["booking_result"]["status"] == "confirmed"
    assert retry["intent"] == "duplicate_skipped"
    assert len(calendar.created) == 1


@pytest.mark.asyncio
async def test_dental_same_sender_different_time_or_date_creates_distinct_events():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 28, 15),
        _kyiv_dt(2026, 8, 28, 16),
        _kyiv_dt(2026, 8, 24, 15),
    ])
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    first = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )
    different_time = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 16", "mid-2")
    )
    different_date = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пн о 15", "mid-3")
    )

    assert first["booking_result"]["event_id"] == "dental-event-1"
    assert different_time["booking_result"]["event_id"] == "dental-event-2"
    assert different_date["booking_result"]["event_id"] == "dental-event-3"
    assert [event["start_dt"] for event in calendar.created] == [
        _kyiv_dt(2026, 8, 28, 15),
        _kyiv_dt(2026, 8, 28, 16),
        _kyiv_dt(2026, 8, 24, 15),
    ]


@pytest.mark.asyncio
async def test_dental_different_sender_same_booking_creates_independent_event():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 28, 15)])
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    first = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1", sender_id="patient-1")
    )
    second = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-2", sender_id="patient-2")
    )

    assert first["booking_result"]["event_id"] == "dental-event-1"
    assert second["booking_result"]["event_id"] == "dental-event-2"
    assert len(calendar.created) == 2


@pytest.mark.asyncio
async def test_dental_same_contact_different_service_at_new_time_is_allowed():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 28, 15),
        _kyiv_dt(2026, 8, 28, 16),
    ])
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    consultation = await processor.process(
        _message_with_mid("Дмитро 0987121328 на консультацію в пт о 15", "mid-1")
    )
    cleaning = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 16", "mid-2")
    )

    assert consultation["booking_result"]["status"] == "confirmed"
    assert cleaning["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 2
    assert processor.booking_service._get_completed_booking("patient-1")["calendar_event_id"] == "dental-event-2"


@pytest.mark.asyncio
async def test_dental_same_contact_different_service_same_time_checks_availability_and_stays_busy():
    calendar = UniqueEventConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 28, 15)],
        created_slots_become_busy=True,
    )
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    cleaning = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )
    filling = await processor.process(
        _message_with_mid("Дмитро 0987121328 хочу поставити пломбу в пт о 15", "mid-2")
    )

    assert cleaning["booking_result"]["status"] == "confirmed"
    assert cleaning["booking_result"]["event_id"] == "dental-event-1"
    assert filling["booking_result"]["status"] == "unavailable"
    assert filling["booking_result"]["event_created"] is False
    assert len(calendar.created) == 1
    assert processor.booking_service._get_completed_booking("patient-1")["current_service_id"] == "dental_cleaning"


@pytest.mark.asyncio
async def test_dental_same_contact_whitening_same_time_after_cleaning_stays_busy():
    calendar = UniqueEventConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 28, 15)],
        created_slots_become_busy=True,
    )
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    cleaning = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )
    whitening = await processor.process(
        _message_with_mid("Дмитро 0987121328 запишіть на відбілювання в пт о 15", "mid-2")
    )

    assert cleaning["booking_result"]["event_id"] == "dental-event-1"
    assert whitening["booking_result"]["status"] == "unavailable"
    assert whitening["booking_result"]["event_created"] is False
    assert len(calendar.created) == 1
    assert processor.booking_service._get_completed_booking("patient-1")["current_service_id"] == "dental_cleaning"


@pytest.mark.asyncio
async def test_dental_explicit_service_does_not_dedupe_against_no_service_completed_booking():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 28, 15)])
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )
    processor.booking_service._mark_booking_completed(
        "patient-1",
        start_dt=_kyiv_dt(2026, 8, 28, 15),
        customer_name="Дмитро",
        email=None,
        phone="0987121328",
        calendar_event_id="legacy-event",
    )

    result = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )

    assert result["booking_result"]["status"] == "confirmed"
    assert result["booking_result"]["event_created"] is True
    assert result["booking_result"]["event_id"] == "dental-event-1"
    assert len(calendar.created) == 1
    assert processor.booking_service._get_completed_booking("patient-1")["current_service_id"] == "dental_cleaning"


@pytest.mark.asyncio
async def test_dental_completed_service_still_dedupes_when_retry_loses_service_context():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 28, 15)])
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )
    processor.booking_service._mark_booking_completed(
        "patient-1",
        start_dt=_kyiv_dt(2026, 8, 28, 15),
        customer_name="Дмитро",
        email=None,
        phone="0987121328",
        calendar_event_id="dental-event-1",
        current_service_id="dental_cleaning",
        current_service_name="Професійна чистка зубів",
    )

    result = processor.booking_service.start_booking_flow(
        sender_id="patient-1",
        message_text="Дмитро 0987121328 в пт о 15",
        source_channel="instagram",
    )

    assert result["status"] == "already_confirmed"
    assert result["event_created"] is False
    assert result["idempotent"] is True
    assert result["event_id"] == "dental-event-1"
    assert len(calendar.created) == 0


@pytest.mark.asyncio
async def test_dental_one_turn_booking_persists_service_id():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 28, 15)])
    processor, _calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    result = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )
    completed = processor.booking_service._get_completed_booking("patient-1")

    assert result["booking_result"]["status"] == "confirmed"
    assert completed["calendar_event_id"] == "dental-event-1"
    assert completed["current_service_id"] == "dental_cleaning"
    assert completed["current_service_name"]


@pytest.mark.asyncio
async def test_dental_multi_turn_booking_persists_service_id():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 28, 15)])
    processor, _calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    await processor.process(_message_with_mid("хочу на чистку в пт о 15", "mid-1"))
    contact = await processor.process(_message_with_mid("Дмитро 0987121328", "mid-2"))
    completed = processor.booking_service._get_completed_booking("patient-1")

    assert contact["booking_result"]["status"] == "confirmed"
    assert completed["calendar_event_id"] == "dental-event-1"
    assert completed["current_service_id"] == "dental_cleaning"
    assert completed["current_service_name"]


@pytest.mark.asyncio
async def test_dental_semantic_retry_dedupes_equivalent_ukrainian_phone_formats():
    calendar = UniqueEventConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 28, 15)],
        created_slots_become_busy=True,
    )
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    first = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )
    retry = await processor.process(
        _message_with_mid("Дмитро +380987121328 на чистку в пт о 15", "mid-2")
    )

    assert first["booking_result"]["event_id"] == "dental-event-1"
    assert retry["booking_result"]["status"] == "already_confirmed"
    assert retry["booking_result"]["event_created"] is False
    assert retry["booking_result"]["idempotent"] is True
    assert retry["booking_result"]["event_id"] == "dental-event-1"
    assert len(calendar.created) == 1


@pytest.mark.asyncio
async def test_dental_semantic_retry_dedupes_spaced_ukrainian_phone_format():
    calendar = UniqueEventConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 28, 15)],
        created_slots_become_busy=True,
    )
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    first = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )
    retry = await processor.process(
        _message_with_mid("Дмитро на чистку в пт о 15 +38 098 712 13 28", "mid-2")
    )

    assert first["booking_result"]["event_id"] == "dental-event-1"
    assert retry["booking_result"]["status"] == "already_confirmed"
    assert retry["booking_result"]["event_created"] is False
    assert retry["booking_result"]["idempotent"] is True
    assert retry["booking_result"]["event_id"] == "dental-event-1"
    assert len(calendar.created) == 1


@pytest.mark.asyncio
async def test_dental_different_phone_same_service_start_checks_availability_and_stays_busy():
    calendar = UniqueEventConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 28, 15)],
        created_slots_become_busy=True,
    )
    processor, calendar = _build_dental_processor(
        calendar_service=calendar,
        dedup_service=DedupService(),
    )

    first = await processor.process(
        _message_with_mid("Дмитро 0987121328 на чистку в пт о 15", "mid-1")
    )
    second = await processor.process(
        _message_with_mid("Дмитро 0987121329 на чистку в пт о 15", "mid-2")
    )

    assert first["booking_result"]["event_id"] == "dental-event-1"
    assert second["booking_result"]["status"] == "unavailable"
    assert second["booking_result"]["event_created"] is False
    assert len(calendar.created) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "а 16?",
        "а о 16?",
        "тоді 16",
        "тоді о 16",
        "краще 16",
        "краще о 16",
        "давайте 16",
        "давай 16",
        "можна 16?",
        "можна о 16?",
        "16",
        "о 16",
        "на 16",
        "16:00",
    ],
)
async def test_dental_selected_time_natural_correction_verifies_new_time(phrase):
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14),
        _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    selected = await processor.process(_message("хочу на чистку у четвер о 14"))
    corrected = await processor.process(_message(phrase))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-27T14:00:00+03:00"
    assert corrected["booking_result"]["status"] == "waiting_for_contact"
    assert corrected["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 27, 14), "duration_minutes": 30},
        {"start_dt": _kyiv_dt(2026, 8, 27, 16), "duration_minutes": 30},
    ]
    assert calendar.created == []
    assert pending["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["customer_name"] is None


@pytest.mark.asyncio
async def test_dental_busy_suggested_time_correction_verifies_new_time():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14),
        _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    suggested = await processor.process(_message("хочу на чистку у четвер о 13"))
    corrected = await processor.process(_message("а 16?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert suggested["booking_result"]["status"] == "slot_suggested"
    assert suggested["booking_result"]["start_dt"] == "2026-08-27T14:00:00+03:00"
    assert corrected["booking_result"]["status"] == "waiting_for_contact"
    assert corrected["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 27, 13), "duration_minutes": 30},
        {"start_dt": _kyiv_dt(2026, 8, 27, 14), "duration_minutes": 30},
        {"start_dt": _kyiv_dt(2026, 8, 27, 16), "duration_minutes": 30},
    ]
    assert calendar.created == []
    assert pending["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert pending["current_service_id"] == "dental_cleaning"


@pytest.mark.asyncio
async def test_dental_time_correction_then_contact_creates_only_new_time_event():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14),
        _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    corrected = await processor.process(_message("тоді 16"))
    name = await processor.process(_message("Дмитро"))
    phone = await processor.process(_message("0987121328"))

    assert corrected["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert name["booking_result"]["status"] == "waiting_for_contact"
    assert phone["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["event_id"] == "dental-event-1"
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 16)


@pytest.mark.asyncio
async def test_dental_time_correction_to_busy_new_time_does_not_keep_old_time():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14),
        _kyiv_dt(2026, 8, 27, 17),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    corrected = await processor.process(_message("тоді 16"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert corrected["booking_result"]["status"] == "slot_suggested"
    assert corrected["booking_result"]["start_dt"] == "2026-08-27T17:00:00+03:00"
    assert calendar.created == []
    # 17:00 is an offered suggestion, not a user-selected slot: state must stay
    # WAITING_FOR_TIME with no start_dt, so contact-only input can never
    # auto-select it and trigger a Calendar create.
    assert pending["state"] == "WAITING_FOR_TIME"
    assert "start_dt" not in pending
    assert pending["requested_date"] == "2026-08-27"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T17:00:00+03:00"}
    ]

    contact = await processor.process(_message("Дмитро 0987121328"))

    assert contact["booking_result"]["status"] == "waiting_for_time"
    assert contact["booking_result"]["event_created"] is False
    assert calendar.created == []
    pending_after_contact = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending_after_contact["state"] == "WAITING_FOR_TIME"
    assert "start_dt" not in pending_after_contact

    accepted = await processor.process(_message("давайте 17"))

    assert accepted["booking_result"]["status"] == "confirmed"
    assert accepted["booking_result"]["event_created"] is True
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 17)


@pytest.mark.asyncio
async def test_dental_time_correction_calendar_exception_does_not_keep_old_time():
    calendar = FailingAt16ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    corrected = await processor.process(_message("тоді 16"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert corrected["booking_result"]["status"] == "availability_check_failed"
    assert corrected["booking_result"]["event_created"] is False
    assert corrected["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert corrected["booking_result"]["requested_date"] == "2026-08-27"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"
    assert pending["current_service_id"] == "dental_cleaning"
    assert "start_dt" not in pending
    assert calendar.checked[-1] == {
        "start_dt": _kyiv_dt(2026, 8, 27, 16),
        "duration_minutes": 30,
    }
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_time_correction_outside_hours_drops_old_time_and_preserves_context():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    corrected = await processor.process(_message("тоді 22"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert corrected["booking_result"]["status"] == "outside_business_hours"
    assert corrected["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert corrected["booking_result"]["requested_date"] == "2026-08-27"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"
    assert pending["current_service_id"] == "dental_cleaning"
    assert "start_dt" not in pending
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 27, 14), "duration_minutes": 30}
    ]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_real_name_during_contact_collection_still_works():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, _calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    name = await processor.process(_message("Дмитро"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert name["booking_result"]["status"] == "waiting_for_contact"
    assert pending["customer_name"] == "Дмитро"
    assert pending["start_dt"] == "2026-08-27T14:00:00+03:00"


@pytest.mark.asyncio
async def test_dental_multiple_alternatives_contact_does_not_select_first():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15),
        _kyiv_dt(2026, 8, 27, 15, 30),
        _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    offered = await processor.process(_message("Хочу на чистку у четвер після 14"))
    assert offered["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T15:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T15:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T16:00:00+03:00"},
    ]

    contact = await processor.process(_message("Дмитро 0987121328"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert contact["booking_result"]["status"] == "waiting_for_time"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert "start_dt" not in pending
    assert calendar.created == []

    accepted = await processor.process(_message("давайте 15:30"))

    assert accepted["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 15, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["о 14 30", "на 14 30", "14 30", "о 14: 30", "14: 30", "о 14:30", "на 14:30"],
)
async def test_dental_waiting_for_time_space_separated_minutes_never_truncate(phrase):
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14, 30)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    corrected = await processor.process(_message(phrase))
    contact = await processor.process(_message("Дмитро 0987121328"))

    assert corrected["booking_result"]["start_dt"] == "2026-08-27T14:30:00+03:00"
    assert contact["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 14, 30)
    assert calendar.checked[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 14, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["14", "о 14", "на 14"])
async def test_dental_waiting_for_time_hour_only_still_means_hh00(phrase):
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message(phrase))

    assert result["booking_result"]["start_dt"] == "2026-08-27T14:00:00+03:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["14 60", "25 30", "о 25 30", "14 99", "о 14 3", "14 300"],
)
async def test_dental_waiting_for_time_malformed_minutes_rejected_safely(phrase):
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14, 30)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message(phrase))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert "start_dt" not in pending
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_space_separated_minutes_busy_never_falls_back_to_hh00():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 17, 30),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message("о 14 30"))

    assert result["booking_result"]["status"] == "slot_suggested"
    assert result["booking_result"]["start_dt"] == "2026-08-27T17:30:00+03:00"
    assert calendar.checked[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 14, 30)
    assert all(c["start_dt"].minute != 0 for c in calendar.checked if c["start_dt"].hour == 14)
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_space_separated_minutes_calendar_exception_no_create():
    calendar = FailingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message("о 14 30"))

    assert result["booking_result"]["status"] == "availability_check_failed"
    assert result["booking_result"]["event_created"] is False
    assert calendar.checked == [{"start_dt": _kyiv_dt(2026, 8, 27, 14, 30), "duration_minutes": 30}]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_waiting_for_contact_correction_space_separated_minutes():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 16, 30),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    corrected = await processor.process(_message("тоді 16 30"))
    contact = await processor.process(_message("Дмитро 0987121328"))

    assert corrected["booking_result"]["start_dt"] == "2026-08-27T16:30:00+03:00"
    assert contact["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 16, 30)


@pytest.mark.parametrize(
    "phrase",
    ["мені 14 років", "нас буде 14 30 людей", "через 14 30 хвилин"],
)
def test_dental_parse_time_only_never_reads_unrelated_number_phrases(phrase):
    processor, _calendar = _build_dental_processor()

    assert processor.booking_service._parse_time_only(phrase) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["нас буде 14 30 людей", "через 14 30 хвилин"],
)
async def test_dental_waiting_for_time_unrelated_number_phrases_are_not_times(phrase):
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14, 30)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message(phrase))

    assert calendar.checked == []
    assert calendar.created == []
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    if pending is not None:
        assert "start_dt" not in pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["15 30", "о 15 30", "15: 30", "давайте 15 30"],
)
async def test_dental_suggested_slot_parser_unchanged_by_time_only_fix(phrase):
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 15, 30), _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на чистку у четвер після 14"))
    result = await processor.process(_message(phrase))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-08-27T15:30:00+03:00"


@pytest.mark.parametrize(
    "phrase",
    [
        "мені 14 років",
        "мені 16",
        "мені потрібно подумати",
        "гривні 14",
        "останні 14 днів",
        "у нас 14 днів",
        "вихідні 14",
        "напишіть мені",
        "передзвоніть мені",
    ],
)
def test_dental_unrelated_ni_substring_phrases_are_not_corrections(phrase):
    processor, _calendar = _build_dental_processor()

    assert processor.booking_service._looks_like_booking_correction(phrase) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "мені 14 років",
        "мені 16",
        "гривні 14",
        "у нас 14 днів",
    ],
)
async def test_dental_waiting_for_time_ni_substring_does_not_fabricate_time(phrase):
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message(phrase))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert calendar.checked == []
    assert calendar.created == []
    assert pending["state"] == "WAITING_FOR_TIME"
    assert "start_dt" not in pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "мені 14 років",
        "мені 16",
        "гривні 14",
        "у нас 14 днів",
    ],
)
async def test_dental_waiting_for_contact_ni_substring_preserves_selected_time(phrase):
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    checked_before = list(calendar.checked)
    await processor.process(_message(phrase))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert pending["start_dt"] == "2026-08-27T15:00:00+03:00"
    assert not any(c["start_dt"].hour == 14 for c in calendar.checked)
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_ni_substring_end_to_end_never_creates_wrong_time_event():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    await processor.process(_message("мені 14 років"))
    result = await processor.process(_message("Дмитро 0987121328"))

    assert result["booking_result"]["status"] != "confirmed"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_ni_substring_from_selected_time_end_to_end_creates_only_selected_time():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 14),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    await processor.process(_message("мені 14 років"))
    result = await processor.process(_message("Дмитро 0987121328"))

    assert result["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 15)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["ні 16", "ні, краще 16", "тоді 16", "а 16?", "давайте 16", "краще 16"],
)
async def test_dental_legitimate_corrections_still_work_after_ni_fix(phrase):
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    result = await processor.process(_message(phrase))

    assert result["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"


@pytest.mark.asyncio
async def test_dental_negative_time_replacement_still_works_after_ni_fix():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    result = await processor.process(_message("не 14, а 16"))

    assert result["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"


@pytest.mark.parametrize(
    "phrase",
    [
        "не 14",
        "не о 14",
        "не на 14",
        "точно не о 14",
        "тільки не о 14",
        "я точно не можу о 14",
    ],
)
def test_dental_pure_negation_never_extracts_a_time(phrase):
    processor, _calendar = _build_dental_processor()

    assert processor.booking_service._extract_corrected_time(phrase) is None
    rejected, replacement, _wants_alternative = processor.booking_service._extract_negated_time_context(phrase)
    assert rejected is not None
    assert replacement is None


@pytest.mark.parametrize(
    "phrase",
    [
        "о 14 не можу",
        "14 не можу",
        "о 14 я не можу",
        "о 14 зайнятий",
        "о 14 я зайнятий",
        "14 зайнятий",
        "о 14 не вийде",
        "о 14 не підходить",
        "14 мені не підходить",
        "о 14 незручно",
        "о 14 не зручно",
        "на 14 не встигаю",
        "о 14 зайнята",
        "о 14 ми зайняті",
        "о 14 не буду",
        "о 14 мене не буде",
        "о 14 не прийду",
        "на 14 не прийду",
        "о 14 не дуже",
        "14 не дуже зручно",
    ],
)
def test_dental_post_time_rejection_never_extracts_a_time(phrase):
    processor, _calendar = _build_dental_processor()

    rejected, replacement, _wants_alternative = processor.booking_service._extract_negated_time_context(phrase)
    assert rejected is not None
    assert rejected.hour == 14
    assert replacement is None


@pytest.mark.parametrize(
    "phrase",
    [
        "о 14 не можу, а о 16 можу",
        "14 не підходить, тоді 16",
        "о 14 зайнятий, давайте 16",
        "на 14 не встигаю, краще на 16",
        "о 14 зайнята, краще 16",
        "на 14 не встигаю, давайте на 16",
        "о 14 не зручно, можна о 16",
        "14 не вийде, 16 підійде",
        "14 не можу, давайте краще 16",
        "не на 14, а на 16",
    ],
)
def test_dental_rejection_with_explicit_replacement_selects_replacement(phrase):
    processor, _calendar = _build_dental_processor()

    rejected, replacement, wants_alternative = processor.booking_service._extract_negated_time_context(phrase)
    assert rejected is not None
    assert rejected.hour == 14
    assert replacement == time(16, 0)
    assert wants_alternative is False


@pytest.mark.parametrize(
    "phrase",
    [
        "о 14 не можу, є інший час?",
        "о 14 не виходить, є інший час?",
        "о 14 не підходить, є щось інше?",
        "о 14 зайнятий, є щось пізніше?",
        "14 не підходить, можна пізніше?",
        "о 14 не можу, що є ще?",
        "о 14 не можу, які ще є варіанти?",
        "на 14 не встигаю, є щось пізніше?",
    ],
)
def test_dental_rejection_with_alternative_request_detected(phrase):
    processor, _calendar = _build_dental_processor()

    rejected, replacement, wants_alternative = processor.booking_service._extract_negated_time_context(phrase)
    assert rejected is not None
    assert rejected.hour == 14
    assert replacement is None
    assert wants_alternative is True


@pytest.mark.parametrize(
    "phrase",
    [
        "я сьогодні зайнятий",
        "я завтра зайнятий",
        "я зайнятий роботою",
        "мені не зручно говорити",
        "не можу зараз говорити",
        "в мене не виходить відповісти",
        "можливо",
        "подумаю",
        "я не знаю",
    ],
)
def test_dental_unrelated_rejection_words_without_time_are_safe(phrase):
    processor, _calendar = _build_dental_processor()

    rejected, replacement, wants_alternative = processor.booking_service._extract_negated_time_context(phrase)
    assert rejected is None
    assert replacement is None
    assert wants_alternative is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "о 14 не можу",
        "о 14 зайнятий",
        "о 14 не підходить",
        "на 14 не встигаю",
        "о 14 не вийде",
    ],
)
async def test_dental_waiting_for_time_post_time_rejection_never_selects_negated_hour(phrase):
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message(phrase))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert calendar.checked == []
    assert calendar.created == []
    assert pending["state"] == "WAITING_FOR_TIME"
    assert "start_dt" not in pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "о 14 не можу",
        "о 14 зайнятий",
        "о 14 не підходить",
        "на 14 не встигаю",
    ],
)
async def test_dental_waiting_for_contact_post_time_rejection_preserves_selected_time(phrase):
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 14),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    await processor.process(_message(phrase))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert pending["start_dt"] == "2026-08-27T15:00:00+03:00"
    assert not any(c["start_dt"].hour == 14 for c in calendar.checked)
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_post_time_rejection_end_to_end_never_creates_negated_time_event():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    await processor.process(_message("о 14 не можу"))
    result = await processor.process(_message("Дмитро 0987121328"))

    assert result["booking_result"]["status"] != "confirmed"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_post_time_rejection_from_selected_time_end_to_end_creates_only_selected_time():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 14),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    await processor.process(_message("о 14 не можу"))
    result = await processor.process(_message("Дмитро 0987121328"))

    assert result["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 15)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "о 14 не можу, а о 16 можу",
        "о 14 зайнятий, давайте 16",
        "14 не підходить, тоді 16",
        "на 14 не встигаю, краще на 16",
    ],
)
async def test_dental_post_time_rejection_replacement_end_to_end_creates_only_replacement(phrase):
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 16), _kyiv_dt(2026, 8, 27, 14),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    corrected = await processor.process(_message(phrase))
    result = await processor.process(_message("Дмитро 0987121328"))

    assert corrected["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert result["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 16)


@pytest.mark.asyncio
async def test_dental_suggested_slots_post_time_rejection_replacement_selects_offered():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на чистку у четвер після 13"))
    result = await processor.process(_message("о 14 не можу, а о 16 можу"))

    assert result["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"


@pytest.mark.asyncio
async def test_dental_suggested_slots_post_time_rejection_unoffered_replacement_stays_safe():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на чистку у четвер після 13"))
    result = await processor.process(_message("о 14 не можу, а о 16 можу"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "availability_time_not_offered"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert "start_dt" not in pending
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "о 14 не можу, є інший час?",
        "о 14 зайнятий, є щось пізніше?",
        "14 не підходить, можна пізніше?",
        "о 14 не можу, які ще є варіанти?",
    ],
)
async def test_dental_alternative_request_keeps_booking_active_and_offers_verified_slot(phrase):
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 17),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message(phrase))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "slot_suggested"
    assert pending is not None
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_cleaning"
    assert not any(c["start_dt"].hour == 14 for c in calendar.checked)
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_alternative_request_does_not_trigger_generic_cancellation():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 17),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message("о 14 зайнятий, є щось пізніше?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] != "cancelled"
    assert pending is not None


@pytest.mark.asyncio
async def test_dental_pending_time_rejection_does_not_trigger_confirmed_cancellation_semantics():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    result = await processor.process(_message("о 14 не прийду, можна 16"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert pending["current_service_id"] == "dental_cleaning"


@pytest.mark.parametrize(
    "phrase",
    [
        "не 14, а 16",
        "не о 14, а о 16",
        "не на 14, а на 16",
        "не 14, краще 16",
        "не 14, тоді 16",
        "не 14, давайте 16",
        "не на 14, давайте на 16",
    ],
)
def test_dental_negation_with_explicit_replacement_resolves_replacement(phrase):
    processor, _calendar = _build_dental_processor()

    assert processor.booking_service._extract_corrected_time(phrase) == time(16, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["точно не о 14", "не о 14", "не на 14"],
)
async def test_dental_waiting_for_time_pure_negation_never_selects_negated_hour(phrase):
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер"))
    result = await processor.process(_message(phrase))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert calendar.checked == []
    assert calendar.created == []
    assert pending["state"] == "WAITING_FOR_TIME"
    assert "start_dt" not in pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["точно не о 14", "не о 14", "не на 14"],
)
async def test_dental_waiting_for_contact_pure_negation_preserves_selected_time(phrase):
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 14),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    result = await processor.process(_message(phrase))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert pending["start_dt"] == "2026-08-27T15:00:00+03:00"
    assert not any(c["start_dt"].hour == 14 for c in calendar.checked)
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_pure_negation_contact_after_never_creates_negated_time_event():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 14),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    await processor.process(_message("точно не о 14"))
    result = await processor.process(_message("Дмитро 0987121328"))

    assert result["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 15)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["не 14, а 16", "не о 14, а о 16", "не на 14, а на 16"],
)
async def test_dental_negation_with_replacement_selects_replacement_not_negated_hour(phrase):
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 16), _kyiv_dt(2026, 8, 27, 14),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    result = await processor.process(_message(phrase))

    assert result["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert not any(c["start_dt"].hour == 14 for c in calendar.checked)


@pytest.mark.asyncio
async def test_dental_suggested_slots_negation_replacement_selects_offered_replacement():
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на чистку у четвер після 13"))
    result = await processor.process(_message("не о 14, а о 16"))

    assert result["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"


@pytest.mark.asyncio
async def test_dental_suggested_slots_negation_unoffered_replacement_stays_safe():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатися на чистку у четвер після 13"))
    result = await processor.process(_message("не о 14, а о 16"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "availability_time_not_offered"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert "start_dt" not in pending
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["о 16", "на 16", "тоді 16", "краще 16", "а 16?", "давайте 16"],
)
async def test_dental_ordinary_positive_corrections_unaffected_by_negation_fix(phrase):
    calendar = UniqueEventConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15), _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    result = await processor.process(_message(phrase))

    assert result["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"


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
async def test_dental_waiting_contact_reject_hour_updates_time_not_name(dental_processor):
    processor, calendar = dental_processor

    await processor.process(_message("Хочу записатись на консультацію у понеділок о 14"))
    corrected = await processor.process(_message("ні 16"))
    pending_after_correction = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert corrected["booking_result"]["status"] == "waiting_for_contact"
    assert pending_after_correction["customer_name"] is None
    assert pending_after_correction["contact_phone"] is None
    assert calendar.checked[-1]["start_dt"].hour == 16
    assert "16:00" in corrected["reply_text"]
    _assert_no_flowly_leakage(corrected["reply_text"])


@pytest.mark.asyncio
async def test_dental_waiting_contact_reject_better_hour_updates_time_not_name(dental_processor):
    processor, calendar = dental_processor

    await processor.process(_message("Хочу записатись на консультацію у понеділок о 14"))
    corrected = await processor.process(_message("ні, краще о 16"))
    pending_after_correction = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert corrected["booking_result"]["status"] == "waiting_for_contact"
    assert pending_after_correction["customer_name"] is None
    assert pending_after_correction["contact_phone"] is None
    assert calendar.checked[-1]["start_dt"].hour == 16
    assert "16:00" in corrected["reply_text"]
    _assert_no_flowly_leakage(corrected["reply_text"])


@pytest.mark.asyncio
async def test_dental_waiting_contact_time_window_control_never_becomes_name(dental_processor):
    processor, calendar = dental_processor

    await processor.process(_message("Хочу записатись на консультацію у понеділок о 14"))
    corrected = await processor.process(_message("краще після 15"))
    pending_after_correction = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert corrected["booking_result"]["status"] == "waiting_for_contact"
    assert pending_after_correction["customer_name"] is None
    assert pending_after_correction["contact_phone"] is None
    assert calendar.checked[-1]["start_dt"].hour == 15
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
@pytest.mark.parametrize("text", ["чистку краще", "ні чистку", "чистку не консультацію"])
async def test_dental_waiting_contact_service_control_phrases_never_become_names(text):
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("Хочу записатись на консультацію у понеділок о 14"))
    result = await processor.process(_message(text))
    pending_after = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert pending_after["customer_name"] is None
    assert pending_after["contact_phone"] is None
    assert pending_after["state"] == "WAITING_FOR_CONTACT"
    assert "T14:00:00" in pending_after["start_dt"]
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["ок", "так", "гуд", "ага"])
async def test_dental_waiting_contact_acknowledgements_never_become_names(text):
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("Хочу записатись на консультацію у понеділок о 14"))
    result = await processor.process(_message(text))
    pending_after = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert pending_after["customer_name"] is None
    assert pending_after["contact_phone"] is None
    assert pending_after["state"] == "WAITING_FOR_CONTACT"
    _assert_no_flowly_leakage(result["reply_text"])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["номер потім дам", "я передумав", "не треба"])
async def test_dental_waiting_contact_control_phrases_never_become_names(text):
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("Хочу записатись на консультацію у понеділок о 14"))
    result = await processor.process(_message(text))
    pending_after = processor.booking_service._get_pending_confirmation("patient-1")

    if pending_after is not None:
        assert pending_after["customer_name"] is None
        assert pending_after["contact_phone"] is None
    assert "передумав" not in result["reply_text"].lower()
    _assert_no_flowly_leakage(result["reply_text"])


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
@pytest.mark.parametrize("name", ["Дмитро", "Іван", "Анна", "Олександр", "Марія", "Діма"])
async def test_dental_waiting_contact_common_single_names_still_work(name):
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("Хочу записатись на консультацію у понеділок о 14"))
    result = await processor.process(_message(name))
    pending_after_name = dict(processor.booking_service._get_pending_confirmation("patient-1"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert pending_after_name["customer_name"] == name
    assert pending_after_name["contact_phone"] is None
    _assert_no_flowly_leakage(result["reply_text"])


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
    result = await processor.process(_message("Хочу записатися на чистку у суботу о 9"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "outside_business_hours"
    assert result["booking_result"]["start_dt"] == "2026-08-29T09:00:00+03:00"
    assert calendar.checked == []


@pytest.mark.asyncio
async def test_dental_active_booking_fresh_saturday_10_checks_calendar(dental_processor):
    processor, calendar = dental_processor

    await _seed_waiting_for_time_with_stale_pricing_context(processor)
    result = await processor.process(_message("Хочу записатися на чистку у суботу о 10"))

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


class RescheduleTrackingCalendarService(RecordingConfiguredCalendarService):
    def __init__(self) -> None:
        super().__init__()
        self.rescheduled: list[dict] = []
        self.deleted: list[str] = []

    def reschedule_event(self, event_id: str, start_dt, duration_minutes: int = 30) -> None:
        self.rescheduled.append(
            {"event_id": event_id, "start_dt": start_dt, "duration_minutes": duration_minutes}
        )

    def delete_event(self, event_id: str) -> None:
        self.deleted.append(event_id)


def _mark_dental_booking_confirmed(
    processor,
    event_id: str = "calendar-event-123",
    current_service_id: str | None = None,
    current_service_name: str | None = None,
) -> None:
    processor.booking_service._mark_booking_completed(
        "patient-1",
        start_dt=_kyiv_dt(2026, 4, 27, 12),
        customer_name="Іван",
        email="client@example.com",
        phone="0987121328",
        calendar_event_id=event_id,
        current_service_id=current_service_id,
        current_service_name=current_service_name,
    )


async def test_dental_confirmed_booking_business_hours_question_does_not_reschedule():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    result = await processor.process(_message("ви завтра працюєте о 17?"))

    assert "Графік роботи" in result["reply_text"]
    assert calendar.rescheduled == []
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert not pending or not pending.get("reschedule_pending")


async def test_dental_confirmed_booking_hours_window_question_does_not_arm_reschedule():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    result = await processor.process(_message("чи працюєте ви сьогодні до 17?"))

    assert "Графік роботи" in result["reply_text"]
    assert calendar.rescheduled == []
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert not pending or not pending.get("reschedule_pending")


async def test_dental_confirmed_booking_unrelated_meeting_mention_does_not_reschedule():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("зустріч завтра о 17"))

    assert calendar.rescheduled == []
    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["calendar_event_id"] == "calendar-event-123"


async def test_dental_confirmed_booking_price_question_does_not_reschedule():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("ціна чистки завтра о 17"))

    assert calendar.rescheduled == []


async def test_dental_confirmed_booking_bare_datetime_reschedules_once():
    # This file's clock is frozen to Saturday 2026-08-22 (see
    # freeze_dental_smoke_booking_clock); "завтра" would land on the closed
    # Sunday, so "сьогодні" is used here to land on an open day instead --
    # the reschedule-succeeds assertion is unaffected by that choice.
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    result = await processor.process(_message("сьогодні о 14"))

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"


async def test_dental_confirmed_booking_explicit_reschedule_still_works():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    result = await processor.process(_message("хочу перенести запис на сьогодні о 14"))

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"


async def test_dental_confirmed_booking_explicit_cancellation_wins_over_datetime():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    result = await processor.process(_message("скасуйте запис на завтра о 17"))

    assert result["booking_result"]["status"] == "cancelled"
    assert calendar.rescheduled == []


async def test_dental_confirmed_booking_stray_reconfirm_then_cancel_deletes_event_once():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 15"))
    confirmed = await processor.process(_message("Іван 0981112233"))
    reconfirmed = await processor.process(_message("так підтверджую"))
    cancelled = await processor.process(_message("скасуйте запис"))

    assert confirmed["booking_result"]["status"] == "confirmed"
    assert reconfirmed["intent"] == "booking_status_confirmed"
    assert reconfirmed["booking_result"] is None
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert cancelled["booking_result"]["status"] == "cancelled"
    assert "скасував" in cancelled["reply_text"].lower()
    assert "не бронюю" not in cancelled["reply_text"].lower()
    assert len(calendar.created) == 1
    assert calendar.deleted == ["dental-event-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "чи є мій запис?",
        "перевірте будь ласка запис",
        "на коли я записаний?",
        "нагадайте коли в мене запис",
        "о котрій у мене прийом?",
    ],
)
async def test_dental_confirmed_booking_natural_status_queries_read_existing_booking(text):
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    processor.booking_service._mark_booking_completed(
        "patient-1",
        start_dt=_kyiv_dt(2026, 8, 27, 15, 30),
        customer_name="Іван",
        email=None,
        phone="0981112233",
        calendar_event_id="calendar-event-status",
        current_service_id="dental_cleaning",
        current_service_name="Професійна гігієна зубів",
    )

    result = await processor.process(_message(text))
    completed = processor.booking_service._get_completed_booking("patient-1")

    assert result["intent"] == "booking_status_confirmed"
    assert result["booking_result"] is None
    assert "27.08" in result["reply_text"]
    assert "15:30" in result["reply_text"]
    assert "на Професійна гігієна зубів підтверджено" not in result["reply_text"]
    assert "Послуга: професійна гігієна зубів" in result["reply_text"]
    assert completed["calendar_event_id"] == "calendar-event-status"
    assert completed["phone"] == "0981112233"
    assert calendar.checked == []
    assert calendar.created == []
    assert calendar.rescheduled == []
    assert calendar.deleted == []


@pytest.mark.asyncio
async def test_dental_status_query_without_completed_booking_does_not_invent_booking():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("перевірте будь ласка запис"))

    assert result["intent"] != "booking_status_confirmed"
    assert "підтверджено" not in result["reply_text"].lower()
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []
    assert calendar.rescheduled == []
    assert calendar.deleted == []


@pytest.mark.asyncio
async def test_dental_natural_status_query_does_not_intercept_cancel_reschedule_or_contact_correction():
    calendar = ContactUpdateTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    processor.booking_service._mark_booking_completed(
        "patient-1",
        start_dt=_kyiv_dt(2026, 8, 27, 15, 30),
        customer_name="Іван",
        email=None,
        phone="0981112233",
        calendar_event_id="calendar-event-status",
        current_service_id="dental_cleaning",
        current_service_name="Професійна гігієна зубів",
    )

    phone = await processor.process(_message("помилився номером, правильний 0509998877"))
    reschedule = await processor.process(_message("перенесіть мій запис"))
    cancel = await processor.process(_message("скасуйте мій запис"))

    assert phone["intent"] == "booking_contact_correction"
    assert phone["booking_result"]["status"] == "contact_updated"
    assert reschedule["intent"] == "booking_reschedule"
    assert reschedule["booking_result"]["status"] == "reschedule_prompt"
    assert cancel["intent"] == "booking_cancel"
    assert cancel["booking_result"]["status"] == "cancelled"
    assert calendar.created == []
    assert len(calendar.contact_updates) == 1
    assert calendar.deleted == ["calendar-event-status"]


class ContactUpdateTrackingCalendarService(RecordingConfiguredCalendarService):
    def __init__(self) -> None:
        super().__init__()
        self.contact_updates: list[dict] = []
        self.deleted: list[str] = []

    def update_booking_contact_details(self, event_id: str, description: str) -> None:
        self.contact_updates.append({"event_id": event_id, "description": description})

    def delete_event(self, event_id: str) -> None:
        self.deleted.append(event_id)


async def test_dental_confirmed_booking_own_phone_correction_updates_record_and_calendar():
    """Repro class: confirmed booking -> "ой, я помилився номером, правильний
    0509998877" must update the stored completed-booking phone AND patch the
    existing Calendar event's description -- never create a second event."""
    calendar = ContactUpdateTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(
        processor,
        event_id="calendar-event-999",
        current_service_id="dental_cleaning",
        current_service_name="Професійна гігієна зубів",
    )

    result = await processor.process(
        _message("ой вибачте, помилився номером, правильний 0509998877")
    )

    assert result["intent"] == "booking_contact_correction"
    assert result["booking_result"]["status"] == "contact_updated"
    assert "оновили номер телефону" in result["reply_text"]
    assert "12:00" in result["reply_text"]

    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["phone"] == "0509998877"

    assert len(calendar.contact_updates) == 1
    assert calendar.contact_updates[0]["event_id"] == "calendar-event-999"
    assert "Service: Професійна гігієна зубів" in calendar.contact_updates[0]["description"]
    assert "0509998877" in calendar.contact_updates[0]["description"]
    assert calendar.created == []


async def test_dental_confirmed_booking_phone_correction_never_creates_duplicate_event():
    calendar = ContactUpdateTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor, event_id="calendar-event-999")

    await processor.process(_message("правильний номер 0509998877, помилилась раніше"))

    assert calendar.created == []
    assert len(calendar.contact_updates) == 1


async def test_dental_confirmed_booking_ambiguous_bare_phone_does_not_change_booking():
    """A bare phone number with no correction/mistake language must never
    be treated as changing the confirmed booking's contact info."""
    calendar = ContactUpdateTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor, event_id="calendar-event-999")

    result = await processor.process(_message("0509998877"))

    assert result["intent"] != "booking_contact_correction"
    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["phone"] == "0987121328"
    assert calendar.contact_updates == []
    assert calendar.created == []


async def test_dental_confirmed_booking_phone_correction_without_calendar_configured_still_saves_locally():
    """When Calendar isn't configured (the normal local/dev state), the
    corrected phone must still be saved to the local completed-booking
    record and the patient must still get a confirmation reply -- a
    Calendar sync failure must never block or hide the local correction."""
    calendar = RecordingConfiguredCalendarService()
    calendar.is_configured = lambda: False  # type: ignore[method-assign]
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor, event_id="calendar-event-999")

    result = await processor.process(
        _message("вибачте, неправильний номер написав, актуальний 0509998877")
    )

    assert result["intent"] == "booking_contact_correction"
    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["phone"] == "0509998877"


async def test_dental_confirmed_booking_explicit_reschedule_not_hijacked_by_correction_wording():
    """A reschedule request that happens to contain a "mistake" word must
    still reschedule normally -- the phone-correction path only fires when
    a real phone number is present."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    result = await processor.process(
        _message("вибачте за помилку, перенесіть будь ласка на сьогодні о 14")
    )

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1


async def test_dental_confirmed_booking_can_i_book_tomorrow_does_not_mutate_old_event():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("можна завтра о 17?"))

    assert calendar.rescheduled == []
    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["calendar_event_id"] == "calendar-event-123"


async def test_dental_direct_booking_then_bare_contact_completes_once():
    """P1-A regression guard: a single-message date/time booking request
    followed by bare contact info must attach to the already-verified slot
    and create exactly one booking (confirms this path is NOT broken)."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    offer = await processor.process(_message("хочу записатися на чистку 3 вересня о 16"))
    assert offer["booking_result"]["status"] == "waiting_for_contact"

    confirmed = await processor.process(_message("Дмитро 0987121328"))

    assert confirmed["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"].hour == 16
    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["customer_name"] == "Дмитро"
    assert completed["phone"] == "0987121328"


class BusyAt17ConfiguredCalendarService(RecordingConfiguredCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        return start_dt.hour != 17


async def test_dental_reject_busy_alternative_with_ni_asks_for_new_time():
    """P1-B: rejecting a single busy-alternative offer with "ні" must ask
    for another time/day, not return an unrelated KB answer, and must not
    create the rejected alternative."""
    calendar = BusyAt17ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    offer = await processor.process(_message("хочу на чистку 3 вересня о 17"))
    assert offer["booking_result"]["status"] == "slot_suggested"

    contact = await processor.process(_message("Дмитро 0987121328"))
    assert contact["booking_result"]["status"] != "confirmed"

    result = await processor.process(_message("ні"))

    assert result["booking_result"]["status"] == "alternative_rejected"
    assert "інший" in result["reply_text"].lower()
    assert calendar.created == []
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending.get("busy_alternative") is not True
    assert pending.get("suggested_slots") in (None, [])


async def test_dental_explicit_service_switch_beats_stale_topic_context():
    """P1-C: an explicit new-service booking request must switch away from
    a stale remembered service, not be swallowed as an unknown detail of
    the old one."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу вініри"))
    result = await processor.process(_message("ні, краще хочу записатися на чистку"))

    assert result["booking_result"]["status"] == "waiting_for_time"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.created == []


async def test_dental_fresh_negative_booking_stop_does_not_start_booking():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("не записуйте поки"))

    assert result["intent"] == "booking_flow_stopped"
    assert "не записую" in result["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_active_negative_booking_stop_clears_unconfirmed_pending():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    result = await processor.process(_message("не записуйте поки"))

    assert result["intent"] == "booking_flow_stopped"
    assert "не записую" in result["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_active_changed_mind_no_need_stops_unconfirmed_booking():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    result = await processor.process(_message("я передумав, не треба"))

    assert result["intent"] == "booking_flow_stopped"
    assert "не записую" in result["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.parametrize(
    "text",
    [
        "ні, дякую, не треба",
        "ні дякую не треба",
        "ні, не треба",
        "дякую, не треба",
        "ні, дякую",
        "не треба, дякую",
    ],
)
async def test_dental_polite_decline_stops_unconfirmed_booking_waiting_for_time(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Хочу записатися на чистку зубів"))
    await processor.process(_message("В п'ятницю"))
    checks_before = len(calendar.checked)
    creates_before = len(calendar.created)
    result = await processor.process(_message(text))

    assert result["intent"] == "booking_flow_stopped"
    assert "не записую" in result["reply_text"]
    assert "котру годину" not in result["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert len(calendar.checked) == checks_before
    assert len(calendar.created) == creates_before


@pytest.mark.parametrize(
    "text",
    [
        "ні, дякую, не треба",
        "ні дякую не треба",
        "ні, не треба",
        "дякую, не треба",
        "ні, дякую",
        "не треба, дякую",
    ],
)
async def test_dental_polite_decline_stops_unconfirmed_booking_waiting_for_contact(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Хочу записатися на чистку зубів"))
    await processor.process(_message("В середу о 15"))
    checks_before = len(calendar.checked)
    creates_before = len(calendar.created)
    result = await processor.process(_message(text))

    assert result["intent"] == "booking_flow_stopped"
    assert "не записую" in result["reply_text"]
    assert "залиште" not in result["reply_text"].lower()
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert len(calendar.checked) == checks_before
    assert len(calendar.created) == creates_before


@pytest.mark.parametrize(
    "text",
    [
        "ні, дякую, не треба",
        "ні дякую не треба",
        "ні, не треба",
        "дякую, не треба",
        "ні, дякую",
        "не треба, дякую",
    ],
)
async def test_dental_polite_decline_fresh_does_not_start_booking(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "booking_flow_stopped"
    assert "не записую" in result["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.parametrize(
    "text",
    [
        "не на чистку, а на відбілювання",
        "ні, краще на відбілювання",
        "не в п'ятницю, а в суботу",
        "ні, давайте краще завтра",
        "не о 12, а о 14",
        "ні, номер інший",
        "не знаю, який час",
        "ні, а скільки коштує?",
        "ні, а дитину теж можна записати?",
    ],
)
async def test_dental_polite_decline_does_not_hijack_corrections_or_questions(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Хочу записатися на чистку зубів"))
    await processor.process(_message("В п'ятницю"))
    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["intent"] != "booking_flow_stopped"
    assert pending is not None
    assert processor.booking_service.get_booking_state("patient-1") != BookingState.NONE
    assert calendar.created == []


async def test_dental_negative_booking_stop_does_not_hijack_service_correction():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message("не на чистку, а на відбілювання"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "teeth_whitening"
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_confirmed_cancel_still_uses_calendar_delete_path_after_stop_fix():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor, event_id="calendar-event-stop-guard")

    result = await processor.process(_message("скасуйте запис"))

    assert result["booking_result"]["status"] == "cancelled"
    assert calendar.deleted == ["calendar-event-stop-guard"]
    assert calendar.created == []


async def test_dental_active_booking_service_switch_preserves_selected_date():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу записатись на чистку у четвер"))
    result = await processor.process(_message("ні, краще на брекети"))

    assert result["booking_result"]["status"] == "waiting_for_time"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"
    assert pending["current_service_id"] == "orthodontic_consultation"
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_waiting_time_service_correction_with_zamist_preserves_date():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    result = await processor.process(_message("а можна замість чистки одразу відбілювання?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "teeth_whitening"
    assert pending["requested_date"] == "2026-08-26"
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_waiting_time_service_correction_with_not_this_but_that_switches_service():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message("ні, я не на чистку, а на відбілювання"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "teeth_whitening"
    assert pending.get("requested_date") is None
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_waiting_time_service_correction_to_consultation_preserves_date():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у понеділок"))
    result = await processor.process(_message("насправді ні, на консультацію"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "dental_consultation"
    assert pending["requested_date"] == "2026-08-24"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.parametrize(
    ("text", "service_id", "service_label"),
    [
        ("а можна замість чистки на відбілювання?", "teeth_whitening", "відбілювання"),
        ("ні, я не на чистку, а на відбілювання", "teeth_whitening", "відбілювання"),
        ("давайте краще відбілювання", "teeth_whitening", "відбілювання"),
        ("насправді хочу відбілювання", "teeth_whitening", "відбілювання"),
        ("а можна все-таки на консультацію?", "dental_consultation", "консультац"),
    ],
)
async def test_dental_waiting_time_service_correction_acknowledges_and_preserves_date(
    text, service_id, service_label
):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert result["reply_text"].startswith("Так, змінив послугу: ")
    assert service_label in result["reply_text"].lower()
    assert "середу" in result["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == service_id
    assert pending["requested_date"] == "2026-08-26"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.parametrize(
    ("text", "service_id", "service_label"),
    [
        ("ні, я не на чистку, а на відбілювання", "teeth_whitening", "відбілювання"),
        ("давайте краще відбілювання", "teeth_whitening", "відбілювання"),
        ("насправді хочу відбілювання", "teeth_whitening", "відбілювання"),
    ],
)
async def test_dental_waiting_time_service_correction_before_date_asks_for_date_with_ack(
    text, service_id, service_label
):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert result["reply_text"].startswith("Так, змінив послугу: ")
    assert service_label in result["reply_text"].lower()
    assert "який день" in result["reply_text"].lower()
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == service_id
    assert pending.get("requested_date") is None
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_waiting_contact_service_correction_preserves_selected_slot_with_ack():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    await processor.process(_message("11"))
    checks_before = len(calendar.checked)
    result = await processor.process(_message("давайте краще відбілювання"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["reply_text"].startswith("Так, змінив послугу: ")
    assert "відбілювання" in result["reply_text"].lower()
    assert "ім’я та номер телефону" in result["reply_text"]
    assert pending["state"] == "WAITING_FOR_CONTACT"
    assert pending["current_service_id"] == "teeth_whitening"
    assert pending["service_id"] == "teeth_whitening"
    assert pending["start_dt"] == "2026-08-26T11:00:00+03:00"
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


@pytest.mark.parametrize(
    "text",
    [
        "а можна на консультацію?",
        "скільки коштує консультація?",
        "а консультація є?",
    ],
)
async def test_dental_active_booking_service_correction_controls_do_not_overwrite_service(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    before = dict(processor.booking_service._get_pending_confirmation("patient-1"))
    result = await processor.process(_message(text))
    after = processor.booking_service._get_pending_confirmation("patient-1")

    assert not result["reply_text"].startswith("Так, змінив послугу: ")
    assert after["current_service_id"] == before["current_service_id"]
    assert after["requested_date"] == before["requested_date"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_unavailable_slot_later_refinement_preserves_day():
    calendar = SelectiveConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 27, 16), _kyiv_dt(2026, 8, 27, 17)]
    )
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 13"))
    later = await processor.process(_message("пізніше"))
    selected = await processor.process(_message("17"))

    assert later["booking_result"]["status"] == "time_window_slots_suggested"
    assert later["booking_result"]["requested_date"] == "2026-08-27"
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-27T17:00:00+03:00"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.created == []


@pytest.mark.parametrize(
    "text",
    [
        "робите лазерне відбілювання?",
        "ставите сапфірові брекети?",
        "імпланти Straumann ставите?",
    ],
)
async def test_dental_unknown_variant_containment_survives_service_switch_fix(text):
    """P1-C must not weaken unknown-variant containment."""
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert "у базі немає підтвердження" in result["reply_text"]
    assert calendar.created == []


async def test_dental_implant_to_consultation_still_prosthetics():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу імплант"))
    await processor.process(_message("хочу записатися на консультацію"))

    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "prosthetics_consultation"


async def test_dental_braces_to_consultation_still_orthodontic():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу брекети"))
    await processor.process(_message("хочу записатися на консультацію"))

    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "orthodontic_consultation"


async def test_dental_fragmented_reschedule_with_time_window_completes():
    """P1-D: "хочу перенести запис" -> "у п'ятницю після 16" must offer
    verified Friday slots and, once one is selected, reschedule the
    existing event exactly once."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("хочу перенести запис"))
    offer = await processor.process(_message("у п'ятницю після 16"))

    assert offer["booking_result"]["status"] == "time_window_slots_suggested"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["reschedule_pending"] is True

    result = await processor.process(_message("17:00"))

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"
    assert calendar.created == []


async def test_dental_fragmented_reschedule_date_then_time_completes_once():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("перенесіть запис"))
    await processor.process(_message("4 вересня"))
    result = await processor.process(_message("о 17"))

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"
    assert calendar.rescheduled[0]["start_dt"] == _kyiv_dt(2026, 9, 4, 17)
    assert calendar.created == []


async def test_dental_pending_reschedule_curly_apostrophe_weekday_time_completes_once():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    prompt = await processor.process(_message("чи можна перенести, у мене змінюється графік?"))
    result = await processor.process(_message("у пʼятницю о 14 буде добре"))

    assert prompt["booking_result"]["status"] == "reschedule_prompt"
    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"
    assert calendar.rescheduled[0]["start_dt"] == _kyiv_dt(2026, 8, 28, 14)
    assert calendar.created == []
    assert calendar.deleted == []


async def test_dental_pending_reschedule_curly_apostrophe_weekday_then_time_completes_once():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("чи можна перенести, у мене змінюється графік?"))
    date = await processor.process(_message("у пʼятницю"))
    result = await processor.process(_message("о 14"))

    assert date["booking_result"]["status"] == "reschedule_prompt"
    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"
    assert calendar.rescheduled[0]["start_dt"] == _kyiv_dt(2026, 8, 28, 14)
    assert calendar.created == []
    assert calendar.deleted == []


async def test_dental_pending_reschedule_curly_apostrophe_faq_does_not_reschedule():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("чи можна перенести, у мене змінюється графік?"))
    result = await processor.process(_message("ви працюєте у пʼятницю о 14?"))

    assert result["intent"] != "booking_reschedule"
    assert calendar.rescheduled == []
    assert calendar.created == []
    assert calendar.deleted == []


async def test_dental_pending_reschedule_negation_does_not_reschedule():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("чи можна перенести, у мене змінюється графік?"))
    result = await processor.process(_message("ні не треба"))

    assert result["intent"] != "booking_reschedule"
    assert calendar.rescheduled == []
    assert calendar.created == []
    assert calendar.deleted == []


async def test_dental_fragmented_reschedule_weekday_then_time_completes_once():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("перенесіть запис"))
    await processor.process(_message("у понеділок"))
    result = await processor.process(_message("о 17"))

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"
    assert calendar.rescheduled[0]["start_dt"] == _kyiv_dt(2026, 8, 24, 17)
    assert "Чекатимемо на вас у цей час" in result["reply_text"]
    assert "Зв’яжемося з вами у цей час" not in result["reply_text"]
    assert calendar.created == []


@pytest.mark.parametrize(
    "followup_text",
    [
        "чи працюєте ви в п'ятницю о 17?",
        "а скільки коштує чистка о 17?",
        "зустріч у п'ятницю о 17",
    ],
)
async def test_dental_pending_reschedule_still_invalidated_by_unrelated_text(followup_text):
    """P1-D must not broadly loosen residual-content validation -- FAQ and
    unrelated text must still invalidate the pending reschedule."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("хочу перенести запис"))
    await processor.process(_message(followup_text))

    assert calendar.rescheduled == []


@pytest.mark.parametrize(
    "followup_text",
    [
        "зустріч у п'ятницю о 17",
        "а скільки коштує чистка о 17?",
        "чи працюєте ви в п'ятницю о 17?",
    ],
)
async def test_dental_active_reschedule_offer_not_hijacked_by_unrelated_datetime(followup_text):
    """P0: once a reschedule offer is already active (slots verified,
    reschedule_pending=True), an unrelated message that merely happens to
    name a date/time matching an offered slot must not silently reschedule
    the existing confirmed booking."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    offer = await processor.process(_message("хочу перенести запис на п'ятницю ввечері"))
    assert offer["booking_result"]["status"] == "daypart_slots_suggested"

    result = await processor.process(_message(followup_text))

    assert result["booking_result"] is None or result["booking_result"].get("status") != "rescheduled"
    assert calendar.rescheduled == []
    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["calendar_event_id"] == "calendar-event-123"


async def test_dental_active_reschedule_offer_bare_datetime_still_reschedules_once():
    """P0 regression guard: a genuinely bare date/time reply to an active
    offer must still reschedule exactly once (shorthand must not be lost)."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("хочу перенести запис на п'ятницю ввечері"))
    result = await processor.process(_message("17:00"))

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"
    assert calendar.created == []


async def test_dental_explicit_reschedule_verb_survives_residual_content_guard():
    """P0 fix must not block a legitimate explicit reschedule request that
    names the reschedule verb itself ("перенесіть") while an offer is
    already active."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("хочу перенести запис на п'ятницю ввечері"))
    result = await processor.process(_message("перенесіть на суботу о 12:00"))

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"


@pytest.mark.parametrize(
    "interruption_text",
    [
        "зустріч завтра",
        "ціна чистки завтра",
        "ви завтра працюєте?",
    ],
)
async def test_dental_active_reschedule_offer_not_hijacked_by_unrelated_day_mention_then_bare_time(
    interruption_text,
):
    """P0 (multi-turn release blocker): a message that merely names a day
    (no time) but carries unrelated content must invalidate an active
    reschedule offer, the same as a single message combining unrelated
    content with a full date/time already does. Without this,
    `_reschedule_offer_still_engaged` treated any day mention as "still
    engaged", so a later, separate bare offered time could resurrect and
    reschedule the existing confirmed booking against unrelated intent."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    offer = await processor.process(_message("хочу перенести запис на п'ятницю ввечері"))
    assert offer["booking_result"]["status"] == "daypart_slots_suggested"

    await processor.process(_message(interruption_text))
    result = await processor.process(_message("17:00"))

    assert result["booking_result"] is None or result["booking_result"].get("status") != "rescheduled"
    assert calendar.rescheduled == []
    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["calendar_event_id"] == "calendar-event-123"


async def test_dental_active_reschedule_offer_day_refinement_then_bare_time_still_reschedules():
    """P0 regression guard: "а завтра?" (a genuine day refinement, not
    unrelated content) followed later by a bare offered time must still
    complete the reschedule -- the invalidation added for the unrelated-
    day-mention case above must not also block legitimate refinements."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("хочу перенести запис"))
    await processor.process(_message("3 вересня ввечері"))
    await processor.process(_message("а завтра?"))
    result = await processor.process(_message("17"))

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"


async def test_dental_active_reschedule_offer_unrelated_faq_then_bare_time_does_not_reschedule():
    """P0 regression guard: an unrelated FAQ interruption followed later by
    a bare number matching a previously offered slot must not reschedule
    the existing confirmed booking (the multi-turn counterpart of the
    already-covered single-message unrelated-content case)."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    offer = await processor.process(_message("хочу перенести запис на п'ятницю ввечері"))
    assert offer["booking_result"]["status"] == "daypart_slots_suggested"

    await processor.process(_message("а скільки коштує чистка?"))
    result = await processor.process(_message("17:00"))

    assert result["booking_result"] is None or result["booking_result"].get("status") != "rescheduled"
    assert calendar.rescheduled == []
    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["calendar_event_id"] == "calendar-event-123"


class BusyAllSaturdayConfiguredCalendarService(RecordingConfiguredCalendarService):
    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append({"start_dt": start_dt, "duration_minutes": duration_minutes})
        return start_dt.weekday() != 5


async def test_dental_nearest_availability_production_phrase_offers_verified_slot():
    """Matrix 1: a pain mention combined with a nearest-availability request
    ("на коли найближчий час можна записатись до дантиста?") must acknowledge
    the pain, resolve the generic dental-consultation service, run a real
    Calendar search, and offer the first verified slot -- not repeat the
    generic "which day and time?" prompt, and not create anything yet."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(
        _message("В мене болить зуб, на коли найближчий час можна записатись до дантиста?")
    )

    assert result["booking_result"]["status"] == "nearest_availability_suggested"
    assert "Розумію" in result["reply_text"]
    assert "найближчий вільний час" in result["reply_text"]
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "dental_consultation"
    assert len(calendar.checked) >= 1
    assert calendar.created == []


async def test_dental_pain_nearest_plain_when_can_come_searches_calendar():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(
        _message("дуже болить зуб, не можу жувати, коли найближче можна?")
    )

    assert result["booking_result"]["status"] == "nearest_availability_suggested"
    assert "Розумію" in result["reply_text"]
    assert "найближчий вільний час" in result["reply_text"]
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "dental_consultation"
    assert len(calendar.checked) >= 1
    assert calendar.created == []


@pytest.mark.parametrize(
    "followup_text",
    [
        "найближчий час",
        "коли найближче?",
        "який найближчий вільний час?",
    ],
)
async def test_dental_nearest_availability_variants_after_known_service(followup_text):
    """Matrix 2-5: once a service is already established, every nearest-
    availability wording must run a real forward Calendar search and offer
    a verified slot -- not repeat the generic day/time prompt, and not fall
    into the old hardcoded tomorrow/day-after-tomorrow availability-question
    generator (which "вільний час" wording used to trigger)."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    first = await processor.process(_message("хочу на чистку"))
    assert first["booking_result"]["status"] == "waiting_for_time"
    checks_before = len(calendar.checked)

    result = await processor.process(_message(followup_text))

    assert result["booking_result"]["status"] == "nearest_availability_suggested"
    assert "Розумію" not in result["reply_text"]
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "dental_cleaning"
    assert len(calendar.checked) > checks_before
    assert calendar.created == []


async def test_dental_nearest_availability_skips_busy_and_closed_days():
    """Availability safety: the search must only ever return a Calendar-
    verified slot within valid business hours, skipping a fully busy day
    (Saturday here) and a closed day (Sunday) without ever offering either."""
    calendar = BusyAllSaturdayConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message("найближчий час"))

    assert result["booking_result"]["status"] == "nearest_availability_suggested"
    start_dt = processor.booking_service._deserialize_pending_start_dt(
        result["booking_result"]["start_dt"]
    )
    assert start_dt.weekday() == 0  # Monday -- Saturday busy, Sunday closed
    assert start_dt.hour == 9
    assert all(check["start_dt"].weekday() != 6 for check in calendar.checked)


async def test_dental_nearest_availability_requires_acceptance_and_contact_before_create():
    """Availability safety: the offered nearest slot must never create a
    Calendar event by itself -- only after the normal accept + contact
    sequence, exactly once."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    offer = await processor.process(_message("найближчий час"))
    assert calendar.created == []

    accepted = await processor.process(_message("так, підходить"))
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert calendar.created == []

    completed = await processor.process(_message("Дмитро 0987121328"))
    assert completed["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1


@pytest.mark.parametrize("confirmation", ["так", "добре", "підходить", "ок", "ок записуй", "давайте", "давай"])
async def test_dental_nearest_single_slot_accepts_natural_confirmations(confirmation):
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("найближчий час"))
    accepted = await processor.process(_message(confirmation))

    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["start_dt"]
    assert calendar.created == []


async def test_dental_multiple_suggested_slots_bare_confirmation_never_picks_arbitrarily():
    calendar = SelectiveConfiguredCalendarService(
        [_kyiv_dt(2026, 9, 3, 17), _kyiv_dt(2026, 9, 3, 17, 30)]
    )
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("3 вересня"))
    await processor.process(_message("ввечері"))
    accepted = await processor.process(_message("ок"))

    assert accepted["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert "start_dt" not in pending
    assert calendar.created == []


async def test_dental_nearest_availability_without_service_still_asks_which_service():
    """Guard: a bare nearest-availability request with no pain/dentist
    context and no prior service or booking intent must not silently
    default to a service or touch the Calendar -- the generic
    dental_consultation fallback is bounded to an actual pain/dentist
    mention, not every unresolved-service nearest-availability request."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("найближчий час"))

    assert result["booking_result"] is None
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert not pending or not pending.get("current_service_id")
    assert calendar.checked == []


async def test_dental_pain_mention_alone_gets_acknowledgement_without_booking():
    """Matrix 6: a bare pain mention with no booking intent gets a neutral
    human acknowledgement -- no diagnosis, no Calendar interaction."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("болить зуб"))

    assert "Розумію" in result["reply_text"]
    assert "діагноз" not in result["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []
    assert processor.booking_service.get_booking_state("patient-1").value == "NONE"


async def test_dental_greeting_with_pain_offers_nearest_booking_followup():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    pain = await processor.process(_message("Добрий день, зуб дуже болить другий день"))
    accepted = await processor.process(_message("Так, передай адміністратору"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert pain["intent"] == "pain_acknowledgement"
    assert "Шкода" in pain["reply_text"] or "Розумію" in pain["reply_text"]
    assert "Подивитися найближчий запис" in pain["reply_text"]
    assert "Чим можемо допомогти" not in pain["reply_text"]
    assert accepted["booking_result"]["status"] == "nearest_availability_suggested"
    assert "найближчий вільний час" in accepted["reply_text"]
    assert "Що саме передати" not in accepted["reply_text"]
    assert pending["current_service_id"] == "dental_consultation"
    assert len(calendar.checked) >= 1
    assert calendar.created == []


async def test_dental_pain_offer_accepts_nearest_availability_wording():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 22, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    pain = await processor.process(_message("дуже болить зуб, не можу жувати"))
    accepted = await processor.process(_message("давайте найближчий час"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert pain["intent"] == "pain_acknowledgement"
    assert accepted["booking_result"]["status"] == "nearest_availability_suggested"
    assert "15:00" in accepted["reply_text"]
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.created == []


async def test_dental_pain_offer_explicit_booking_uses_message_date_window():
    calendar = SelectiveConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 24, 17), _kyiv_dt(2026, 8, 24, 17, 30)]
    )
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    pain = await processor.process(_message("дуже болить зуб, не можу жувати"))
    result = await processor.process(_message("давайте на понеділок ввечері запишете?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert pain["intent"] == "pain_acknowledgement"
    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "daypart_slots_suggested"
    assert "17:00" in result["reply_text"]
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.checked
    assert calendar.created == []


async def test_dental_pain_offer_explicit_closed_day_booking_does_not_fallback():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("дуже болить зуб, не можу жувати"))
    result = await processor.process(_message("давайте на завтра ввечері запишете?"))

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "outside_business_hours"
    assert "неділю" in result["reply_text"]
    assert "Хочу правильно зорієнтувати" not in result["reply_text"]
    assert calendar.created == []


async def test_dental_pain_offer_negated_booking_does_not_start_booking():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("дуже болить зуб, не можу жувати"))
    result = await processor.process(_message("не хочу записуватись"))

    assert result["booking_result"] is None
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_pain_offer_faq_does_not_start_booking():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("дуже болить зуб, не можу жувати"))
    result = await processor.process(_message("скільки коштує консультація?"))

    assert result["booking_result"] is None
    assert "700 грн" in result["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_explicit_wisdom_extraction_price_beats_stale_caries_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує консультація?"))
    await processor.process(_message("а лікування карієсу?"))
    result = await processor.process(_message("а видалення зуба мудрості?"))
    context = processor.memory_service.get_context("patient-1")

    assert "3500 грн" in result["reply_text"]
    assert "2200 грн" not in result["reply_text"]
    assert context["current_service_id"] == "wisdom_tooth_extraction"
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_pain_specific_service_context_preserves_pronoun_extraction_price():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    pain = await processor.process(_message("болить зуб мудрості"))
    result = await processor.process(_message("скільки коштує його видалити?"))
    context = processor.memory_service.get_context("patient-1")

    assert pain["intent"] == "pain_acknowledgement"
    assert result["intent"] == "front_desk_contextual_answer"
    assert "3500 грн" in result["reply_text"]
    assert "1800 грн" not in result["reply_text"]
    assert context["current_service_id"] == "wisdom_tooth_extraction"
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_pain_specific_service_context_carries_into_booking_followup():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 24, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("болить зуб мудрості"))
    result = await processor.process(_message("давайте на понеділок о 15 запишете?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_flow"
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.checked == [{"start_dt": _kyiv_dt(2026, 8, 24, 15), "duration_minutes": 30}]
    assert calendar.created == []


async def test_dental_pain_detail_keeps_nearest_booking_offer_context():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 22, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    pain = await processor.process(_message("Добрий день, зуб дуже болить другий день"))
    detail = await processor.process(_message("вже постійний"))
    accepted = await processor.process(_message("давайте"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert pain["intent"] == "pain_acknowledgement"
    assert detail["intent"] == "pain_acknowledgement"
    assert "краще не відкладати" in detail["reply_text"]
    assert "Хочу правильно зорієнтувати" not in detail["reply_text"]
    assert accepted["booking_result"]["status"] == "nearest_availability_suggested"
    assert "15:00" in accepted["reply_text"]
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_vague_symptom_clears_stale_xray_pricing_context():
    processor, calendar = _build_dental_processor()

    xray = await processor.process(_message("скільки коштує рентген?"))
    symptom = await processor.process(_message("зуб турбує"))
    context = processor.memory_service.get_context("patient-1")

    assert "Рентген-діагностика" in xray["reply_text"]
    assert symptom["intent"] == "pain_acknowledgement"
    assert "Розумію" in symptom["reply_text"]
    assert "рентген" not in symptom["reply_text"].lower()
    assert "400 грн" not in symptom["reply_text"]
    assert context.get("current_service_id") is None
    assert context["question_context"] == "pain_nearest_offer"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_vague_symptom_prevents_next_price_followup_from_reusing_xray():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує рентген?"))
    await processor.process(_message("зуб турбує"))
    price = await processor.process(_message("скільки коштує?"))
    context = processor.memory_service.get_context("patient-1")

    assert "Рентген-діагностика" not in price["reply_text"]
    assert "400 грн" not in price["reply_text"]
    assert "600 грн" not in price["reply_text"]
    assert "1200 грн" not in price["reply_text"]
    assert price["intent"] == "front_desk_safe_fallback"
    assert context.get("current_service_id") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first_text", ["скільки коштує консультація?", "скільки лікування карієсу?"])
async def test_dental_vague_symptom_clears_other_stale_pricing_contexts(first_text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message(first_text))
    symptom = await processor.process(_message("мене турбує зуб"))
    price = await processor.process(_message("скільки коштує?"))
    context = processor.memory_service.get_context("patient-1")

    assert symptom["intent"] == "pain_acknowledgement"
    assert "Розумію" in symptom["reply_text"]
    assert "700 грн" not in symptom["reply_text"]
    assert "2200 грн" not in symptom["reply_text"]
    assert "700 грн" not in price["reply_text"]
    assert "2200 грн" not in price["reply_text"]
    assert context.get("current_service_id") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["болить зуб", "зуб турбує"])
async def test_dental_fresh_vague_symptom_uses_pain_acknowledgement(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "pain_acknowledgement"
    assert "Розумію" in result["reply_text"]
    assert "Подивитися найближчий запис" in result["reply_text"]
    assert context.get("current_service_id") is None
    assert context["question_context"] == "pain_nearest_offer"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_explicit_xray_pricing_still_resolves_normally():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("скільки коштує рентген?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Рентген-діагностика" in result["reply_text"]
    assert "400 грн" in result["reply_text"]
    assert context["current_service_id"] == "dental_diagnostics"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_procedure_comfort_followup_keeps_known_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує видалення зуба?"))
    result = await processor.process(_message("а це боляче?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Видалення зуба" in result["reply_text"]
    assert "комфорту" in result["reply_text"]
    assert context["current_service_id"] == "tooth_extraction"
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_pain_mention_with_booking_intent_starts_booking_flow():
    """Matrix 7: pain mentioned together with explicit booking intent gets
    acknowledged once, then proceeds into the normal booking flow (here:
    asking which service, since none was named) -- no invented diagnosis."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("дуже болить зуб, хочу записатись"))

    assert result["booking_result"]["status"] == "waiting_for_service"
    assert "Розумію" in result["reply_text"]
    assert "На яку послугу" in result["reply_text"]
    assert calendar.checked == []


@pytest.mark.asyncio
async def test_dental_generic_dentist_booking_asks_for_service_before_time():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Привіт, хочу записатись до стоматолога"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "waiting_for_service"
    assert result["reply_text"].startswith("Вітаю!")
    assert "На яку послугу" in result["reply_text"]
    assert pending.get("current_service_id") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_generic_dentist_booking_ignores_stale_consultation_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує консультація?"))
    result = await processor.process(_message("Привіт, хочу записатись до стоматолога"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "waiting_for_service"
    assert "На яку послугу" in result["reply_text"]
    assert pending.get("current_service_id") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_final_recheck_busy_preserves_context_and_accepts_verified_alternative():
    calendar = SlotBusyOnFinalRecheckCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    selected = await processor.process(_message("хочу записатись на чистку у понеділок о 12"))
    alternative = await processor.process(_message("0987121328 Діма"))
    pending_after_alternative = processor.booking_service._get_pending_confirmation("patient-1") or {}
    accepted = await processor.process(_message("Так, на 13"))

    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert alternative["booking_result"]["status"] == "slot_suggested"
    assert "13:00" in alternative["reply_text"]
    assert pending_after_alternative["current_service_id"] == "dental_cleaning"
    assert pending_after_alternative["customer_name"] == "Діма"
    assert pending_after_alternative["contact_phone"] == "0987121328"
    assert pending_after_alternative["busy_alternative"] is True
    assert pending_after_alternative["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-24T13:00:00+03:00"}
    ]
    assert accepted["booking_result"]["status"] == "confirmed"
    assert accepted["booking_result"]["customer_name"] == "Діма"
    assert accepted["booking_result"]["contact_phone"] == "0987121328"
    assert calendar.created[-1]["start_dt"] == _kyiv_dt(2026, 8, 24, 13)
    assert len(calendar.created) == 1


@pytest.mark.asyncio
async def test_dental_final_recheck_busy_without_verified_alternative_never_uses_fallback_slots():
    calendar = SlotBusyOnFinalRecheckNoAlternativeCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    selected = await processor.process(_message("хочу записатись на чистку у понеділок о 12"))
    unavailable = await processor.process(_message("0987121328 Діма"))

    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert unavailable["booking_result"]["status"] == "unavailable"
    assert "інший" in unavailable["reply_text"]
    assert "час" in unavailable["reply_text"]
    assert "день" in unavailable["reply_text"]
    assert "завтра о 12:00" not in unavailable["reply_text"]
    assert "завтра о 15:00" not in unavailable["reply_text"]
    assert "післязавтра" not in unavailable["reply_text"]
    assert calendar.created == []


async def test_dental_pain_diagnosis_question_still_blocked():
    """Matrix 8: asking the bot to confirm a specific diagnosis must still
    be refused by the existing diagnosis-safety guard, unaffected by the
    new pain-acknowledgement/nearest-availability handling."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("болить зуб, це пульпіт?"))

    assert "не можу поставити діагноз" in result["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []


# ---------------------------------------------------------------------------
# Conversational understanding fix: natural slot selection (A1-A7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply_text",
    [
        "17 година буде зручно",
        "17 година буде зручноо",
        "давайте на 17",
        "підходить 17",
        "тоді 17",
        "хочу на 17",
        "мені на 17",
        "можна 17?",
    ],
)
async def test_dental_natural_offered_slot_selection_variants(reply_text):
    """A1-A4: natural phrasing that directly answers an active multi-slot
    offer ("17 година буде зручно", typos included, "підходить 17", etc.)
    must select the matching offered slot and progress to contact -- not
    repeat the generic "on which time?" prompt."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    offer = await processor.process(_message("3 вересня ввечері"))
    assert offer["booking_result"]["status"] == "daypart_slots_suggested"

    result = await processor.process(_message(reply_text))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-09-03T17:00:00+03:00"
    assert calendar.created == []


@pytest.mark.parametrize(
    "reply_text",
    [
        "мені 17 років",
        "нас буде 10 людей",
        "дитині 5 років",
    ],
)
async def test_dental_natural_slot_selection_excludes_unrelated_numbers(reply_text):
    """A5-A7: age/headcount numbers that happen to equal an offered slot's
    hour must never be mistaken for a time selection."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("3 вересня ввечері"))

    result = await processor.process(_message(reply_text))

    assert result["booking_result"]["status"] != "waiting_for_contact"
    assert calendar.created == []


async def test_dental_natural_slot_selection_reschedule_flow_too():
    """The same natural-language tolerance applies to an active reschedule
    offer, not just a fresh booking -- reusing the same shared parsing
    path, still gated by the existing residual-content safety check."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("хочу перенести запис на п'ятницю ввечері"))
    result = await processor.process(_message("17 година буде зручно"))

    assert result["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1
    assert calendar.rescheduled[0]["event_id"] == "calendar-event-123"


# ---------------------------------------------------------------------------
# Conversational understanding fix: offer continuity (B8-B9)
# ---------------------------------------------------------------------------


async def test_dental_greeting_during_active_offer_restates_context():
    """B8: a harmless greeting mid-offer must not be treated as answering
    or resetting the pending question -- it should acknowledge and restate
    the still-active offer, not fall back to a generic prompt."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    offer = await processor.process(_message("3 вересня ввечері"))
    assert offer["booking_result"]["status"] == "daypart_slots_suggested"

    result = await processor.process(_message("Привіт"))

    assert result["booking_result"]["status"] == "greeting_during_active_offer"
    assert "17:00" in result["reply_text"]
    assert calendar.created == []
    # The offer must still be genuinely selectable afterwards.
    followup = await processor.process(_message("17:00"))
    assert followup["booking_result"]["status"] == "waiting_for_contact"


async def test_dental_greeting_during_reschedule_offer_does_not_resurrect_stale_state():
    """B8/B9 safety boundary: a greeting during an ACTIVE RESCHEDULE offer
    must not keep dangerous reschedule state alive -- the existing stale-
    reschedule-offer invalidation (which treats a bare greeting as "not
    engaged") must still run first and clear it; the greeting handler must
    then find nothing left to restate."""
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    _mark_dental_booking_confirmed(processor)

    await processor.process(_message("хочу перенести запис на п'ятницю ввечері"))
    greeting_result = await processor.process(_message("Привіт"))
    assert greeting_result["booking_result"] is None or greeting_result["booking_result"].get(
        "status"
    ) != "greeting_during_active_offer"

    result = await processor.process(_message("17:00"))

    assert result["booking_result"] is None or result["booking_result"].get("status") != "rescheduled"
    assert calendar.rescheduled == []
    completed = processor.booking_service._get_completed_booking("patient-1")
    assert completed["calendar_event_id"] == "calendar-event-123"


# ---------------------------------------------------------------------------
# Conversational understanding fix: supported service grounding (C10-C15)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ставите брекети?",
        "можна поставити брекети?",
        "хочу поставити брекети",
        "робите брекети?",
        "запишіть на брекети",
    ],
)
async def test_dental_ordinary_action_verbs_do_not_contain_supported_service(text):
    """C10-C11: ordinary action verbs around a supported base service
    ("ставите", "поставити", "робите", "запишіть") must not trigger the
    unsupported-variant safe-uncertainty reply -- the service is grounded
    in the KB and should be answered directly."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message(text))

    assert "у базі немає підтвердження" not in result["reply_text"]


async def test_dental_grounded_price_question_for_supported_service():
    """C12: a plain price question for a supported service must be
    answered with the grounded KB price, not contained as unknown."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("скільки коштують брекети?"))

    assert "у базі немає підтвердження" not in result["reply_text"]


@pytest.mark.parametrize(
    "text",
    [
        "сапфірові брекети ставите?",
        "імпланти Straumann робите?",
        "лазерне відбілювання робите?",
    ],
)
async def test_dental_unsupported_variant_containment_still_works(text):
    """C13-C15: an unsupported qualifier/brand/material on top of a
    supported base service must still trigger safe-uncertainty containment
    -- the broadened action-verb exclusion must not weaken this."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message(text))

    assert "у базі немає підтвердження" in result["reply_text"]


async def test_dental_unsupported_booking_then_supported_service_switch_starts_booking():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    unsupported = await processor.process(_message("хочу записатися на лазерне відбілювання"))
    switched = await processor.process(_message("тоді на чистку"))

    assert "у базі немає підтвердження" in unsupported["reply_text"]
    assert switched["booking_result"]["status"] == "waiting_for_time"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_unsupported_booking_then_acknowledged_supported_service_switch_starts_booking():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    unsupported = await processor.process(_message("хочу записатися на лазерне відбілювання"))
    switched = await processor.process(_message("гаразд, тоді на чистку"))

    assert "у базі немає підтвердження" in unsupported["reply_text"]
    assert switched["booking_result"]["status"] == "waiting_for_time"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_unknown_detail_ack_without_service_still_retains_subject():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу записатися на лазерне відбілювання"))
    result = await processor.process(_message("гаразд, уточніть"))

    assert result["intent"] == "unknown_detail_followup"
    assert "Відбілювання зубів" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.parametrize(
    "text",
    [
        "можна замовити набір для відбілювання поштою, без візиту?",
        "хочу зробити відбілювання дистанційно без прийому",
        "можна отримати капи для елайнерів з доставкою без консультації?",
    ],
)
async def test_dental_no_visit_service_constraint_does_not_start_booking(text):
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message(text))

    assert result["intent"] == "front_desk_safe_fallback"
    assert "немає підтвердження" in result["reply_text"]
    assert "дистанційно" in result["reply_text"]
    assert result["booking_result"] is None
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.NONE
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.parametrize(
    ("text", "expected_service_id"),
    [
        ("хочу записатись на відбілювання", "teeth_whitening"),
        ("можна на відбілювання?", "teeth_whitening"),
        ("хочу консультацію", "dental_consultation"),
    ],
)
async def test_dental_no_visit_constraint_false_positives_still_book(text, expected_service_id):
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message(text))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == expected_service_id
    assert "немає підтвердження" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_pediatric_cleaning_adult_price_followup_switches_to_adult():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("дитині 7 років треба чистку"))
    child_price = await processor.process(_message("скільки?"))
    adult_price = await processor.process(_message("а дорослим скільки?"))

    assert "1200 грн" in child_price["reply_text"]
    assert "1800 грн" in adult_price["reply_text"]
    assert "1200 грн" not in adult_price["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_pediatric_cleaning_for_adult_followup_switches_to_adult():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("дитині треба чистку"))
    await processor.process(_message("скільки?"))
    adult_price = await processor.process(_message("а для дорослого?"))

    assert "1800 грн" in adult_price["reply_text"]
    assert "1200 грн" not in adult_price["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_unknown_detail_after_context_does_not_repeat_old_service():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    sedation = await processor.process(_message("робите седацію?"))
    laser = await processor.process(_message("а лазерне лікування зубів?"))

    assert "Седація" in sedation["reply_text"]
    assert "у базі немає підтвердження" in laser["reply_text"]
    assert "лазерне лікування зубів" in laser["reply_text"]
    assert "Седація" not in laser["reply_text"]
    assert "лазер" not in sedation["reply_text"].lower()
    assert laser["reply_text"] != sedation["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_unsupported_variant_after_broader_context_is_contained():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("ставите брекети?"))
    sapphire = await processor.process(_message("ставите сапфірові брекети?"))

    assert "у базі немає підтвердження" in sapphire["reply_text"]
    assert "Брекети" in sapphire["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_short_unsupported_variant_after_broader_context_is_contained():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("брекети ставите?"))
    sapphire = await processor.process(_message("сапфірові є?"))

    assert "у базі немає підтвердження" in sapphire["reply_text"]
    assert "Брекети" in sapphire["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_active_booking_specialty_faq_then_consultation_redirects_to_specialty():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    veneers = await processor.process(_message("а вініри робите?"))
    booking = await processor.process(_message("хочу записатися на консультацію"))

    assert "встановлюють керамічні вініри" in veneers["reply_text"]
    assert booking["booking_result"]["status"] == "waiting_for_time"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["current_service_id"] == "prosthetics_consultation"
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_active_booking_natural_hours_question_offers_slots(monkeypatch):
    class FridayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 28, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", FridayDentalSmokeDatetime)
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 29, 10),
        _kyiv_dt(2026, 8, 29, 10, 30),
        _kyiv_dt(2026, 8, 29, 11),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("пломби ставите?"))
    await processor.process(_message("ок, хочу записатися"))
    result = await processor.process(_message("завтра які є години?"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "10:00, 10:30 або 11:00" in result["reply_text"]
    assert calendar.created == []


async def test_dental_nearest_slot_polite_acceptance_progresses_to_contact():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 29, 10)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    offered = await processor.process(_message("болить зуб, коли найближче?"))
    accepted = await processor.process(_message("ок записуйте"))

    assert offered["booking_result"]["status"] == "nearest_availability_suggested"
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert accepted["booking_result"]["start_dt"] == "2026-08-29T10:00:00+03:00"
    assert calendar.created == []


# ---------------------------------------------------------------------------
# Conversational understanding fix: contextual uncertainty follow-up (D16-D17)
# ---------------------------------------------------------------------------


async def test_dental_uncertainty_followup_retains_subject_without_fake_handoff():
    """D16: "гаразд, уточніть" right after a safe-uncertainty containment
    reply must retain the unresolved subject (braces) rather than asking a
    generic "what would you like clarified?", and must not claim a human
    handoff that never actually happens."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("сапфірові брекети ставите?"))
    result = await processor.process(_message("Гаразд, уточніть"))

    assert "Брекети" in result["reply_text"]
    assert "що саме уточнити" not in result["reply_text"].lower()
    assert "передав" not in result["reply_text"].lower()
    assert "передам" not in result["reply_text"].lower()


async def test_dental_uncertainty_followup_bare_ack_does_not_start_booking():
    """D17: a bare "добре" after the same containment reply must not
    fabricate a clarification or accidentally start a booking flow for the
    unresolved service."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("сапфірові брекети ставите?"))
    result = await processor.process(_message("добре"))

    assert result["booking_result"] is None
    assert "Брекети" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_unknown_detail_does_not_collect_fake_admin_callback_details():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    turns = [
        await processor.process(_message("ставите сапфірові брекети?")),
        await processor.process(_message("Дмитро 0987121328")),
        await processor.process(_message("дзвінок")),
        await processor.process(_message("завтра")),
        await processor.process(_message("та пофіг")),
    ]
    combined = "\n".join(turn["reply_text"] for turn in turns)

    assert "немає підтвердження" in turns[0]["reply_text"].lower()
    assert all(turn["booking_result"] is None for turn in turns)
    assert "вам зателефонують" not in combined.lower()
    assert "передам" not in combined.lower()
    assert "уточню" not in combined.lower()
    assert "коли передзвонити" not in combined.lower()
    assert "спосіб зв" not in combined.lower()
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_uncertainty_followup_scoped_to_pending_marker_only():
    """Guard: a generic "так"/"добре" with no preceding unknown-detail
    containment reply must be completely unaffected by the new followup
    handler (it must not globally intercept acknowledgement words)."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("так"))

    assert "Брекети" not in result["reply_text"]


async def test_dental_date_only_correction_preserves_previous_time():
    """P1: "не, краще 4 вересня" must not misread the day-of-month digit
    "4" as an hour (04:00) -- it must preserve the previously-known 16:00
    and re-check it on the corrected date."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу чистку 3 вересня о 16"))
    result = await processor.process(_message("не, краще 4 вересня"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-09-04T16:00:00+03:00"


async def test_dental_date_only_correction_numeric_format_preserves_time():
    """P1: same guard for the numeric "DD.MM" date format -- neither the
    day nor month component may be misread as an hour."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу чистку 3 вересня о 16"))
    result = await processor.process(_message("не, краще 04.09"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-09-04T16:00:00+03:00"


async def test_dental_negated_date_replacement_does_not_preserve_old_date():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу записатись на консультацію сьогодні"))
    result = await processor.process(_message("ні краще не сьогодні а в понеділок після 15"))

    assert result["booking_result"]["booking_state"] == "WAITING_FOR_CONTACT"
    assert result["booking_result"]["start_dt"] == "2026-08-24T15:00:00+03:00"
    assert "сьогодні" not in result["reply_text"].lower()
    assert calendar.created == []


@pytest.mark.parametrize(
    ("text", "expected_start_dt"),
    [
        ("4 вересня о 17", "2026-09-04T17:00:00+03:00"),
        ("04.09 о 17", "2026-09-04T17:00:00+03:00"),
    ],
)
async def test_dental_date_with_explicit_time_still_parses_exact_hour(text, expected_start_dt):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу чистку"))
    result = await processor.process(_message(text))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == expected_start_dt


async def test_dental_busy_alternative_invalidated_by_faq_then_fresh_checked():
    """P2: a busy-slot alternative offer must be dropped by an unrelated
    FAQ interruption; a later coincidental match gets a fresh Calendar
    check rather than being silently trusted from the stale offer."""
    calendar = BusyAt17ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    offer = await processor.process(_message("хочу на чистку 3 вересня о 17"))
    assert offer["booking_result"]["status"] == "slot_suggested"
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["busy_alternative"] is True

    await processor.process(_message("а скільки коштує чистка"))
    pending_after_faq = processor.booking_service._get_pending_confirmation("patient-1")
    assert not pending_after_faq.get("busy_alternative")

    checks_before = len(calendar.checked)
    result = await processor.process(_message("18"))

    assert len(calendar.checked) > checks_before
    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert calendar.created == []


async def test_dental_busy_alternative_contact_only_does_not_invalidate_offer():
    """P2 fix must not regress: supplying contact info alone (the missing
    piece the offer is waiting on) must not be treated as abandoning the
    offer -- explicit acceptance afterwards must still complete once."""
    calendar = BusyAt17ConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку 3 вересня о 17"))
    await processor.process(_message("Дмитро 0987121328"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending["busy_alternative"] is True

    result = await processor.process(_message("так, підходить"))

    assert result["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"].hour == 18


async def test_dental_ok_book_it_resumes_pending_contact_not_fresh_booking():
    """P1-E: "ок записуй" while WAITING_FOR_CONTACT for an already-verified
    slot must resume that booking, not reset it."""
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу чистку"))
    offer = await processor.process(_message("можна 3 вересня о 17?"))
    assert offer["booking_result"]["status"] == "waiting_for_contact"

    await processor.process(_message("стоп а скільки чистка?"))
    pending_before = processor.booking_service._get_pending_confirmation("patient-1")
    assert pending_before["state"] == "WAITING_FOR_CONTACT"
    assert pending_before.get("start_dt")

    resume = await processor.process(_message("ок записуй"))
    pending_after = processor.booking_service._get_pending_confirmation("patient-1")

    assert pending_after["state"] == "WAITING_FOR_CONTACT"
    assert pending_after.get("start_dt") == pending_before.get("start_dt")
    assert calendar.created == []

    confirmed = await processor.process(_message("Дмитро 0987121328"))

    assert confirmed["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1


async def test_dental_records_still_start_fresh_booking_without_active_contact_wait():
    """P1-E caveat: outside an active WAITING_FOR_CONTACT booking,
    "записуй"/"хочу записатися" must still start a fresh booking."""
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("хочу записатись"))

    assert result["booking_result"]["status"] == "waiting_for_service"


async def test_dental_pediatric_cleaning_bare_adult_followup_resolves_dental_cleaning():
    """P1-F: a bare "а дорослим?" after discussing pediatric cleaning must
    resolve to adult dental_cleaning, not an unrelated service."""
    processor, calendar = _build_dental_processor()

    await processor.process(_message("дитяча чистка є?"))
    await processor.process(_message("скільки?"))
    result = await processor.process(_message("а дорослим?"))

    assert "1800" in result["reply_text"]
    assert "19000" not in result["reply_text"]
    ctx = processor.memory_service.get_context("patient-1")
    assert ctx.get("current_service_id") == "dental_cleaning"


async def test_dental_pediatric_caries_child_cleaning_followup_still_pediatric():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("дитині треба лікувати карієс"))
    result = await processor.process(_message("а чистку дітям робите?"))

    assert "дитячу профілактичну чистку" in result["reply_text"]


async def test_dental_pediatric_caries_adult_cleaning_followup_still_switches():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("дитині треба лікувати карієс"))
    result = await processor.process(_message("а дорослим чистку робите?"))

    assert "професійну гігієну" in result["reply_text"]


@pytest.mark.asyncio
async def test_dental_early_contact_survives_invalid_time_then_valid_time():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер, Дмитро, 0987121328"))
    invalid = await processor.process(_message("о 22"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert invalid["booking_result"]["status"] == "outside_business_hours"
    assert invalid["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["customer_name"] == "Дмитро"
    assert pending["contact_phone"] == "0987121328"
    assert "start_dt" not in pending
    assert calendar.created == []

    confirmed = await processor.process(_message("14"))
    completed = processor.booking_service._get_completed_booking("patient-1")

    assert confirmed["booking_result"]["status"] == "confirmed"
    assert completed["customer_name"] == "Дмитро"
    assert completed["phone"] == "0987121328"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 14)


@pytest.mark.asyncio
async def test_dental_early_contact_busy_time_preserves_context_without_create():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер, Дмитро, 0987121328"))
    busy = await processor.process(_message("14"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert busy["booking_result"]["status"] == "slot_suggested"
    assert busy["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert busy["booking_result"]["start_dt"] == "2026-08-27T15:00:00+03:00"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["customer_name"] == "Дмитро"
    assert pending["contact_phone"] == "0987121328"
    assert "start_dt" not in pending
    assert pending["suggested_slots"] == [
        {"day_key": "tomorrow", "start_dt": "2026-08-27T15:00:00+03:00"}
    ]
    assert calendar.checked == [
        {"start_dt": _kyiv_dt(2026, 8, 27, 14), "duration_minutes": 30},
        {"start_dt": _kyiv_dt(2026, 8, 27, 15), "duration_minutes": 30},
    ]
    assert calendar.created == []

    for text in ["Дмитро", "0987121328", "Дмитро 0987121328"]:
        repeated = await processor.process(_message(text))
        assert repeated["booking_result"]["status"] == "waiting_for_time"
        assert calendar.created == []

    accepted = await processor.process(_message("давайте 15"))

    assert accepted["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 15)


@pytest.mark.asyncio
async def test_dental_early_contact_survives_faq_interruption_and_resumes():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер, Дмитро, 0987121328"))
    faq = await processor.process(_message("скільки коштує відбілювання?"))
    pending_after_faq = processor.booking_service._get_pending_confirmation("patient-1")
    confirmed = await processor.process(_message("14"))

    assert faq["intent"] == "booking_grounded_question"
    assert pending_after_faq["state"] == "WAITING_FOR_TIME"
    assert pending_after_faq["customer_name"] == "Дмитро"
    assert pending_after_faq["contact_phone"] == "0987121328"
    assert confirmed["booking_result"]["status"] == "confirmed"
    assert len(calendar.created) == 1


@pytest.mark.asyncio
async def test_dental_active_booking_full_payload_beats_service_faq_routing():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("Хочу на чистку"))
    full = await processor.process(_message("Дмитро 0987121328 хочу на чистку в четвер о 15"))

    assert start["booking_result"]["status"] == "waiting_for_time"
    assert full["intent"] == "booking_flow"
    assert full["booking_result"]["status"] == "confirmed"
    assert full["booking_result"]["customer_name"] == "Дмитро"
    assert full["booking_result"]["contact_phone"] == "0987121328"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 15)


@pytest.mark.asyncio
async def test_dental_active_booking_genuine_faq_still_answers_and_preserves_state():
    processor, _calendar = _build_dental_processor()

    await processor.process(_message("Хочу на чистку"))
    faq = await processor.process(_message("а скільки коштує чистка?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert faq["intent"] == "booking_grounded_question"
    assert "1800 грн" in faq["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_cleaning"


@pytest.mark.asyncio
async def test_dental_generic_booking_requires_service_before_calendar_create():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    start = await processor.process(_message("Хочу записатись"))
    time = await processor.process(_message("у четвер о 15"))
    service = await processor.process(_message("на чистку"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert start["booking_result"]["status"] == "waiting_for_service"
    assert time["booking_result"]["status"] == "waiting_for_service"
    assert service["booking_result"]["status"] == "waiting_for_contact"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["start_dt"] == "2026-08-27T15:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_waiting_for_time_bare_time_asks_only_for_date_then_continues():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    time_only = await processor.process(_message("о 15"))
    dated = await processor.process(_message("у четвер"))

    assert time_only["booking_result"]["status"] == "waiting_for_date"
    assert "на який день" in time_only["reply_text"].lower()
    assert "котру годину" not in time_only["reply_text"].lower()
    assert dated["booking_result"]["status"] == "waiting_for_contact"
    assert dated["booking_result"]["start_dt"] == "2026-08-27T15:00:00+03:00"
    assert calendar.checked == [{"start_dt": _kyiv_dt(2026, 8, 27, 15), "duration_minutes": 30}]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_waiting_for_time_bare_time_window_asks_only_for_date_then_continues():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    window_only = await processor.process(_message("після 15"))
    dated = await processor.process(_message("у четвер"))

    assert window_only["booking_result"]["status"] == "waiting_for_date"
    assert "на який день" in window_only["reply_text"].lower()
    assert "котру годину" not in window_only["reply_text"].lower()
    assert dated["booking_result"]["status"] == "time_window_slots_suggested"
    assert "15:00" in dated["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_waiting_for_time_bare_daypart_asks_only_for_date_then_continues():
    calendar = RecordingConfiguredCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    daypart_only = await processor.process(_message("ввечері"))
    dated = await processor.process(_message("у четвер"))

    assert daypart_only["booking_result"]["status"] == "waiting_for_date"
    assert "на який день" in daypart_only["reply_text"].lower()
    assert "котру годину" not in daypart_only["reply_text"].lower()
    assert dated["booking_result"]["status"] == "time_window_slots_suggested"
    assert "17:00" in dated["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_generic_booking_never_creates_without_service_after_contact():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу записатись"))
    await processor.process(_message("у четвер о 15"))
    contact = await processor.process(_message("Дмитро 0987121328"))

    assert contact["booking_result"]["status"] == "waiting_for_service"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_early_contact_cancellation_clears_pending_without_event():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер, Дмитро, 0987121328"))
    cancelled = await processor.process(_message("скасуй запис"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert cancelled["booking_result"]["status"] == "cancelled"
    assert cancelled["booking_result"]["booking_state"] == "NONE"
    assert pending is None
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["о 14 не можу", "не о 14"])
async def test_dental_early_contact_rejected_time_does_not_book_rejected_time(text):
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("Хочу на чистку у четвер, Дмитро, 0987121328"))
    rejected = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert rejected["booking_result"]["status"] in {
        "slot_suggested",
        "waiting_for_time",
    }
    assert pending is not None
    assert pending["customer_name"] == "Дмитро"
    assert pending["contact_phone"] == "0987121328"
    assert calendar.created == []
    assert all(
        created["start_dt"] != _kyiv_dt(2026, 8, 27, 14)
        for created in calendar.created
    )


@pytest.mark.asyncio
async def test_dental_combined_fresh_rejected_time_never_selects_rejected_time():
    calendar = UniqueEventConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("Хочу на чистку у четвер, о 14 не можу"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert result["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-27"
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["customer_name"] is None
    assert pending["contact_phone"] is None
    assert "start_dt" not in pending
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["скасуй", "скасуй запис", "не треба", "не хочу записуватись", "я передумав"],
)
async def test_dental_waiting_for_contact_clear_cancellation_clears_pending(text):
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "cancelled"
    assert pending is None
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["скасуй", "скасуй запис", "не треба", "не хочу записуватись", "я передумав"],
)
async def test_dental_waiting_for_time_clear_cancellation_clears_pending(text):
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 14)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу записатись на чистку"))
    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert result["booking_result"]["status"] == "cancelled"
    assert pending is None
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "ні 16",
        "ні, краще о 16",
        "краще 16",
        "тоді 16",
        "а 16?",
        "ні чистку, краще консультацію",
    ],
)
async def test_dental_waiting_for_contact_corrections_are_not_cancellation(text):
    calendar = SelectiveConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 16)]
    )
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert pending is not None
    assert pending["state"] in {"WAITING_FOR_TIME", "WAITING_FOR_CONTACT"}


@pytest.mark.asyncio
async def test_dental_faq_interruption_at_waiting_for_contact_then_correction():
    calendar = SelectiveConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 27, 14), _kyiv_dt(2026, 8, 27, 16)]
    )
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку у четвер о 14"))
    faq = await processor.process(_message("скільки коштує відбілювання?"))
    corrected = await processor.process(_message("тоді 16"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert faq["intent"] == "booking_grounded_question"
    assert corrected["booking_result"]["status"] == "waiting_for_contact"
    assert corrected["booking_result"]["start_dt"] == "2026-08-27T16:00:00+03:00"
    assert pending["current_service_id"] == "dental_cleaning"


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
    assert "4500 грн" in whitening["reply_text"]
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
@pytest.mark.parametrize(
    "text",
    [
        "опухла щока",
        "опухла десна",
        "сильно опухло",
        "набрякла щока",
        "температура і опухла щока",
        "вибили зуб",
        "вибив зуб",
        "вибито зуб",
        "сильна кровотеча",
        "кров не зупиняється",
    ],
)
async def test_dental_emergency_inflections_use_safe_escalation(text):
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["routing_category"] == "safe_handoff"
    assert result["intent"] == "medical_emergency"
    assert "екстреної медичної допомоги" in result["reply_text"]
    assert "чаті не можу безпечно оцінити" in result["reply_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["опух питання по ціні", "набряклий графік", "зубна щітка"])
async def test_dental_benign_words_do_not_trigger_emergency(text):
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] != "medical_emergency"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["болить зуб", "що випити?", "який антибіотик?", "це карієс?"],
)
async def test_dental_medical_safety_no_diagnosis_or_prescription(text):
    processor, _calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    reply = result["reply_text"].lower()

    assert "антибіотик амоксицилін" not in reply
    assert "випийте" not in reply
    assert "приймайте" not in reply
    assert "це карієс" not in reply
    assert "не можу поставити діагноз" in reply or "огляд" in reply or "уточніть" in reply


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
    assert "адміністратор зв'язався" not in instructions.lower()
    assert "залишити ім'я та номер телефону" not in instructions.lower()
    assert "callback" not in instructions.lower()
    assert "старт від 200" not in instructions.lower()


async def _processor_waiting_for_contact():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15),
        _kyiv_dt(2026, 8, 27, 16),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    await processor.process(_message("Хочу на чистку у четвер о 15"))
    return processor, calendar


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, expected_name, expected_phone",
    [
        ("Дмитро", "Дмитро", None),
        ("Дмитро 0987121328", "Дмитро", "0987121328"),
        ("Дмитро, 0987121328", "Дмитро", "0987121328"),
        ("0987121328 Дмитро", "Дмитро", "0987121328"),
        ("Мене звати Дмитро", "Дмитро", None),
        ("Я Дмитро", "Дмитро", None),
        ("Олена", "Олена", None),
        ("Іван", "Іван", None),
        ("Анна", "Анна", None),
        ("Марія", "Марія", None),
        ("Олександр", "Олександр", None),
        ("Юлія", "Юлія", None),
        ("Андрій", "Андрій", None),
        ("Дмитро Ішлер", "Дмитро Ішлер", None),
    ],
)
async def test_dental_waiting_for_contact_accepts_only_credible_name_inputs(
    text,
    expected_name,
    expected_phone,
):
    processor, calendar = await _processor_waiting_for_contact()

    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert (result["booking_result"].get("customer_name") or pending.get("customer_name")) == expected_name
    assert (result["booking_result"].get("contact_phone") or pending.get("contact_phone")) == expected_phone
    assert len(calendar.created) == (1 if expected_phone else 0)


@pytest.mark.asyncio
async def test_dental_waiting_for_contact_phone_only_asks_for_name_without_create():
    processor, calendar = await _processor_waiting_for_contact()

    result = await processor.process(_message("0987121328"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "waiting_for_name"
    assert pending["contact_phone"] == "0987121328"
    assert pending["customer_name"] is None
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "о 14 не можу",
        "14 не підходить",
        "о 14 зайнятий",
        "будь ласка",
        "якщо можна",
        "не можу",
        "не виходить",
        "зайнятий",
        "давайте 16",
        "о 16",
        "16",
        "після 15",
        "зранку",
        "увечері",
        "так",
        "ні",
        "добре",
        "ок",
        "дякую",
        "а які є години?",
        "скільки коштує?",
        "де ви знаходитесь?",
        "хочу на чистку",
        "запишіть мене",
        "можна інший час?",
    ],
)
async def test_dental_waiting_for_contact_never_stores_booking_prose_as_name(text):
    processor, calendar = await _processor_waiting_for_contact()

    await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert pending.get("customer_name") is None
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, expected_date, expected_start_dt, expected_name, expected_phone",
    [
        ("Хочу записатись", None, None, None, None),
        ("Хочу записатись у четвер", "2026-08-27", None, None, None),
        ("Хочу записатись о 15", None, None, None, None),
        ("Хочу записатись у четвер о 15", "2026-08-27", "2026-08-27T15:00:00+03:00", None, None),
        (
            "Хочу записатись у четвер о 15, Дмитро, 0987121328",
            "2026-08-27",
            "2026-08-27T15:00:00+03:00",
            "Дмитро",
            "0987121328",
        ),
    ],
)
async def test_dental_unknown_service_preserves_context_without_calendar_check(
    text,
    expected_date,
    expected_start_dt,
    expected_name,
    expected_phone,
):
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "waiting_for_service"
    assert pending.get("current_service_id") is None
    assert pending.get("requested_date") == expected_date
    assert pending.get("start_dt") == expected_start_dt
    assert pending.get("customer_name") == expected_name
    assert pending.get("contact_phone") == expected_phone
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_service_answer_reuses_preserved_full_context_before_create():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    first = await processor.process(_message("Хочу записатись у четвер о 15, Дмитро, 0987121328"))
    second = await processor.process(_message("на чистку"))

    assert first["booking_result"]["status"] == "waiting_for_service"
    assert calendar.checked == [{"start_dt": _kyiv_dt(2026, 8, 27, 15), "duration_minutes": 30}]
    assert second["booking_result"]["status"] == "confirmed"
    assert second["booking_result"]["customer_name"] == "Дмитро"
    assert second["booking_result"]["contact_phone"] == "0987121328"
    assert len(calendar.created) == 1
    assert calendar.created[0]["start_dt"] == _kyiv_dt(2026, 8, 27, 15)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "хочу полікувати зуби",
        "треба до стоматолога",
        "мені треба запис",
        "хочу на прийом",
        "потрібен лікар",
    ],
)
async def test_dental_ambiguous_service_phrases_do_not_reach_calendar(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message(text))

    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_filling_anesthesia_referent_and_clarification_use_last_service():
    processor, calendar = _build_dental_processor()

    price = await processor.process(_message("скільки коштує пломбаа"))
    anesthesia = await processor.process(_message("це з анестезією?"))
    clarified = await processor.process(_message("вставлення пломби"))

    assert "2200 грн" in price["reply_text"]
    assert "Лікування карієсу" in anesthesia["reply_text"]
    assert "анестез" in anesthesia["reply_text"]
    assert "Лікування карієсу" in clarified["reply_text"]
    assert "анестез" in clarified["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_greeting_with_substantive_questions_does_not_consume_intent():
    processor, calendar = _build_dental_processor()

    laser = await processor.process(_message("доброго дня, чи робите ви лазерні процедури?"))
    price = await processor.process(_message("Привіт, скільки коштує чистка?"))

    assert "адміністратор" in laser["reply_text"].lower()
    assert "1800 грн" in price["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_first_turn_greeting_location_question_prepends_matching_greeting():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Добрий вечір, де ви знаходитесь?"))

    assert result["reply_text"].startswith("Добрий вечір! ")
    assert result["reply_text"] == (
        "Добрий вечір! Ми знаходимося в Києві: Печерськ, вул. Липська, 12, "
        "2 поверх. Найближче метро: Арсенальна."
    )
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_first_turn_greeting_booking_intent_prepends_matching_greeting():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Доброго дня, хочу полікувати карієс"))

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert result["reply_text"] == (
        "Добрий день! Супер, тоді можемо підібрати візит. "
        "Напишіть, будь ласка, який день і приблизний час вам зручний?"
    )
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_first_turn_location_question_without_greeting_is_unchanged():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("де ви знаходитесь?"))

    assert result["reply_text"] == (
        "Ми знаходимося в Києві: Печерськ, вул. Липська, 12, 2 поверх. "
        "Найближче метро: Арсенальна."
    )
    assert not result["reply_text"].startswith("Добрий")
    assert not result["reply_text"].startswith("Привіт!")
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_sender_with_previous_assistant_history_greeting_still_gets_prefix():
    processor, calendar = _build_dental_processor()
    processor.memory_service.add_user_message("patient-1", "попереднє повідомлення")
    processor.memory_service.add_assistant_message("patient-1", "попередня відповідь")

    result = await processor.process(_message("Добрий вечір, де ви знаходитесь?"))

    assert result["reply_text"] == (
        "Добрий вечір! Ми знаходимося в Києві: Печерськ, вул. Липська, 12, "
        "2 поверх. Найближче метро: Арсенальна."
    )
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_repeated_greeting_later_in_conversation_is_acknowledged_again():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("де ви знаходитесь?"))
    result = await processor.process(_message("Добрий вечір, скільки коштує чистка?"))

    assert result["reply_text"] == (
        "Добрий вечір! Професійна гігієна коштує від 1800 грн. "
        "Точна вартість залежить від обсягу роботи після огляду."
    )
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_greeting_only_first_turn_keeps_existing_reply_without_duplication():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Привіт"))

    assert result["reply_text"] == "Вітаю! Smile Dental Clinic. Чим можемо допомогти?"
    assert not result["reply_text"].startswith("Привіт! Вітаю!")
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_greeting_with_booking_request_enters_booking_service():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Добрий день, хочу записатися"))

    assert result["booking_result"]["status"] == "waiting_for_service"
    assert result["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_holiday_hours_use_safe_unknown_boundary():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("ви працюєте на різдво?"))

    assert "Святковий" in result["reply_text"]
    assert "потрібно уточнити" in result["reply_text"]
    assert "Пн-Пт" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_directions_answer_grounded_location_without_origin_prompt():
    processor, calendar = _build_dental_processor()

    start = await processor.process(_message("як до вас добратися?"))
    exit_reply = await processor.process(_message("скільки коштує чистка?"))

    assert "Звідки" not in start["reply_text"]
    assert "вул. Липська, 12" in start["reply_text"]
    assert "навігатор" in start["reply_text"]
    assert ".." not in start["reply_text"]
    assert "1800 грн" in exit_reply["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["ввечері", "увечері", "десь ввечері"])
async def test_dental_pending_date_merges_evening_daypart(phrase):
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 17),
        _kyiv_dt(2026, 8, 27, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у четвер"))
    result = await processor.process(_message(phrase))

    assert result["booking_result"]["status"] == "daypart_slots_suggested"
    assert {slot["start_dt"] for slot in result["booking_result"]["suggested_slots"]} == {
        "2026-08-27T17:00:00+03:00",
        "2026-08-27T18:00:00+03:00",
    }
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_saturday_evening_refinement_explains_working_hours():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 29, 10),
        _kyiv_dt(2026, 8, 29, 10, 30),
        _kyiv_dt(2026, 8, 29, 11),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у суботу"))
    offered = await processor.process(_message("коли є час?"))
    evening = await processor.process(_message("А ввечері?"))

    assert "10:00, 10:30 або 11:00" in offered["reply_text"]
    assert evening["booking_result"]["status"] == "outside_business_hours"
    assert "суботу" in evening["reply_text"]
    assert "з 10:00 до 16:00" in evening["reply_text"]
    assert "Вечірніх слотів цього дня, на жаль, немає" in evening["reply_text"]
    assert "Найпізніший вільний час цього дня бачу о 11:00" in evening["reply_text"]
    assert "Підійде, чи краще подивитися інший день?" in evening["reply_text"]
    assert "На цей час клініка не працює, тому ввечері не підходить" not in evening["reply_text"]
    assert evening["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-29T11:00:00+03:00"}
    ]
    accepted = await processor.process(_message("підходить"))
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert "11:00" in accepted["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_saturday_evening_latest_slot_rejection_keeps_booking_open():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 29, 10),
        _kyiv_dt(2026, 8, 29, 10, 30),
        _kyiv_dt(2026, 8, 29, 11),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у суботу"))
    await processor.process(_message("коли є час?"))
    evening = await processor.process(_message("А ввечері?"))
    rejected = await processor.process(_message("ні, не підходить"))

    assert "Вечірніх слотів цього дня, на жаль, немає" in evening["reply_text"]
    assert rejected["booking_result"]["status"] == "alternative_rejected"
    assert "інший зручний час або день" in rejected["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_saturday_after_15_refinement_explains_hours_when_no_slots():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 29, 10),
        _kyiv_dt(2026, 8, 29, 10, 30),
        _kyiv_dt(2026, 8, 29, 11),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у суботу"))
    await processor.process(_message("коли є час?"))
    result = await processor.process(_message("З 15 немає слотів?"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["suggested_slots"] == []
    assert "після 15:00 не бачу вільних слотів" in result["reply_text"]
    assert "з 10:00 до 16:00" in result["reply_text"]
    assert "на котру годину" not in result["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_one_turn_date_evening_refinement_checks_evening_only():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 12),
        _kyiv_dt(2026, 8, 27, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message("А щось у четвер ввечері?"))

    assert result["booking_result"]["status"] == "daypart_slots_suggested"
    assert result["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T18:00:00+03:00"}
    ]
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    ["пізніше?", "а пізніше?", "є щось пізніше?"],
)
async def test_dental_availability_refinement_later_uses_same_day_context(phrase):
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 15),
        _kyiv_dt(2026, 8, 27, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    offered = await processor.process(_message("у четвер після 14"))
    refined = await processor.process(_message(phrase))

    assert offered["booking_result"]["status"] == "time_window_slots_suggested"
    assert refined["booking_result"]["status"] == "time_window_slots_suggested"
    assert "після 18:00" in refined["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_availability_refinement_earlier_uses_same_day_context():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 12),
        _kyiv_dt(2026, 8, 27, 15),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у четвер після 14"))
    refined = await processor.process(_message("раніше?"))

    assert refined["booking_result"]["status"] == "time_window_slots_suggested"
    assert refined["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T12:00:00+03:00"}
    ]
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, expected_slots",
    [
        (
            "у четвер після 17",
            [{"day_key": "selected_day", "start_dt": "2026-08-27T18:00:00+03:00"}],
        ),
        (
            "у четвер до 15",
            [{"day_key": "selected_day", "start_dt": "2026-08-27T12:00:00+03:00"}],
        ),
    ],
)
async def test_dental_availability_time_window_refinements_after_before(text, expected_slots):
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 12),
        _kyiv_dt(2026, 8, 27, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message(text))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["booking_result"]["suggested_slots"] == expected_slots
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["післязавтра 16", "після завтра 16", "післязаавтра 16"],
)
async def test_dental_day_after_tomorrow_natural_forms_in_pending_booking(text):
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 24, 16)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message(text))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-08-24T16:00:00+03:00"
    assert len(calendar.checked) == 1
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_ukrainian_ob_marker_behaves_like_o_marker():
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 27, 15)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    result = await processor.process(_message("хочу на чистку у четвер об 15"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["start_dt"] == "2026-08-27T15:00:00+03:00"
    assert calendar.checked == [{"start_dt": _kyiv_dt(2026, 8, 27, 15), "duration_minutes": 30}]
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["так", "так, підходить", "підходить", "добре", "давайте"])
async def test_dental_pending_contact_affirmations_do_not_lose_booking_state(text):
    processor, calendar = await _processor_waiting_for_contact()

    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["booking_state"] == "WAITING_FOR_CONTACT"
    assert pending["start_dt"] == "2026-08-27T15:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_consultation_context_zapysui_continues_booking_service():
    processor, calendar = _build_dental_processor()

    service = await processor.process(_message("огляд"))
    booking = await processor.process(_message("записуй"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert "Консультація стоматолога" in service["reply_text"]
    assert booking["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["огляд", "огляд стоматолога", "стоматологічний огляд"])
async def test_dental_bounded_oglyad_resolves_consultation(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert "Консультація стоматолога" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_oglyad_price_question_resolves_consultation_price():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("скільки у вас коштує огляд?"))

    assert "700 грн" in result["reply_text"]
    assert "Консультація стоматолога" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["огляд машини", "технічний огляд", "огляд квартири", "огляд документів"],
)
async def test_dental_oglyad_unrelated_objects_do_not_resolve_consultation(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert "Консультація стоматолога" not in result["reply_text"]
    assert "700 грн" not in result["reply_text"]
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_active_booking_koly_means_availability_not_hours():
    processor, calendar = _build_dental_processor(
        calendar_service=SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 24, 16)])
    )

    service = await processor.process(_message("огляд"))
    booking = await processor.process(_message("добре"))
    availability = await processor.process(_message("коли?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert "Консультація стоматолога" in service["reply_text"]
    assert booking["booking_result"]["status"] == "waiting_for_time"
    assert availability["intent"] == "booking_availability_question"
    assert "Працюємо Пн-Пт" not in availability["reply_text"]
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_active_booking_tomorrow_can_offers_verified_slots(monkeypatch):
    class FridayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 28, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", FridayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor(
        calendar_service=SelectiveConfiguredCalendarService([
            _kyiv_dt(2026, 8, 29, 10),
            _kyiv_dt(2026, 8, 29, 10, 30),
            _kyiv_dt(2026, 8, 29, 11),
        ])
    )

    await processor.process(_message("огляд"))
    await processor.process(_message("Ага"))
    availability = await processor.process(_message("А завтра можна?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert availability["intent"] == "booking_availability_question"
    assert availability["booking_result"]["status"] == "time_window_slots_suggested"
    assert "10:00" in availability["reply_text"]
    assert "10:30" in availability["reply_text"]
    assert "11:00" in availability["reply_text"]
    assert pending["current_service_id"] == "dental_consultation"
    assert pending["requested_date"] == "2026-08-29"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_missing_service_short_oglyad_then_after_work_offers_slots(monkeypatch):
    class WednesdayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 26, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", WednesdayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor(
        calendar_service=SelectiveConfiguredCalendarService([
            _kyiv_dt(2026, 8, 27, 17, 30),
            _kyiv_dt(2026, 8, 27, 18, 30),
        ])
    )

    await processor.process(_message("Привіт, хочу записатись до стоматолога"))
    service = await processor.process(_message("Та просто огляд"))
    availability = await processor.process(_message("А завтра після роботи є щось?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert service["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "dental_consultation"
    assert availability["booking_result"]["status"] == "daypart_slots_suggested"
    assert "17:30" in availability["reply_text"]
    assert "18:30" in availability["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_active_booking_later_no_slots_does_not_cancel(monkeypatch):
    class FridayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 28, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", FridayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor(
        calendar_service=SelectiveConfiguredCalendarService([
            _kyiv_dt(2026, 8, 29, 10),
            _kyiv_dt(2026, 8, 29, 10, 30),
            _kyiv_dt(2026, 8, 29, 11),
        ])
    )

    await processor.process(_message("огляд"))
    await processor.process(_message("Ага"))
    await processor.process(_message("А завтра можна?"))
    later = await processor.process(_message("А пізніше нема?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert later["booking_result"]["status"] == "time_window_slots_suggested"
    assert "не бачу вільних слотів" in later["reply_text"]
    assert "суботу" in later["reply_text"]
    assert "10:00 до 16:00" in later["reply_text"]
    assert "не бронюю" not in later["reply_text"].lower()
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_price_context_when_can_starts_availability_for_service():
    processor, calendar = _build_dental_processor(
        calendar_service=SelectiveConfiguredCalendarService([
            _kyiv_dt(2026, 8, 24, 14),
            _kyiv_dt(2026, 8, 24, 17, 30),
        ])
    )

    await processor.process(_message("Скільки чистка?"))
    await processor.process(_message("А чого від?"))
    await processor.process(_message("Ясно"))
    availability = await processor.process(_message("Ну добре коли можна?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert availability["intent"] == "booking_availability_question"
    assert availability["booking_result"]["status"] == "availability_suggested"
    assert "Напишіть, будь ласка, який день" not in availability["reply_text"]
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_short_ack_after_price_does_not_fallback():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки чистка?"))
    result = await processor.process(_message("Ясно"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert result["reply_text"] == "Добре."
    assert "Хочу правильно зорієнтувати" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_numeric_time_after_ukrainian_context_keeps_ukrainian_hours_reply(monkeypatch):
    class FridayEveningDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 28, 19, 20, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", FridayEveningDentalSmokeDatetime)
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Привіт, хочу записатись до стоматолога"))
    await processor.process(_message("Та просто огляд"))
    await processor.process(_message("А завтра після роботи є щось?"))
    result = await processor.process(_message("18:30"))

    assert result["booking_result"]["status"] == "outside_business_hours"
    assert "The clinic works" not in result["reply_text"]
    assert "on tomorrow" not in result["reply_text"]
    assert "Завтра, у суботу, клініка працює з 10:00 до 16:00." in result["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_short_when_and_second_selects_offered_slot(monkeypatch):
    class WednesdayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 26, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", WednesdayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor(
        calendar_service=SelectiveConfiguredCalendarService([
            _kyiv_dt(2026, 8, 27, 12),
            _kyiv_dt(2026, 8, 27, 15),
        ])
    )

    await processor.process(_message("Скільки консультація?"))
    await processor.process(_message("ок"))
    availability = await processor.process(_message("а коли"))
    selected = await processor.process(_message("друге"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert availability["intent"] == "booking_availability_question"
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert pending["start_dt"] == "2026-08-27T15:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_nearest_single_slot_allows_explicit_later_time_on_same_day(monkeypatch):
    class MondayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", MondayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor(
        calendar_service=SelectiveConfiguredCalendarService([
            _kyiv_dt(2026, 8, 24, 16, 30),
            _kyiv_dt(2026, 8, 24, 18),
        ])
    )

    await processor.process(_message("Добрий день, зуб дуже болить другий день"))
    await processor.process(_message("вже постійний"))
    nearest = await processor.process(_message("давайте"))
    later_time = await processor.process(_message("18"))

    assert nearest["booking_result"]["status"] == "nearest_availability_suggested"
    assert later_time["booking_result"]["status"] == "waiting_for_contact"
    assert later_time["booking_result"]["start_dt"] == "2026-08-24T18:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_selected_slot_date_switch_shows_new_day_slots(monkeypatch):
    class WednesdayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 26, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", WednesdayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor(
        calendar_service=SelectiveConfiguredCalendarService([
            _kyiv_dt(2026, 8, 27, 17),
            _kyiv_dt(2026, 8, 28, 10, 30),
            _kyiv_dt(2026, 8, 28, 15),
            _kyiv_dt(2026, 8, 28, 18),
        ])
    )

    await processor.process(_message("Хочу записатись на огляд завтра"))
    await processor.process(_message("17"))
    friday = await processor.process(_message("Стоп а в п’ятницю що є?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert friday["booking_result"]["status"] == "time_window_slots_suggested"
    assert "10:30" in friday["reply_text"]
    assert "15:00" in friday["reply_text"]
    assert "18:00" in friday["reply_text"]
    assert "залиште" not in friday["reply_text"].lower()
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["requested_date"] == "2026-08-28"
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_invalid_ordinal_after_single_slot_restates_offer_without_reset(monkeypatch):
    class WednesdayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 26, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", WednesdayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor(
        calendar_service=SelectiveConfiguredCalendarService([
            _kyiv_dt(2026, 8, 27, 12),
        ])
    )

    await processor.process(_message("Скільки консультація?"))
    await processor.process(_message("ок"))
    await processor.process(_message("а коли"))
    second = await processor.process(_message("друге"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert second["booking_result"]["status"] == "availability_time_not_offered"
    assert "12:00" in second["reply_text"]
    assert "який день і приблизний час" not in second["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_active_booking_explicit_hours_faq_preserves_state():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("огляд"))
    await processor.process(_message("добре"))
    hours = await processor.process(_message("який у вас графік?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert "Пн-Пт" in hours["reply_text"] or "09:00-19:00" in hours["reply_text"]
    assert pending["current_service_id"] == "dental_consultation"
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_closed_day_evening_preserves_active_booking_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    closed = await processor.process(_message("завтра ввечері"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert closed["booking_result"]["status"] == "outside_business_hours"
    assert closed["booking_result"]["booking_state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_cleaning"
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_active_booking_reaffirmation_keeps_requested_date():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("на понеділок"))
    result = await processor.process(_message("хочу тоді записатись"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "Так, звісно" in result["reply_text"]
    assert "На понеділок вже підбираємо час" in result["reply_text"]
    assert "на котру годину" in result["reply_text"]
    assert pending["requested_day_label"] == "понеділок"
    assert pending["current_service_id"] == "dental_cleaning"
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_closed_day_reply_does_not_repeat_closed_message():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message("завтра можна?"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert result["reply_text"].lower().count("клініка не працює") == 1
    assert "У цей час клініка не працює" not in result["reply_text"]
    assert "Найближчий робочий день" in result["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert calendar.checked
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_today_past_time_is_not_checked_or_booked(monkeypatch):
    class LateFridayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = _REAL_DATETIME(2026, 8, 28, 18, 55, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", LateFridayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на огляд"))
    await processor.process(_message("можна сьогодні?"))
    result = await processor.process(_message("на 18"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "past_time"
    assert "сьогодні вже не встигнемо" in result["reply_text"]
    assert pending["current_service_id"] == "dental_consultation"
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_pending_contact_rejects_slot_that_became_past(monkeypatch):
    class MovingFridayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        current = _REAL_DATETIME(2026, 8, 28, 17, 50, tzinfo=ZoneInfo("Europe/Kyiv"))

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", MovingFridayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor()

    selected = await processor.process(_message("хочу на чистку сьогодні о 18"))
    MovingFridayDentalSmokeDatetime.current = _REAL_DATETIME(
        2026,
        8,
        28,
        18,
        1,
        tzinfo=ZoneInfo("Europe/Kyiv"),
    )
    result = await processor.process(_message("Дмитро 0987121329"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert result["booking_result"]["status"] == "past_time"
    assert "сьогодні вже не встигнемо" in result["reply_text"]
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["customer_name"] == "Дмитро"
    assert pending["contact_phone"] == "0987121329"
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert len(calendar.checked) == 1
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_selected_slot_reconsideration_allows_date_and_window_refinement():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 16),
        _kyiv_dt(2026, 8, 25, 16, 30),
        _kyiv_dt(2026, 8, 25, 17),
        _kyiv_dt(2026, 8, 27, 17),
        _kyiv_dt(2026, 8, 27, 17, 30),
        _kyiv_dt(2026, 8, 27, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на огляд"))
    tuesday = await processor.process(_message("у вівторок після 16"))
    selected = await processor.process(_message("17:00 нормально"))
    reconsidered = await processor.process(_message("стоп, я переплутав"))
    thursday = await processor.process(_message("мені краще в четвер"))
    refined = await processor.process(_message("після 16 теж"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert tuesday["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-25T16:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-25T16:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-25T17:00:00+03:00"},
    ]
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert reconsidered["booking_result"]["status"] == "waiting_for_time"
    assert "інший час" in reconsidered["reply_text"].lower()
    assert thursday["booking_result"]["status"] == "waiting_for_time"
    assert thursday["booking_result"]["requested_date"] == "2026-08-27"
    assert refined["booking_result"]["status"] == "time_window_slots_suggested"
    assert refined["booking_result"]["suggested_slots"] == [
        {"day_key": "selected_day", "start_dt": "2026-08-27T17:00:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T17:30:00+03:00"},
        {"day_key": "selected_day", "start_dt": "2026-08-27T18:00:00+03:00"},
    ]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_active_booking_next_week_asks_for_specific_day():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатися на професійну чистку"))
    result = await processor.process(_message("можна наступного тижня?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "waiting_for_next_week_day"
    assert "наступний тиждень" in result["reply_text"]
    assert "який день наступного тижня" in result["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_cleaning"
    assert "requested_date" not in pending
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_next_week_any_day_offers_first_available_working_day():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 24, 10),
        _kyiv_dt(2026, 8, 24, 10, 30),
        _kyiv_dt(2026, 8, 24, 11),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу записатися на професійну чистку"))
    await processor.process(_message("коли є час наступного тижня?"))
    result = await processor.process(_message("Будь який"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "найближчі варіанти наступного тижня" in result["reply_text"]
    assert "У понеділок вільний час" in result["reply_text"]
    assert "10:00" in result["reply_text"]
    assert pending["requested_date"] == "2026-08-24"
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.checked
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_greeting_without_active_offer_returns_clean_greeting():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатися на професійну чистку"))
    result = await processor.process(_message("Привіт"))

    assert result["booking_result"] is None
    assert "Вітаю! Smile Dental Clinic. Чим можемо допомогти?" in result["reply_text"]
    assert "Для візит" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_absolute_date_reply_uses_natural_ukrainian_label():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 9, 3, 10),
        _kyiv_dt(2026, 9, 3, 10, 30),
        _kyiv_dt(2026, 9, 3, 11),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на огляд"))
    result = await processor.process(_message("можна 3 вересня?"))

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "3-го вересня вільний час" in result["reply_text"]
    assert "У 3 вересня" not in result["reply_text"]
    assert "У 3-го вересня" not in result["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_exact_closing_time_explains_visit_cannot_finish_before_close():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатись на консультацію"))
    result = await processor.process(_message("у середу о 19:00 можна?"))

    assert result["booking_result"]["status"] == "outside_business_hours"
    assert "клініка працює з 09:00 до 19:00" in result["reply_text"]
    assert "Візит на 19:00 вже не встигаємо провести до закриття" in result["reply_text"]
    assert "У цей час клініка не працює" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_stop_without_faq_reopens_time_selection_not_cancellation():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 26, 15),
        _kyiv_dt(2026, 8, 25, 10),
        _kyiv_dt(2026, 8, 25, 10, 30),
        _kyiv_dt(2026, 8, 25, 11),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("треба записатись до стоматолога"))
    await processor.process(_message("огляд"))
    selected = await processor.process(_message("на середу о 15"))
    stopped = await processor.process(_message("а ні стоп"))
    unavailable_day = await processor.process(_message("післязавтра не зможу"))
    tuesday = await processor.process(_message("чи є час на вівторок?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert stopped["booking_result"]["status"] == "waiting_for_time"
    assert "скасувати запис" not in stopped["reply_text"].lower()
    assert unavailable_day["booking_result"]["status"] == "waiting_for_time"
    assert "не ставлю цей день" in unavailable_day["reply_text"]
    assert tuesday["booking_result"]["status"] == "time_window_slots_suggested"
    assert "10:00, 10:30 або 11:00" in tuesday["reply_text"]
    assert pending["state"] == "WAITING_FOR_TIME"
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_latest_morning_followup_answers_last_current_slot():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 25, 10),
        _kyiv_dt(2026, 8, 25, 10, 30),
        _kyiv_dt(2026, 8, 25, 11),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на огляд"))
    morning = await processor.process(_message("у вівторок зранку"))
    latest = await processor.process(_message("а найпізніше зранку це коли?"))
    selected = await processor.process(_message("давайте тоді 10:30"))

    assert morning["booking_result"]["status"] == "daypart_slots_suggested"
    assert latest["booking_result"]["status"] == "latest_offered_slot_answer"
    assert "Найпізніше зранку у вівторок бачу о 11:00" in latest["reply_text"]
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-25T10:30:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_latest_slot_reference_can_be_accepted_with_pronoun():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 24, 17),
        _kyiv_dt(2026, 8, 24, 17, 30),
        _kyiv_dt(2026, 8, 24, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на огляд"))
    offered = await processor.process(_message("післязавтра після 17"))
    latest = await processor.process(_message("який найпізніший?"))
    accepted = await processor.process(_message("давайте його"))

    assert offered["booking_result"]["status"] == "time_window_slots_suggested"
    assert "17:00, 17:30 або 18:00" in offered["reply_text"]
    assert latest["booking_result"]["status"] == "latest_offered_slot_answer"
    assert "18:00" in latest["reply_text"]
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert accepted["booking_result"]["start_dt"] == "2026-08-24T18:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_current_offer_question_repeats_current_window_not_full_day():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 26, 12, 30),
        _kyiv_dt(2026, 8, 26, 14),
        _kyiv_dt(2026, 8, 26, 14, 30),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу записатись на чистку"))
    await processor.process(_message("у середу можна?"))
    afternoon = await processor.process(_message("десь після обіду"))
    repeated = await processor.process(_message("що у вас є?"))

    assert afternoon["booking_result"]["status"] == "daypart_slots_suggested"
    assert repeated["booking_result"]["status"] == "current_offer_repeated"
    assert "після обіду" in repeated["reply_text"]
    assert "12:30, 14:00 або 14:30" in repeated["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_first_rejected_then_next_offer_can_be_accepted():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 28, 17),
        _kyiv_dt(2026, 8, 28, 17, 30),
        _kyiv_dt(2026, 8, 28, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("чистка є?"))
    await processor.process(_message("ок"))
    await processor.process(_message("на п'ятницю ввечері"))
    rejected = await processor.process(_message("перший не встигаю"))
    next_slot = await processor.process(_message("наступний?"))
    accepted = await processor.process(_message("норм"))

    assert rejected["booking_result"]["status"] == "offered_slot_rejected"
    assert "17:30 або 18:00" in rejected["reply_text"]
    assert next_slot["booking_result"]["status"] == "next_offered_slot_answer"
    assert "17:30" in next_slot["reply_text"]
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert accepted["booking_result"]["start_dt"] == "2026-08-28T17:30:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_last_slot_selection_phrase_selects_last_current_offer():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 16),
        _kyiv_dt(2026, 8, 27, 16, 30),
        _kyiv_dt(2026, 8, 27, 17),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("потрібна консультація стоматолога"))
    await processor.process(_message("можна у четвер?"))
    await processor.process(_message("після 16"))
    selected = await processor.process(_message("давайте останній"))

    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-27T17:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_latest_tomorrow_question_after_outside_hours_keeps_booking_active(monkeypatch):
    class FridayEveningDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 28, 19, 20, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", FridayEveningDentalSmokeDatetime)
    calendar = SelectiveConfiguredCalendarService([_kyiv_dt(2026, 8, 29, 15, 30)])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на огляд"))
    await processor.process(_message("завтра о 17"))
    latest = await processor.process(_message("А найпізніше завтра коли можна?"))

    assert latest["booking_result"]["status"] == "latest_offered_slot_answer"
    assert "15:30" in latest["reply_text"]
    assert "не бронюю" not in latest["reply_text"].lower()
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_later_question_after_date_suggests_evening_slots_not_cancel():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 27, 17),
        _kyiv_dt(2026, 8, 27, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на огляд"))
    await processor.process(_message("у четвер"))
    later = await processor.process(_message("А пізніше є?"))

    assert later["booking_result"]["status"] == "time_window_slots_suggested"
    assert "17:00" in later["reply_text"]
    assert "18:00" in later["reply_text"]
    assert "не бронюю" not in later["reply_text"].lower()
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_not_earliest_next_narrows_to_second_slot_then_accepts():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 24, 9),
        _kyiv_dt(2026, 8, 24, 10),
        _kyiv_dt(2026, 8, 24, 10, 30),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на огляд"))
    await processor.process(_message("у понеділок зранку"))
    next_slot = await processor.process(_message("Не найраніший, наступний"))
    accepted = await processor.process(_message("Так, цей норм"))

    assert next_slot["booking_result"]["status"] == "next_offered_slot_answer"
    assert "10:00" in next_slot["reply_text"]
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert accepted["booking_result"]["start_dt"] == "2026-08-24T10:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_first_rejected_future_tense_then_next_can_be_selected():
    calendar = SelectiveConfiguredCalendarService([
        _kyiv_dt(2026, 8, 24, 17, 30),
        _kyiv_dt(2026, 8, 24, 18),
    ])
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на огляд"))
    await processor.process(_message("у понеділок ввечері"))
    rejected = await processor.process(_message("перший не встигну"))
    next_slot = await processor.process(_message("а наступний?"))
    accepted = await processor.process(_message("о цей норм"))

    assert rejected["booking_result"]["status"] == "offered_slot_rejected"
    assert "18:00" in rejected["reply_text"]
    assert next_slot["booking_result"]["status"] == "next_offered_slot_answer"
    assert accepted["booking_result"]["status"] == "waiting_for_contact"
    assert accepted["booking_result"]["start_dt"] == "2026-08-24T18:00:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_active_booking_after_work_daypart_beats_contextual_summary():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("брекети ставите?"))
    await processor.process(_message("коли ортодонт може прийняти?"))
    await processor.process(_message("у вівторок"))
    evening = await processor.process(_message("Після роботи краще"))
    selected = await processor.process(_message("другий варіант підійде"))

    assert evening["booking_result"]["status"] == "daypart_slots_suggested"
    assert "17:00" in evening["reply_text"]
    assert "18:00" in evening["reply_text"]
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-25T17:30:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_active_booking_before_lunch_daypart_can_select_ordinal_slot():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує імплант?"))
    await processor.process(_message("добре, давайте запишусь"))
    await processor.process(_message("у середу"))
    morning = await processor.process(_message("До обіду бажано"))
    selected = await processor.process(_message("другий варіант підійде"))

    assert morning["booking_result"]["status"] == "daypart_slots_suggested"
    assert "09:00" in morning["reply_text"]
    assert selected["booking_result"]["status"] == "waiting_for_contact"
    assert selected["booking_result"]["start_dt"] == "2026-08-26T09:30:00+03:00"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_this_weekend_availability_uses_saturday_not_fallback():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("підкажіть елайнери робите?"))
    await processor.process(_message("ок"))
    await processor.process(_message("може тоді прийду спочатку"))
    weekend = await processor.process(_message("на цих вихідних є щось?"))

    assert weekend["booking_result"]["status"] == "time_window_slots_suggested"
    assert "суботу" in weekend["reply_text"]
    assert "Який час вам зручніший?" in weekend["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_nearest_to_current_time_repeats_current_window_not_global_nearest():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("вініри робите?"))
    await processor.process(_message("давайте в середу"))
    offered = await processor.process(_message("Мені десь після 16 зручно"))
    repeated = await processor.process(_message("А що найближче до цього часу є?"))

    assert offered["booking_result"]["status"] == "time_window_slots_suggested"
    assert repeated["booking_result"]["status"] == "current_offer_repeated"
    assert "16:00" in repeated["reply_text"]
    assert "завтра" not in repeated["reply_text"].lower()
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_ambiguous_this_option_repeats_multiple_slots_without_arbitrary_pick():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на консультацію"))
    await processor.process(_message("у середу після обіду"))
    repeated = await processor.process(_message("Давайте цей"))

    assert repeated["booking_result"]["status"] == "current_offer_repeated"
    assert "12:00" in repeated["reply_text"]
    assert "12:30" in repeated["reply_text"]
    assert processor.booking_service.get_booking_state("patient-1").value == "WAITING_FOR_TIME"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_morning_rejection_after_morning_offer_suggests_later_slots():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на консультацію"))
    await processor.process(_message("на наступну п'ятницю можна?"))
    later = await processor.process(_message("Зранку не можу"))

    assert later["booking_result"]["status"] == "time_window_slots_suggested"
    assert "після 10:00" in later["reply_text"]
    assert "10:30" in later["reply_text"]
    assert "09:00" not in later["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_closed_day_carryover_blocks_before_time_after_service_detail(monkeypatch):
    class FridayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 8, 28, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", FridayDentalSmokeDatetime)
    processor, calendar = _build_dental_processor()

    generic = await processor.process(_message("Хочу записатися до стоматолога післязавтра"))
    detail = await processor.process(_message("Треба подивитися зуб, реагує на холодне"))

    assert generic["booking_result"]["status"] == "waiting_for_service"
    assert detail["booking_result"]["status"] == "outside_business_hours"
    assert "клініка не працює" in detail["reply_text"].lower()
    assert "на котру годину" not in detail["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_specialty_consultation_requirement_preserves_braces_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Добрий день, а брекети у вас ставлять?"))
    result = await processor.process(_message("А перед встановленням треба спочатку прийти до ортодонта?"))
    booking = await processor.process(_message("А коли ортодонт може прийняти?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "front_desk_contextual_answer"
    assert "ортодонт" in result["reply_text"].lower() or "консультац" in result["reply_text"].lower()
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert booking["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "orthodontic_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_specialty_consultation_requirement_preserves_implant_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки у вас коштує імплант?"))
    result = await processor.process(_message("Мені спочатку треба на консультацію?"))
    booking = await processor.process(_message("Добре, давайте запишусь"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "front_desk_contextual_answer"
    assert "консультац" in result["reply_text"].lower()
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert booking["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "prosthetics_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_contextual_option_list_question_uses_remembered_braces_options():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Добрий день, а брекети у вас ставлять?"))
    result = await processor.process(_message("А які саме є?"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Металеві брекети" in result["reply_text"]
    assert "Керамічні брекети" in result["reply_text"]
    assert "Хочу правильно зорієнтувати" not in result["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_veneers_interest_phrase_is_not_unknown_variant():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Привіт, хочу зробити вініри, але поки просто цікавлюсь"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Вініри" in result["reply_text"]
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_child_orthodontist_visit_phrase_is_not_unknown_variant():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Добрий день, хочу показати дитину ортодонту"))

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}
    assert pending["current_service_id"] == "orthodontic_consultation"
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_contextual_consultation_booking_phrase_uses_veneers_redirect():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Привіт, хочу зробити вініри, але поки просто цікавлюсь"))
    result = await processor.process(_message("Можна тоді прийти просто проконсультуватися?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "prosthetics_consultation"
    assert "Хочу правильно зорієнтувати" not in result["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_ambiguous_this_does_not_collect_contact_until_specific_slot():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Привіт, хочу зробити зуби білішими"))
    await processor.process(_message("Мені не чистка напевно, а саме відбілювання"))
    await processor.process(_message("Ок"))
    await processor.process(_message("А на наступну п'ятницю можна?"))
    await processor.process(_message("Що є після обіду?"))
    repeated = await processor.process(_message("Давайте цей"))
    name = await processor.process(_message("Дмитро"))
    phone = await processor.process(_message("0987121328"))

    assert repeated["booking_result"]["status"] == "current_offer_repeated"
    assert "який час" in repeated["reply_text"].lower()
    assert name["booking_result"]["status"] == "waiting_for_time"
    assert "який час" in name["reply_text"].lower() or "котру годину" in name["reply_text"].lower()
    assert phone["booking_result"]["status"] == "waiting_for_time"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_through_next_week_request_is_not_next_week():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки у вас коштує імплант?"))
    await processor.process(_message("Добре, давайте запишусь"))
    result = await processor.process(_message("Не на наступному тижні, а через тиждень"))

    assert result["booking_result"]["status"] == "waiting_for_specific_day"
    assert "через тиждень" in result["reply_text"].lower()
    assert "наступний тиждень" not in result["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["добре", "так", "коли?", "пізніше"])
async def test_dental_short_continuations_are_inert_without_context(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["booking_result"] is None
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_weekly_hours_and_address_still_work_after_context_hardening():
    processor, calendar = _build_dental_processor()

    hours = await processor.process(_message("коли ви працюєте?"))
    address = await processor.process(_message("де ви знаходитесь?"))
    greeting = await processor.process(_message("привіт"))

    assert "Пн-Пт" in hours["reply_text"] or "09:00-19:00" in hours["reply_text"]
    assert "Липська, 12" in address["reply_text"]
    assert "Вітаю" in greeting["reply_text"] or "Smile Dental Clinic" in greeting["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


UNKNOWN_SERVICE_DETAIL_CONTAINMENT_MARKER = "у базі немає підтвердження"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "ви робите лазерне відбілювання?",
        "ставите сапфірові брекети?",
        "робите імпланти Straumann?",
    ],
)
async def test_dental_unknown_service_variant_stays_safely_contained(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert UNKNOWN_SERVICE_DETAIL_CONTAINMENT_MARKER in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_supported_implant_question_is_not_wrongly_contained():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("робите імпланти?"))

    assert UNKNOWN_SERVICE_DETAIL_CONTAINMENT_MARKER not in result["reply_text"]
    context = processor.memory_service.get_context("patient-1")
    assert context.get("current_service_id") == "dental_implant"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_closed_day_date_only_offers_next_open_day_slots(monkeypatch):
    class FridayDentalSmokeDatetime(FixedDentalSmokeDatetime):
        @classmethod
        def now(cls, tz=None):
            fixed = _REAL_DATETIME(2026, 8, 28, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(booking_service_module, "datetime", FridayDentalSmokeDatetime)
    calendar = SelectiveConfiguredCalendarService(
        [
            _kyiv_dt(2026, 8, 31, 10),
            _kyiv_dt(2026, 8, 31, 10, 30),
            _kyiv_dt(2026, 8, 31, 11),
        ]
    )
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message("післязавтра можна?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "time_window_slots_suggested"
    assert "Післязавтра, у неділю, клініка не працює" in result["reply_text"]
    assert "Найближчий робочий день — понеділок" in result["reply_text"]
    assert "10:00" in result["reply_text"]
    assert pending["requested_date"] == "2026-08-31"
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.checked
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_price_and_jaw_followup_are_natural():
    processor, calendar = _build_dental_processor()

    availability = await processor.process(_message("ви ставите брекети?"))
    price = await processor.process(_message("А яка вартість?"))
    better = await processor.process(_message("А які кращі?"))
    jaw = await processor.process(_message("це за щелепу чи за дві?"))

    assert "Так, у нас можна пройти ортодонтичне лікування брекетами" in availability["reply_text"]
    assert price["reply_text"].startswith("Вартість брекетів:")
    assert "за щелепу" not in price["reply_text"].lower()
    assert "Дистанційно не можемо сказати" in better["reply_text"]
    assert "підбирає ортодонт після огляду" in better["reply_text"]
    assert "за одну щелепу" in jaw["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_specialty_acknowledgement_prompts_consultation_cta_without_booking():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("ви ставите брекети?"))
    result = await processor.process(_message("Гаразд"))

    assert result["intent"] == "contextual_affirmation"
    assert "записатися на консультацію ортодонта" in result["reply_text"]
    assert result["booking_result"] is None
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_repeated_braces_capability_question_stays_direct_yes_answer():
    processor, calendar = _build_dental_processor()

    first = await processor.process(_message("Ви ставите брекети?"))
    repeated = await processor.process(_message("ви ставите брекети?"))
    yes_or_no = await processor.process(_message("так чи ні"))

    assert "Так, у нас можна пройти ортодонтичне лікування брекетами" in first["reply_text"]
    assert "Так, у нас можна пройти ортодонтичне лікування брекетами" in repeated["reply_text"]
    assert "Вартість брекетів" not in repeated["reply_text"]
    assert "Так" in yes_or_no["reply_text"]
    assert "брекет" in yes_or_no["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_nearest_availability_typo_triggers_real_slot_search():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("ви ставите брекети?"))
    await processor.process(_message("так"))
    result = await processor.process(_message("коли найббличий можливий запис"))

    assert result["booking_result"]["status"] == "nearest_availability_suggested"
    assert "найближчий вільний час" in result["reply_text"].lower()
    assert calendar.checked
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_generic_availability_suggestions_skip_closed_days():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("ви ставите брекети?"))
    await processor.process(_message("так"))
    result = await processor.process(_message("коли можна"))

    assert result["booking_result"]["status"] == "availability_suggested"
    assert "На завтра" not in result["reply_text"]
    assert "неділю" not in result["reply_text"].lower()
    assert "післязавтра" in result["reply_text"].lower()
    assert calendar.checked
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_confirmation_after_multiple_suggested_slots_asks_for_specific_time():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("ви ставите брекети?"))
    await processor.process(_message("так"))
    await processor.process(_message("коли можна"))
    result = await processor.process(_message("так"))

    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "який саме час" in result["reply_text"].lower()
    assert "11:00" in result["reply_text"]
    assert "16:00" in result["reply_text"]
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_offered_day_time_outside_list_gets_checked_directly():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Привіт, хочу записатись до стоматолога"))
    await processor.process(_message("та просто огляд"))
    await processor.process(_message("на понеділок коли можна"))
    result = await processor.process(_message("11"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "11:00" in result["reply_text"]
    assert calendar.checked[-1]["start_dt"].hour == 11
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_later_offered_day_time_outside_refined_list_gets_checked_directly():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Скільки у вас коштує огляд?"))
    await processor.process(_message("Ага"))
    await processor.process(_message("А завтра можна?"))
    await processor.process(_message("А пізніше нема?"))
    result = await processor.process(_message("Давай 15"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert "15:00" in result["reply_text"]
    assert calendar.checked[-1]["start_dt"].hour == 15
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_bare_consultation_followup_uses_orthodontic_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("ви ставите брекети?"))
    await processor.process(_message("яка вартість?"))
    acknowledgement = await processor.process(_message("зрозумів"))
    consultation = await processor.process(_message("а консультація?"))
    booking = await processor.process(_message("давайте тоді запишусь"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert acknowledgement["reply_text"] == "Добре."
    assert "Консультація ортодонта" in consultation["reply_text"]
    assert "Консультація стоматолога коштує" not in consultation["reply_text"]
    assert booking["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "orthodontic_consultation"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_next_week_booking_words_are_not_unknown_service_detail():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("хочу на чистку зубів наступного тижня"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert pending["current_service_id"] == "dental_cleaning"
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_exam_with_date_and_daypart_resolves_consultation_booking():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("хочу на огляд у вівторок після обіду"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["booking_result"]["status"] == "daypart_slots_suggested"
    assert pending["current_service_id"] == "dental_consultation"
    assert "12:00" in result["reply_text"]
    assert calendar.checked
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_missing_service_date_or_nearest_does_not_repeat_blindly():
    processor, calendar = _build_dental_processor()

    first = await processor.process(_message("хочу записатись до стоматолога"))
    date = await processor.process(_message("завтра"))
    nearest = await processor.process(_message("а коли найближче можна?"))

    assert first["booking_result"]["status"] == "waiting_for_service"
    assert "на завтра" in date["reply_text"].lower()
    assert "найближчий час" in nearest["reply_text"].lower()
    assert "на яку послугу" in nearest["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_known_orthodontic_comparison_is_not_unknown_variant():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("що краще елайнерами чи брекетів?"))

    assert "немає підтвердження" not in result["reply_text"].lower()
    assert "брекет" in result["reply_text"].lower() or "елайнер" in result["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_pain_question_during_booking_uses_active_service_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("мені треба видалити зуб"))
    result = await processor.process(_message("а це дуже боляче буде?"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_grounded_question"
    assert "Видалення зуба" in result["reply_text"]
    assert "комфорту" in result["reply_text"]
    assert "знеболення" in result["reply_text"]
    assert "Липська" not in result["reply_text"]
    assert "пародонтолога" not in result["reply_text"].lower()
    assert context["current_service_id"] == "tooth_extraction"
    assert pending["current_service_id"] == "dental_consultation"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_pain_question_after_service_context_does_not_hijack_to_random_service():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("чистка"))
    result = await processor.process(_message("а це буде боляче?"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Професійна гігієна зубів" in result["reply_text"]
    assert "комфорту" in result["reply_text"]
    assert "Липська" not in result["reply_text"]
    assert "пародонтолога" not in result["reply_text"].lower()
    assert context["current_service_id"] == "dental_cleaning"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "скок коштуе видалит зуб мудрості?",
        "скок коштує видалити зуб мудрості?",
    ],
)
async def test_dental_price_question_with_typos_and_colloquial_forms_stays_grounded(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "front_desk_contextual_answer"
    assert "3500 грн" in result["reply_text"]
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, expected",
    [
        ("хочу вініри, скільки коштують", "12000 грн"),
        ("хочу поставити брекети, яка ціна", "Металеві брекети"),
        ("хочу зробити чистку, скільки це буде коштувати", "1800 грн"),
    ],
)
async def test_dental_want_plus_service_plus_price_answers_price_not_booking(text, expected):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "front_desk_contextual_answer"
    assert expected in result["reply_text"]
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert result["booking_result"] is None
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_implant_descriptive_context_is_not_unknown_detail():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("імплантація зуба, немає одного зуба вже пів року"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert "імплант" in result["reply_text"].lower()
    assert context["current_service_id"] == "dental_implant"
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_braces_material_advice_followup_stays_braces_not_crowns():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("ви ставите брекети?"))
    result = await processor.process(_message("металеві чи керамічні що ви порадите"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert "Коронки" not in result["reply_text"]
    assert "Металеві брекети" in result["reply_text"]
    assert "Керамічні брекети" in result["reply_text"]
    assert "Дистанційно не можемо сказати" in result["reply_text"]
    assert context["current_service_id"] == "braces"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_implant_turnkey_price_with_crown_stays_implant_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує імплант?"))
    result = await processor.process(_message("точну ціну імпланта з коронкою під ключ"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert "16000 грн" in result["reply_text"]
    assert "Коронка розраховується окремо" in result["reply_text"]
    assert context["current_service_id"] == "dental_implant"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_implantologist_consultation_alias_stays_specific():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує імплант?"))
    result = await processor.process(_message("консультація імплантолога"))
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Консультація імплантолога коштує від 700 грн" in result["reply_text"]
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert context["current_service_id"] == "implant_consultation"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_chat_discussion_request_does_not_start_booking():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує імплант?"))
    result = await processor.process(_message("можна не на дзвінок, а в чаті обговорити?"))

    assert result["intent"] == "front_desk_chat_discussion"
    assert "у чаті" in result["reply_text"].lower()
    assert "точний план" in result["reply_text"].lower()
    assert result["booking_result"] is None
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_fresh_implant_turnkey_price_with_crown_stays_implant():
    processor, calendar = _build_dental_processor()

    result = await processor.process(
        _message("а ви можете сказати точну ціну імпланта з коронкою під ключ")
    )
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "front_desk_contextual_answer"
    assert "16000 грн" in result["reply_text"]
    assert "Коронка розраховується окремо" in result["reply_text"]
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert context["current_service_id"] == "dental_implant"
    assert result["booking_result"] is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_fresh_implantologist_consultation_booking_uses_specific_service():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("хочу записатись на консультацію імплантолога"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}
    context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert context["current_service_id"] == "implant_consultation"
    assert pending["current_service_id"] == "prosthetics_consultation"
    assert pending["current_service_name"] == "Консультація імплантолога"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_patient_asks_clinic_to_pick_time_offers_nearest_and_contact_confirms():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатися на відбілювання"))
    offer = await processor.process(_message("як вам зручно підберіть самі"))
    confirmed = await processor.process(_message("Ганна Іщенко, 0685554433"))

    assert offer["booking_result"]["status"] == "nearest_availability_suggested"
    assert "найближчий вільний час" in offer["reply_text"].lower()
    assert "Записати вас" in offer["reply_text"]
    assert confirmed["booking_result"]["status"] == "confirmed"
    assert "Ганна Іщенко" in confirmed["reply_text"]
    assert "Чекатимемо на вас" in confirmed["reply_text"]
    assert len(calendar.created) == 1


@pytest.mark.asyncio
async def test_dental_combined_booking_with_flexible_time_offers_nearest_slot():
    processor, calendar = _build_dental_processor()

    offer = await processor.process(
        _message("запишіть мене на відбілювання, мені як вам зручно, підберіть самі")
    )
    confirmed = await processor.process(_message("Ганна Іщенко, 0685554433"))

    assert offer["booking_result"]["status"] == "nearest_availability_suggested"
    assert "немає підтвердження" not in offer["reply_text"].lower()
    assert "найближчий вільний час" in offer["reply_text"].lower()
    assert confirmed["booking_result"]["status"] == "confirmed"
    assert "Ганна Іщенко" in confirmed["reply_text"]
    assert len(calendar.created) == 1


@pytest.mark.asyncio
async def test_dental_greeting_with_pediatric_caries_context_does_not_consume_intent():
    processor, calendar = _build_dental_processor()

    result = await processor.process(
        _message("добрий вечір, у доньки, здається, дірка в молочному зубі, їй 6 років")
    )
    context = processor.memory_service.get_context("patient-1")

    assert "Вітаю" not in result["reply_text"] or "Чим можемо допомогти" not in result["reply_text"]
    assert "1800 грн" in result["reply_text"] or "діт" in result["reply_text"].lower()
    assert context["current_service_id"] == "pediatric_caries_treatment"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_pediatric_fear_followup_uses_child_comfort_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("дитині 6 років треба полікувати карієс"))
    result = await processor.process(
        _message("вона дуже боїться лікарів чесно, як ви з дітьми працюєте?")
    )
    context = processor.memory_service.get_context("patient-1")

    assert "графік роботи" not in result["reply_text"].lower()
    assert "дітьми" in result["reply_text"].lower()
    assert "спокійному темпі" in result["reply_text"].lower()
    assert context["current_service_id"] == "pediatric_caries_treatment"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_without_pain_wordform_uses_comfort_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу видалити зуб мудрості"))
    result = await processor.process(_message("а без болю можна це зробити?"))

    assert "комфорту" in result["reply_text"]
    assert "знеболення" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_installment_faq_wins_over_service_unknown_detail_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує відбілювання?"))
    result = await processor.process(_message("а розстрочка є?"))

    assert "Так, доступне розтермінування через monobank та ПУМБ" in result["reply_text"]
    assert "немає підтвердження" not in result["reply_text"].lower()
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_installment_bank_followup_uses_same_grounded_faq():
    processor, calendar = _build_dental_processor()

    first = await processor.process(_message("а розтермінування є?"))
    followup = await processor.process(_message("через який банк?"))

    assert first["reply_text"] == INSTALLMENT_FAQ_MARKER
    assert followup["reply_text"] == INSTALLMENT_FAQ_MARKER
    assert "monobank" in followup["reply_text"]
    assert "ПУМБ" in followup["reply_text"]
    assert "Хочу правильно зорієнтувати" not in followup["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


INSTALLMENT_FAQ_MARKER = "Так, доступне розтермінування через monobank та ПУМБ."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "У вас є розтермінування?",
        "Є розстрочка?",
        "Можна оплатити частинами?",
        "У вас є оплата частинами?",
    ],
)
async def test_dental_installment_synonyms_get_same_grounded_answer(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["reply_text"] == INSTALLMENT_FAQ_MARKER
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "чи працює монобанк?",
        "можна через ПУМБ?",
        "у вас є monobank?",
        "через monobank можна?",
        "ПУМБ підтримуєте?",
    ],
)
async def test_dental_installment_supported_bank_names_use_grounded_faq(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["reply_text"] == INSTALLMENT_FAQ_MARKER
    assert result["intent"] == "front_desk_contextual_answer"
    assert calendar.checked == []
    assert calendar.created == []
    assert processor.booking_service._get_pending_confirmation("patient-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "можна оплата частинами за брекети?",
        "можна оплатити брекети частинами?",
        "за брекети є розстрочка?",
        "брекети можна взяти в розстрочку?",
        "можна оплатити відбілювання частинами?",
        "за імпланти можна платити частинами?",
    ],
)
async def test_dental_named_service_installment_questions_do_not_start_booking(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1")
    context = processor.memory_service.get_context("patient-1")

    assert result["reply_text"] == INSTALLMENT_FAQ_MARKER
    assert result["intent"] == "front_desk_contextual_answer"
    assert pending is None
    assert context["question_context"] == "faq"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "чи працює монобанк?",
        "можна через ПУМБ?",
        "у вас є monobank?",
        "через monobank можна?",
        "ПУМБ підтримуєте?",
    ],
)
async def test_dental_installment_bank_faq_during_active_booking_preserves_state(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    await processor.process(_message("о 15"))
    before = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    checks_before = len(calendar.checked)
    creates_before = len(calendar.created)

    result = await processor.process(_message(text))
    after = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["reply_text"] == INSTALLMENT_FAQ_MARKER
    assert result["intent"] == "booking_grounded_question"
    assert after == before
    assert after["current_service_id"] == "dental_cleaning"
    assert after["start_dt"] == "2026-08-26T15:00:00+03:00"
    assert len(calendar.checked) == checks_before
    assert len(calendar.created) == creates_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "можна оплата частинами за брекети?",
        "можна оплатити брекети частинами?",
        "за брекети є розстрочка?",
        "брекети можна взяти в розстрочку?",
        "можна оплатити відбілювання частинами?",
        "за імпланти можна платити частинами?",
    ],
)
async def test_dental_named_service_installment_faq_during_active_booking_preserves_state(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    await processor.process(_message("о 15"))
    before_pending = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    before_context = processor.memory_service.get_context("patient-1")
    checks_before = len(calendar.checked)
    creates_before = len(calendar.created)

    result = await processor.process(_message(text))
    after_pending = processor.booking_service._get_pending_confirmation("patient-1") or {}
    after_context = processor.memory_service.get_context("patient-1")

    assert result["reply_text"] == INSTALLMENT_FAQ_MARKER
    assert result["intent"] == "booking_grounded_question"
    assert after_pending == before_pending
    assert after_context == before_context
    assert len(calendar.checked) == checks_before
    assert len(calendar.created) == creates_before


@pytest.mark.asyncio
async def test_dental_unknown_bank_name_does_not_get_supported_installment_answer():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("через приватбанк можна?"))

    assert INSTALLMENT_FAQ_MARKER not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []
    assert processor.booking_service._get_pending_confirmation("patient-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "хочу записатися на брекети",
        "можна записатися на брекети?",
        "запишіть мене на брекети",
        "хочу брекети",
    ],
)
async def test_dental_installment_guard_preserves_explicit_braces_booking_controls(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_request"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert pending["current_service_id"] == "orthodontic_consultation"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_insurance_question_does_not_answer_with_hours():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Ви працюєте зі страховкою?"))

    assert "Роботу зі страховими потрібно уточнити з адміністратором" in result["reply_text"]
    assert "09:00-19:00" not in result["reply_text"]
    assert "Пн-Пт" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_insurance_faq_still_matches_original_phrasing():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Приймаєте страховку?"))

    assert "Роботу зі страховими потрібно уточнити з адміністратором" in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_payment_options_question_answers_payment_not_availability():
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("Які є варіанти оплати?"))

    assert "Приймаємо готівку, оплату карткою, Apple Pay та Google Pay" in result["reply_text"]
    assert result["intent"] != "booking_availability_question"
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_generic_availability_questions_still_route_to_booking_after_marker_narrowing():
    """The overly-broad "які є варіанти" appointment marker was removed (it
    hijacked unrelated "варіанти X" questions like payment options); this
    guards that genuine availability phrasing -- including the sibling
    "варіанти по часу" marker that remains -- still routes to booking
    availability, not a regression from narrowing the marker list."""
    calendar = SelectiveConfiguredCalendarService(
        [_kyiv_dt(2026, 8, 24, 14), _kyiv_dt(2026, 8, 24, 17, 30)]
    )

    processor1, calendar1 = _build_dental_processor(calendar_service=calendar)
    await processor1.process(_message("хочу записатись на чистку"))
    result1 = await processor1.process(_message("які вільні слоти є?"))
    assert result1["intent"] == "booking_availability_question"

    processor2, calendar2 = _build_dental_processor(calendar_service=calendar)
    await processor2.process(_message("хочу на консультацію"))
    result2 = await processor2.process(_message("варіанти по часу які є?"))
    assert result2["intent"] == "booking_availability_question"

    processor3, calendar3 = _build_dental_processor(calendar_service=calendar)
    await processor3.process(_message("хочу записатись на чистку"))
    result3 = await processor3.process(_message("коли можна?"))
    assert result3["intent"] == "booking_availability_question"


async def test_dental_active_booking_location_and_metro_question_not_hijacked_by_nearest_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("коли найближчий час?"))
    checks_before = len(calendar.checked)
    result = await processor.process(_message("А де ви знаходитесь і яке найближче метро?"))

    assert result["intent"] == "booking_grounded_question"
    assert "Липська, 12" in result["reply_text"]
    assert "Арсенальна" in result["reply_text"]
    assert len(calendar.checked) == checks_before
    assert calendar.created == []
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.WAITING_FOR_TIME


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "яке найближче метро?",
        "біля якого метро ви?",
        "до вас далеко від метро?",
        "ви біля Арсенальної?",
        "де ви знаходитесь?",
        "яка адреса?",
    ],
)
async def test_dental_fresh_metro_location_questions_use_grounded_location(text):
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Липська, 12" in result["reply_text"]
    assert "Арсенальна" in result["reply_text"]
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_metro_question_after_service_context_does_not_start_nearest_booking():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("скільки коштує чистка?"))
    checks_before = len(calendar.checked)
    result = await processor.process(_message("а яке найближче метро?"))

    assert result["intent"] == "front_desk_contextual_answer"
    assert "Липська, 12" in result["reply_text"]
    assert "Арсенальна" in result["reply_text"]
    assert processor.booking_service._get_pending_confirmation("patient-1") is None
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


@pytest.mark.asyncio
async def test_dental_metro_question_during_active_booking_preserves_pending_state():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    before_pending = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    before_context = processor.memory_service.get_context("patient-1")
    checks_before = len(calendar.checked)

    result = await processor.process(_message("а біля якого метро ви?"))
    after_pending = processor.booking_service._get_pending_confirmation("patient-1") or {}
    after_context = processor.memory_service.get_context("patient-1")

    assert result["intent"] == "booking_grounded_question"
    assert "Липська, 12" in result["reply_text"]
    assert "Арсенальна" in result["reply_text"]
    assert after_pending == before_pending
    assert after_context.get("current_service_id") == before_context.get("current_service_id")
    assert after_context.get("current_service_name") == before_context.get("current_service_name")
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Від Арсенальної далеко?",
        "Від Арсенальної далеко йти?",
        "Скільки йти від метро?",
        "Від метро далеко йти?",
    ],
)
async def test_dental_active_booking_location_followup_not_hijacked_by_cleaning_context(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Хочу записатися на чистку зубів"))
    await processor.process(_message("Добрий вечір, де ви знаходитесь?"))
    await processor.process(_message("А біля якого метро?"))
    before_pending = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    checks_before = len(calendar.checked)

    result = await processor.process(_message(text))
    after_pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_grounded_question"
    assert "Липська, 12" in result["reply_text"]
    assert "Арсенальна" in result["reply_text"]
    assert "Професійна гігієна" not in result["reply_text"]
    assert "1800 грн" not in result["reply_text"]
    assert after_pending == before_pending
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Як від метро до вас дійти?",
        "А як до вас дійти від Арсенальної?",
    ],
)
async def test_dental_active_booking_existing_directions_followups_still_location_replies(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("Хочу записатися на чистку зубів"))
    before_pending = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    checks_before = len(calendar.checked)

    result = await processor.process(_message(text))
    after_pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_grounded_question"
    assert "Липська, 12" in result["reply_text"]
    assert "Арсенальна" in result["reply_text"]
    assert after_pending == before_pending
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "followup", "expected"),
    [
        (["скільки коштує чистка?"], "а скільки?", "1800 грн"),
        (["видалення зуба"], "а це боляче?", "знеболення"),
        (["лікування карієсу"], "а входить анестезія?", "анестез"),
    ],
)
async def test_dental_contextual_followups_still_use_meaningful_service_context(
    setup, followup, expected
):
    processor, calendar = _build_dental_processor()

    for text in setup:
        await processor.process(_message(text))
    result = await processor.process(_message(followup))

    assert expected in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "найближчий вільний час",
        "який найближчий час?",
        "коли найближче можна?",
    ],
)
async def test_dental_metro_location_guard_preserves_nearest_availability_requests(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message(text))

    assert result["intent"] in {"booking_flow", "booking_nearest_availability", "booking_availability_question"}
    assert result["booking_result"] is not None
    assert "Липська" not in result["reply_text"]
    assert calendar.checked != []
    assert calendar.created == []


async def test_dental_active_booking_hours_and_payment_faq_not_hijacked_by_booking_context():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу записатись на чистку"))
    await processor.process(_message("коли найближчий час?"))
    checks_before = len(calendar.checked)

    hours = await processor.process(_message("а в суботу працюєте?"))
    payment = await processor.process(_message("а оплата частинами є?"))

    assert hours["intent"] == "booking_grounded_question"
    assert "10:00-16:00" in hours["reply_text"]
    assert payment["intent"] == "booking_grounded_question"
    assert "monobank" in payment["reply_text"]
    assert "ПУМБ" in payment["reply_text"]
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


async def test_dental_active_booking_admin_callback_request_with_date_not_treated_as_booking_date():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("коли найближчий час?"))
    checks_before = len(calendar.checked)
    result = await processor.process(
        _message("Можете передати адміністратору, щоб він мені завтра подзвонив?")
    )

    assert result["intent"] == "booking_grounded_question"
    assert result["booking_result"] is None
    assert "AI-асистент" in result["reply_text"]
    assert "завтра" not in result["reply_text"].lower()
    assert "можу запропонувати" not in result["reply_text"].lower()
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


class MondayReplyDatetime(_REAL_DATETIME):
    @classmethod
    def now(cls, tz=None):
        fixed = _REAL_DATETIME(2026, 8, 24, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        return fixed if tz is None else fixed.astimezone(tz)


class SaturdayReplyDatetime(_REAL_DATETIME):
    @classmethod
    def now(cls, tz=None):
        fixed = _REAL_DATETIME(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        return fixed if tz is None else fixed.astimezone(tz)


class SundayReplyDatetime(_REAL_DATETIME):
    @classmethod
    def now(cls, tz=None):
        fixed = _REAL_DATETIME(2026, 8, 23, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        return fixed if tz is None else fixed.astimezone(tz)


@pytest.mark.parametrize(
    "text",
    [
        "адміністратор сьогодні працює?",
        "сьогодні є адміністратор?",
        "можна зв'язатися з адміністратором сьогодні?",
    ],
)
async def test_dental_administrator_today_hours_use_grounded_same_as_clinic_rule_weekday(
    monkeypatch, text
):
    monkeypatch.setattr(business_hours_module, "datetime", MondayReplyDatetime)
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert "адміністратор сьогодні працює з 09:00 до 19:00" in result["reply_text"]
    assert "Графік роботи:" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_administrator_today_hours_use_grounded_same_as_clinic_rule_saturday(
    monkeypatch,
):
    monkeypatch.setattr(business_hours_module, "datetime", SaturdayReplyDatetime)
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("А адміністратор сьогодні працює?"))

    assert "адміністратор сьогодні працює з 10:00 до 16:00" in result["reply_text"]
    assert "Графік роботи:" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


async def test_dental_administrator_today_hours_use_grounded_same_as_clinic_rule_sunday(
    monkeypatch,
):
    monkeypatch.setattr(business_hours_module, "datetime", SundayReplyDatetime)
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message("адміністратор сьогодні працює?"))

    assert "Сьогодні адміністратор не працює" in result["reply_text"]
    assert "У неділю клініка вихідна" in result["reply_text"]
    assert "Графік роботи:" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.parametrize(
    "text",
    [
        "а адміністратор завтра працює?",
        "коли працює адміністратор?",
        "який графік адміністратора?",
    ],
)
async def test_dental_administrator_hours_natural_variants_are_direct_grounded_replies(
    monkeypatch, text
):
    monkeypatch.setattr(business_hours_module, "datetime", SaturdayReplyDatetime)
    processor, calendar = _build_dental_processor()

    result = await processor.process(_message(text))

    assert "адміністратор" in result["reply_text"].lower()
    assert "Графік роботи:" not in result["reply_text"]
    assert calendar.checked == []
    assert calendar.created == []


@pytest.mark.parametrize(
    "text",
    [
        "адміністратор працює сьогодні?",
        "ви сьогодні відкриті?",
        "лікар сьогодні працює?",
        "ви працюєте сьогодні?",
        "клініка працює сьогодні?",
        "можна сьогодні оплатити карткою?",
        "сьогодні є адміністратор?",
    ],
)
async def test_dental_active_booking_fact_question_with_today_preserves_waiting_for_time(
    monkeypatch, text
):
    monkeypatch.setattr(business_hours_module, "datetime", SaturdayReplyDatetime)
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    before_pending = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    checks_before = len(calendar.checked)

    result = await processor.process(_message(text))
    after_pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_grounded_question"
    assert result["booking_result"] is None
    assert after_pending == before_pending
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.WAITING_FOR_TIME
    assert "Добре, на сьогодні" not in result["reply_text"]
    if "адміністратор" in text:
        assert "адміністратор сьогодні працює з 10:00 до 16:00" in result["reply_text"]
        assert "Графік роботи:" not in result["reply_text"]
    elif "відкрит" in text or "ви працюєте" in text:
        assert "Графік роботи" in result["reply_text"]
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


@pytest.mark.parametrize(
    "text",
    [
        "адміністратор працює сьогодні?",
        "ви сьогодні відкриті?",
        "лікар сьогодні працює?",
        "можна сьогодні оплатити карткою?",
    ],
)
async def test_dental_active_booking_fact_question_with_today_preserves_waiting_for_contact_slot(
    monkeypatch, text
):
    monkeypatch.setattr(business_hours_module, "datetime", SaturdayReplyDatetime)
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу о 15"))
    before_pending = dict(processor.booking_service._get_pending_confirmation("patient-1") or {})
    checks_before = len(calendar.checked)

    result = await processor.process(_message(text))
    after_pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_grounded_question"
    assert result["booking_result"] is None
    assert before_pending.get("start_dt") == "2026-08-26T15:00:00+03:00"
    assert after_pending == before_pending
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.WAITING_FOR_CONTACT
    if "адміністратор" in text:
        assert "адміністратор сьогодні працює з 10:00 до 16:00" in result["reply_text"]
        assert "Графік роботи:" not in result["reply_text"]
    elif "відкрит" in text:
        assert "Графік роботи" in result["reply_text"]
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


@pytest.mark.parametrize(
    "text",
    [
        "краще сьогодні",
        "а можна сьогодні?",
        "давайте сьогодні",
    ],
)
async def test_dental_active_booking_genuine_today_date_corrections_still_update_pending(text):
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    result = await processor.process(_message(text))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] in {"booking_flow", "booking_availability_question"}
    assert pending.get("requested_date") == "2026-08-22"
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.WAITING_FOR_TIME
    assert calendar.created == []


async def test_dental_active_booking_combined_fact_and_booking_today_update_allowed():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у середу"))
    result = await processor.process(_message("ви сьогодні відкриті? хочу прийти сьогодні"))
    pending = processor.booking_service._get_pending_confirmation("patient-1") or {}

    assert result["intent"] == "booking_flow"
    assert pending.get("requested_date") == "2026-08-22"
    assert processor.booking_service.get_booking_state("patient-1") == BookingState.WAITING_FOR_TIME
    assert calendar.created == []


async def test_dental_active_booking_genuine_continuation_still_checks_calendar():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на чистку"))
    result = await processor.process(_message("у четвер о 15"))

    assert result["booking_result"]["status"] == "waiting_for_contact"
    assert calendar.checked[-1]["start_dt"] == _kyiv_dt(2026, 8, 27, 15)
    assert calendar.created == []


async def test_dental_active_booking_cancel_and_reschedule_still_route_to_booking_service():
    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)

    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у четвер о 15"))
    await processor.process(_message("Дмитро 0987121328"))
    cancel = await processor.process(_message("скасуйте мій запис"))

    assert cancel["booking_result"]["status"] == "cancelled"
    assert calendar.deleted == ["dental-event-1"]

    calendar = RescheduleTrackingCalendarService()
    processor, calendar = _build_dental_processor(calendar_service=calendar)
    await processor.process(_message("хочу на чистку"))
    await processor.process(_message("у четвер о 15"))
    await processor.process(_message("Дмитро 0987121328"))
    reschedule = await processor.process(_message("перенесіть запис на пʼятницю о 14"))

    assert reschedule["booking_result"]["status"] == "rescheduled"
    assert len(calendar.rescheduled) == 1


async def test_dental_fresh_service_booking_does_not_inherit_stale_unconfirmed_date():
    processor, calendar = _build_dental_processor()

    await processor.process(_message("хочу на огляд"))
    stale_date = await processor.process(_message("післязавтра"))
    checks_before = len(calendar.checked)
    result = await processor.process(_message("хочу на чистку"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert stale_date["booking_result"]["status"] == "waiting_for_time"
    assert stale_date["booking_result"]["requested_date"] == "2026-08-24"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "післязавтра" not in result["reply_text"].lower()
    assert "який день" in result["reply_text"].lower() or "день і приблизний час" in result["reply_text"].lower()
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending.get("requested_date") is None
    assert pending.get("start_dt") is None
    assert len(calendar.checked) == checks_before
    assert calendar.created == []


async def test_dental_missing_service_continuation_still_preserves_previous_date():
    processor, calendar = _build_dental_processor()

    first = await processor.process(_message("хочу записатись післязавтра"))
    result = await processor.process(_message("на чистку"))
    pending = processor.booking_service._get_pending_confirmation("patient-1")

    assert first["booking_result"]["status"] == "waiting_for_service"
    assert result["booking_result"]["status"] == "waiting_for_time"
    assert "післязавтра" in result["reply_text"].lower()
    assert pending["current_service_id"] == "dental_cleaning"
    assert pending["requested_date"] == "2026-08-24"
    assert calendar.created == []
