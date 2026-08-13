from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.core.config import settings


logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GoogleCalendarClientError(Exception):
    pass


@dataclass
class CalendarSlot:
    start: datetime
    end: datetime


@dataclass
class CreatedCalendarEvent:
    event_id: str
    html_link: str
    status: str


class GoogleCalendarClient:
    def __init__(self) -> None:
        self.enabled = settings.google_calendar_enabled
        self.calendar_id = settings.google_calendar_id
        self.service_account_file = settings.google_service_account_file
        self.service_account_json = settings.google_service_account_json
        self.timezone = settings.google_calendar_timezone
        self._service = None

    def is_configured(self) -> bool:
        has_credentials_file = bool(
            self.service_account_file
            and Path(self.service_account_file).exists()
        )
        has_credentials_json = bool(self.service_account_json.strip())
        return bool(
            self.enabled
            and self.calendar_id
            and (has_credentials_file or has_credentials_json)
        )

    def _get_service(self):
        if self._service is not None:
            return self._service

        if not self.is_configured():
            raise GoogleCalendarClientError("Google Calendar is not fully configured.")

        if self.service_account_file and Path(self.service_account_file).exists():
            credentials = service_account.Credentials.from_service_account_file(
                self.service_account_file,
                scopes=SCOPES,
            )
        elif self.service_account_json.strip():
            try:
                credentials_info = json.loads(self.service_account_json)
            except json.JSONDecodeError as exc:
                raise GoogleCalendarClientError("Invalid Google service account JSON.") from exc

            credentials = service_account.Credentials.from_service_account_info(
                credentials_info,
                scopes=SCOPES,
            )
        else:
            raise GoogleCalendarClientError("Missing Google Calendar credentials.")

        self._service = build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )
        return self._service

    @staticmethod
    def _to_rfc3339(dt: datetime) -> str:
        if dt.tzinfo is None:
            raise GoogleCalendarClientError("Datetime must be timezone-aware.")
        return dt.isoformat()

    def healthcheck(self) -> dict[str, Any]:
        if not self.is_configured():
            return {
                "enabled": self.enabled,
                "configured": False,
                "connected": False,
                "reason": "Missing Google Calendar env or credentials.",
            }

        try:
            service = self._get_service()
            calendar = service.calendars().get(calendarId=self.calendar_id).execute()

            return {
                "enabled": True,
                "configured": True,
                "connected": True,
                "calendar_id": self.calendar_id,
                "calendar_summary": calendar.get("summary"),
                "calendar_timezone": calendar.get("timeZone"),
            }
        except Exception as exc:
            return {
                "enabled": True,
                "configured": True,
                "connected": False,
                "reason": str(exc),
            }

    def get_busy_periods(self, time_min: datetime, time_max: datetime) -> list[CalendarSlot]:
        try:
            service = self._get_service()

            body = {
                "timeMin": self._to_rfc3339(time_min),
                "timeMax": self._to_rfc3339(time_max),
                "timeZone": self.timezone,
                "items": [{"id": self.calendar_id}],
            }

            logger.info(
                "calendar freebusy query calendar_id=%s time_min=%s time_max=%s",
                self.calendar_id,
                time_min.isoformat(),
                time_max.isoformat(),
            )

            response = service.freebusy().query(body=body).execute()

            logger.info(
                "calendar freebusy response received calendar_id=%s has_response=%s",
                self.calendar_id,
                bool(response),
            )

            calendar_data = response.get("calendars", {}).get(self.calendar_id, {})

            if not calendar_data:
                raise GoogleCalendarClientError(
                    f"Freebusy returned no calendar data for calendar_id={self.calendar_id}"
                )

            calendar_errors = calendar_data.get("errors", [])
            if calendar_errors:
                raise GoogleCalendarClientError(
                    f"Freebusy returned errors for calendar_id={self.calendar_id}: {calendar_errors}"
                )

            busy_items = calendar_data.get("busy", [])

            logger.info(
                "calendar freebusy parsed calendar_id=%s busy_count=%s",
                self.calendar_id,
                len(busy_items),
            )

            result: list[CalendarSlot] = []
            for item in busy_items:
                result.append(
                    CalendarSlot(
                        start=datetime.fromisoformat(item["start"]),
                        end=datetime.fromisoformat(item["end"]),
                    )
                )
            return result

        except HttpError as exc:
            raise GoogleCalendarClientError(
                f"Google Calendar freebusy failed: {exc}"
            ) from exc
        except Exception as exc:
            raise GoogleCalendarClientError(
                f"Unexpected Google Calendar error: {exc}"
            ) from exc

    def is_time_available(self, start_dt: datetime, end_dt: datetime) -> bool:
        if end_dt <= start_dt:
            raise GoogleCalendarClientError("end_dt must be after start_dt")

        logger.info(
            "calendar availability check start calendar_id=%s start_dt=%s end_dt=%s",
            self.calendar_id,
            start_dt.isoformat(),
            end_dt.isoformat(),
        )

        busy_periods = self.get_busy_periods(start_dt, end_dt)

        logger.info(
            "calendar availability check busy_periods calendar_id=%s count=%s",
            self.calendar_id,
            len(busy_periods),
        )

        for busy in busy_periods:
            overlaps = start_dt < busy.end and end_dt > busy.start
            if overlaps:
                logger.info(
                    "calendar availability overlap detected calendar_id=%s start_dt=%s end_dt=%s busy_start=%s busy_end=%s",
                    self.calendar_id,
                    start_dt.isoformat(),
                    end_dt.isoformat(),
                    busy.start.isoformat(),
                    busy.end.isoformat(),
                )
                return False

        logger.info(
            "calendar availability no overlap calendar_id=%s start_dt=%s end_dt=%s",
            self.calendar_id,
            start_dt.isoformat(),
            end_dt.isoformat(),
        )
        return True

    def create_event(
        self,
        start_dt: datetime,
        end_dt: datetime,
        summary: str,
        description: str = "",
        attendee_emails: List[str] | None = None,
    ) -> CreatedCalendarEvent:
        if end_dt <= start_dt:
            raise GoogleCalendarClientError("end_dt must be after start_dt")

        try:
            service = self._get_service()

            event_body = {
                "summary": summary,
                "description": description,
                "start": {
                    "dateTime": self._to_rfc3339(start_dt),
                    "timeZone": self.timezone,
                },
                "end": {
                    "dateTime": self._to_rfc3339(end_dt),
                    "timeZone": self.timezone,
                },
            }
            attendees = [{"email": email} for email in (attendee_emails or []) if email]
            if attendees:
                event_body["attendees"] = attendees

            logger.info(
                "calendar create_event start calendar_id=%s start_dt=%s end_dt=%s summary=%r attendees=%s",
                self.calendar_id,
                start_dt.isoformat(),
                end_dt.isoformat(),
                summary,
                len(attendees),
            )

            created = (
                service.events()
                .insert(
                    calendarId=self.calendar_id,
                    body=event_body,
                    sendUpdates="all" if attendees else "none",
                )
                .execute()
            )

            logger.info(
                "calendar create_event success calendar_id=%s event_id=%s status=%s",
                self.calendar_id,
                created.get("id"),
                created.get("status", "confirmed"),
            )

            return CreatedCalendarEvent(
                event_id=created["id"],
                html_link=created.get("htmlLink", ""),
                status=created.get("status", "confirmed"),
            )

        except HttpError as exc:
            raise GoogleCalendarClientError(
                f"Google Calendar create event failed: {exc}"
            ) from exc
        except Exception as exc:
            raise GoogleCalendarClientError(
                f"Unexpected Google Calendar create event error: {exc}"
            ) from exc

    def update_event_time(
        self,
        event_id: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> None:
        if not event_id:
            raise GoogleCalendarClientError("event_id is required.")
        self._to_rfc3339(start_dt)
        self._to_rfc3339(end_dt)
        if end_dt <= start_dt:
            raise GoogleCalendarClientError("end_dt must be after start_dt")

        body = {
            "start": {
                "dateTime": self._to_rfc3339(start_dt),
                "timeZone": self.timezone,
            },
            "end": {
                "dateTime": self._to_rfc3339(end_dt),
                "timeZone": self.timezone,
            },
        }

        try:
            service = self._get_service()

            logger.info(
                "calendar update_event_time start calendar_id=%s event_id=%s start_dt=%s end_dt=%s",
                self.calendar_id,
                event_id,
                start_dt.isoformat(),
                end_dt.isoformat(),
            )

            (
                service.events()
                .patch(
                    calendarId=self.calendar_id,
                    eventId=event_id,
                    body=body,
                    sendUpdates="none",
                )
                .execute()
            )

            logger.info(
                "calendar update_event_time success calendar_id=%s event_id=%s",
                self.calendar_id,
                event_id,
            )

        except HttpError as exc:
            raise GoogleCalendarClientError(
                f"Google Calendar update event failed: {exc}"
            ) from exc
        except Exception as exc:
            raise GoogleCalendarClientError(
                f"Unexpected Google Calendar update event error: {exc}"
            ) from exc

    def delete_event(self, event_id: str) -> None:
        if not event_id:
            raise GoogleCalendarClientError("event_id is required.")

        try:
            service = self._get_service()

            logger.info(
                "calendar delete_event start calendar_id=%s event_id=%s",
                self.calendar_id,
                event_id,
            )

            (
                service.events()
                .delete(
                    calendarId=self.calendar_id,
                    eventId=event_id,
                    sendUpdates="none",
                )
                .execute()
            )

            logger.info(
                "calendar delete_event success calendar_id=%s event_id=%s",
                self.calendar_id,
                event_id,
            )

        except HttpError as exc:
            raise GoogleCalendarClientError(
                f"Google Calendar delete event failed: {exc}"
            ) from exc
        except Exception as exc:
            raise GoogleCalendarClientError(
                f"Unexpected Google Calendar delete event error: {exc}"
            ) from exc
