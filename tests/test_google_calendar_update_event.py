from datetime import datetime

import pytest

from app.infrastructure.google.calendar_client import (
    GoogleCalendarClient,
    GoogleCalendarClientError,
)


class RecordingEvents:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.patch_calls = []

    def patch(self, **kwargs):
        self.patch_calls.append(kwargs)
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("provider failed")
        return {"id": "event-1", "status": "confirmed"}


class RecordingService:
    def __init__(self, *, fail: bool = False) -> None:
        self.events_resource = RecordingEvents(fail=fail)

    def events(self):
        return self.events_resource


def _client(service: RecordingService) -> GoogleCalendarClient:
    client = GoogleCalendarClient()
    client.enabled = True
    client.calendar_id = "calendar@example.test"
    client.service_account_json = "{}"
    client.timezone = "Europe/Kyiv"
    client._service = service
    return client


def test_update_event_time_uses_patch_with_start_and_end_only():
    service = RecordingService()
    client = _client(service)
    start_dt = datetime.fromisoformat("2026-04-27T12:00:00+03:00")
    end_dt = datetime.fromisoformat("2026-04-27T12:30:00+03:00")

    client.update_event_time("event-1", start_dt, end_dt)

    assert service.events_resource.patch_calls == [
        {
            "calendarId": "calendar@example.test",
            "eventId": "event-1",
            "body": {
                "start": {
                    "dateTime": "2026-04-27T12:00:00+03:00",
                    "timeZone": "Europe/Kyiv",
                },
                "end": {
                    "dateTime": "2026-04-27T12:30:00+03:00",
                    "timeZone": "Europe/Kyiv",
                },
            },
            "sendUpdates": "none",
        }
    ]


@pytest.mark.parametrize(
    ("event_id", "start_dt", "end_dt"),
    [
        ("", datetime.fromisoformat("2026-04-27T12:00:00+03:00"), datetime.fromisoformat("2026-04-27T12:30:00+03:00")),
        ("event-1", datetime(2026, 4, 27, 12, 0), datetime.fromisoformat("2026-04-27T12:30:00+03:00")),
        ("event-1", datetime.fromisoformat("2026-04-27T12:00:00+03:00"), datetime.fromisoformat("2026-04-27T11:30:00+03:00")),
    ],
)
def test_update_event_time_validates_inputs(event_id, start_dt, end_dt):
    client = _client(RecordingService())

    with pytest.raises(GoogleCalendarClientError):
        client.update_event_time(event_id, start_dt, end_dt)


def test_update_event_time_wraps_provider_errors():
    client = _client(RecordingService(fail=True))

    with pytest.raises(GoogleCalendarClientError, match="update event"):
        client.update_event_time(
            "event-1",
            datetime.fromisoformat("2026-04-27T12:00:00+03:00"),
            datetime.fromisoformat("2026-04-27T12:30:00+03:00"),
        )
