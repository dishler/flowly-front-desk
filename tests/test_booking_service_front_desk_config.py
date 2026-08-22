from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.application.services.booking_service import BookingService
from app.application.services.front_desk_config_service import FrontDeskConfigService
from app.application.services.language_service import LanguageService


class RecordingCalendarService:
    def __init__(self) -> None:
        self.checked = []
        self.created = []
        self.google_calendar_client = self

    def is_configured(self) -> bool:
        return True

    def check_specific_time_availability(self, start_dt, duration_minutes: int = 30) -> bool:
        self.checked.append(
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
            event_id = "event-1"
            html_link = "https://calendar.example/event"
            status = "confirmed"

        return CreatedEvent()


def _write_config(tmp_path, *, booking: dict, business: dict | None = None):
    path = tmp_path / "front_desk_config.json"
    path.write_text(
        json.dumps(
            {
                "business": business
                or {
                    "name": "Smile Dental Clinic",
                    "working_hours": {"Mon-Sun": "00:00-23:59"},
                },
                "assistant": {
                    "supported_languages": ["uk", "en"],
                    "tone": "calm front desk",
                    "default_language": "uk",
                },
                "booking": booking,
                "qualification": {"enabled": False, "questions": []},
                "safety": {
                    "do_not_claim": ["medical diagnosis"],
                    "unknown_fallback": "Уточніть, будь ласка.",
                },
                "handoff": {
                    "rules": ["complex_request"],
                    "reply": "Передам адміністратору.",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _service(tmp_path, *, booking: dict, calendar_service=None, business: dict | None = None):
    return BookingService(
        calendar_service=calendar_service or RecordingCalendarService(),
        language_service=LanguageService(),
        front_desk_config_service=FrontDeskConfigService(str(_write_config(tmp_path, booking=booking, business=business))),
    )


def _booking_config(duration_minutes: int = 30) -> dict:
    return {
        "enabled": True,
        "appointment_label": "візит",
        "duration_minutes": duration_minutes,
        "required_contact_fields": ["name", "phone"],
    }


def _smile_business_hours() -> dict:
    return {
        "name": "Smile Dental Clinic",
        "working_hours": {
            "Mon-Fri": "09:00-19:00",
            "Sat": "10:00-16:00",
            "Sun": "closed",
        },
    }


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Kyiv"))


def test_booking_disabled_hands_off_without_starting_state(tmp_path):
    service = _service(
        tmp_path,
        booking={
            "enabled": False,
            "appointment_label": "візит",
            "duration_minutes": 30,
            "required_contact_fields": ["name", "phone"],
        },
    )

    result = service.start_booking_flow("user-1", "завтра о 12")

    assert result["status"] == "booking_disabled"
    assert result["booking_state"] == "NONE"
    assert service.get_booking_state("user-1").value == "NONE"


def test_exact_time_booking_rejects_closed_sunday_before_calendar_check(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        booking=_booking_config(),
        calendar_service=calendar,
        business=_smile_business_hours(),
    )

    result = service.start_booking_flow(
        "user-1",
        "Хочу записатися на чистку завтра о 12",
        requested_dt_override=_dt(2026, 8, 23, 12),
    )

    assert result["status"] == "outside_business_hours"
    assert "клініка не працює" in result["reply_text"].lower()
    assert calendar.checked == []
    assert service.get_booking_state("user-1").value == "NONE"


def test_exact_time_booking_checks_calendar_for_open_monday(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        booking=_booking_config(),
        calendar_service=calendar,
        business=_smile_business_hours(),
    )

    result = service.start_booking_flow("user-1", "Хочу записатися на чистку у понеділок о 12")

    assert result["status"] == "waiting_for_contact"
    assert calendar.checked
    assert calendar.checked[-1]["start_dt"].weekday() == 0
    assert calendar.checked[-1]["start_dt"].hour == 12


def test_exact_time_booking_rejects_saturday_before_opening(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        booking=_booking_config(),
        calendar_service=calendar,
        business=_smile_business_hours(),
    )

    result = service.start_booking_flow(
        "user-1",
        "субота 09:00",
        requested_dt_override=_dt(2026, 8, 22, 9),
    )

    assert result["status"] == "outside_business_hours"
    assert calendar.checked == []


def test_exact_time_booking_checks_calendar_for_saturday_opening(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        booking=_booking_config(),
        calendar_service=calendar,
        business=_smile_business_hours(),
    )

    result = service.start_booking_flow(
        "user-1",
        "субота 10:00",
        requested_dt_override=_dt(2026, 8, 22, 10),
    )

    assert result["status"] == "waiting_for_contact"
    assert calendar.checked[-1]["start_dt"].hour == 10


def test_exact_time_booking_rejects_saturday_after_closing(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        booking=_booking_config(),
        calendar_service=calendar,
        business=_smile_business_hours(),
    )

    result = service.start_booking_flow(
        "user-1",
        "субота 16:30",
        requested_dt_override=_dt(2026, 8, 22, 16, 30),
    )

    assert result["status"] == "outside_business_hours"
    assert calendar.checked == []


def test_exact_time_booking_rejects_weekday_before_opening(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        booking=_booking_config(),
        calendar_service=calendar,
        business=_smile_business_hours(),
    )

    result = service.start_booking_flow(
        "user-1",
        "понеділок 08:30",
        requested_dt_override=_dt(2026, 8, 24, 8, 30),
    )

    assert result["status"] == "outside_business_hours"
    assert calendar.checked == []


def test_exact_time_booking_allows_weekday_slot_ending_at_closing(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        booking=_booking_config(duration_minutes=30),
        calendar_service=calendar,
        business=_smile_business_hours(),
    )

    result = service.start_booking_flow(
        "user-1",
        "понеділок 18:30",
        requested_dt_override=_dt(2026, 8, 24, 18, 30),
    )

    assert result["status"] == "waiting_for_contact"
    assert calendar.checked[-1]["start_dt"].hour == 18
    assert calendar.checked[-1]["start_dt"].minute == 30


def test_exact_time_booking_rejects_weekday_slot_starting_at_closing(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        booking=_booking_config(duration_minutes=30),
        calendar_service=calendar,
        business=_smile_business_hours(),
    )

    result = service.start_booking_flow(
        "user-1",
        "понеділок 19:00",
        requested_dt_override=_dt(2026, 8, 24, 19),
    )

    assert result["status"] == "outside_business_hours"
    assert calendar.checked == []


def test_exact_time_booking_fails_closed_when_working_hours_missing(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        booking=_booking_config(),
        calendar_service=calendar,
        business={"name": "Smile Dental Clinic"},
    )

    result = service.start_booking_flow(
        "user-1",
        "понеділок 12:00",
        requested_dt_override=_dt(2026, 8, 24, 12),
    )

    assert result["status"] == "outside_business_hours"
    assert calendar.checked == []


def test_booking_duration_and_label_come_from_config(tmp_path):
    calendar = RecordingCalendarService()
    service = _service(
        tmp_path,
        calendar_service=calendar,
        booking={
            "enabled": True,
            "appointment_label": "візит",
            "duration_minutes": 45,
            "required_contact_fields": ["name", "phone"],
        },
    )

    waiting = service.start_booking_flow("user-1", "завтра о 12")
    confirmed = service.process_booking_message("user-1", "Іван +380991112233")

    assert "візит" in waiting["reply_text"]
    assert calendar.checked[0]["duration_minutes"] == 45
    assert calendar.checked[-1]["duration_minutes"] == 45
    assert calendar.created[0]["duration_minutes"] == 45
    assert calendar.created[0]["summary"] == "Візит booking"
    assert confirmed["status"] == "confirmed"
    assert "візит" in confirmed["reply_text"]


def test_required_contact_fields_can_require_phone_not_email(tmp_path):
    service = _service(
        tmp_path,
        booking={
            "enabled": True,
            "appointment_label": "візит",
            "duration_minutes": 30,
            "required_contact_fields": ["name", "phone"],
        },
    )

    service.start_booking_flow("user-1", "завтра о 12")
    result = service.process_booking_message("user-1", "Іван ivan@example.test")

    assert result["status"] == "waiting_for_contact"
    assert "номер телефону" in result["reply_text"]


def test_booking_without_config_preserves_legacy_email_or_phone_contact_behavior():
    calendar = RecordingCalendarService()
    service = BookingService(
        calendar_service=calendar,
        language_service=LanguageService(),
    )

    service.start_booking_flow("user-1", "завтра о 12")
    result = service.process_booking_message("user-1", "Іван ivan@example.test")

    assert result["status"] == "confirmed"
    assert calendar.created[0]["duration_minutes"] == 30
    assert "дзвінок" in result["reply_text"]
