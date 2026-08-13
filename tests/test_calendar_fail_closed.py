from datetime import datetime

from app.application.services.calendar_service import CalendarService


class UnconfiguredCalendarClient:
    def is_configured(self):
        return False


class FailingCalendarClient:
    def is_configured(self):
        return True

    def is_time_available(self, start_dt, end_dt):
        raise RuntimeError("Google Calendar unavailable")

    def update_event_time(self, event_id, start_dt, end_dt):
        raise RuntimeError("Google Calendar unavailable")


class BusyCalendarClient:
    def is_configured(self):
        return True

    def is_time_available(self, start_dt, end_dt):
        return False


class AvailableCalendarClient:
    def is_configured(self):
        return True

    def is_time_available(self, start_dt, end_dt):
        return True

    def __init__(self):
        self.updated_events = []

    def update_event_time(self, event_id, start_dt, end_dt):
        self.updated_events.append(
            {
                "event_id": event_id,
                "start_dt": start_dt,
                "end_dt": end_dt,
            }
        )


def test_unconfigured_calendar_returns_no_slots():
    service = CalendarService(
        google_calendar_client=UnconfiguredCalendarClient()
    )

    assert service.get_available_slots("en") == []


def test_calendar_api_failure_returns_no_slots():
    service = CalendarService(
        google_calendar_client=FailingCalendarClient()
    )

    assert service.get_available_slots("en") == []


def test_busy_calendar_does_not_generate_fake_fallback_slots():
    service = CalendarService(
        google_calendar_client=BusyCalendarClient()
    )

    assert service.get_available_slots("en") == []


def test_unconfigured_calendar_specific_time_is_not_available():
    service = CalendarService(
        google_calendar_client=UnconfiguredCalendarClient()
    )

    start_dt = datetime(2026, 8, 13, 11, 0)

    assert service.check_specific_time_availability(start_dt) is False


def test_calendar_api_failure_specific_time_is_not_available():
    service = CalendarService(
        google_calendar_client=FailingCalendarClient()
    )

    start_dt = datetime(2026, 8, 13, 11, 0)

    assert service.check_specific_time_availability(start_dt) is False


def test_real_available_slots_still_work():
    service = CalendarService(
        google_calendar_client=AvailableCalendarClient()
    )

    slots = service.get_available_slots("en")

    assert slots == [
        "tomorrow at 11:00",
        "tomorrow at 15:00",
        "the day after tomorrow at 13:00",
    ]


def test_unconfigured_calendar_reschedule_fails_closed():
    service = CalendarService(
        google_calendar_client=UnconfiguredCalendarClient()
    )

    start_dt = datetime(2026, 8, 13, 11, 0)

    try:
        service.reschedule_event("event-1", start_dt)
    except RuntimeError as exc:
        assert "not configured" in str(exc)
    else:
        raise AssertionError("reschedule_event should fail closed")


def test_calendar_api_failure_reschedule_fails_closed():
    service = CalendarService(
        google_calendar_client=FailingCalendarClient()
    )

    start_dt = datetime(2026, 8, 13, 11, 0)

    try:
        service.reschedule_event("event-1", start_dt)
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("reschedule_event should fail closed")


def test_calendar_reschedule_normalizes_naive_datetime():
    client = AvailableCalendarClient()
    service = CalendarService(google_calendar_client=client)

    service.reschedule_event("event-1", datetime(2026, 8, 13, 11, 0))

    assert client.updated_events[0]["event_id"] == "event-1"
    assert client.updated_events[0]["start_dt"].tzinfo is not None
