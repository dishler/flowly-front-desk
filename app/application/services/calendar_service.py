from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.infrastructure.google.calendar_client import (
    CreatedCalendarEvent,
    GoogleCalendarClient,
)

logger = logging.getLogger(__name__)


class CalendarService:
    def __init__(self, google_calendar_client: GoogleCalendarClient | None = None) -> None:
        self.google_calendar_client = google_calendar_client
        self.timezone = ZoneInfo(settings.default_timezone)

    def get_fallback_slots(self, language: str) -> List[str]:
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

    def _localized_slot(self, slot_key: str, language: str) -> str:
        mapping = {
            "tomorrow_11": {
                "uk": "завтра о 11:00",
                "en": "tomorrow at 11:00",
            },
            "tomorrow_15": {
                "uk": "завтра о 15:00",
                "en": "tomorrow at 15:00",
            },
            "day_after_13": {
                "uk": "післязавтра о 13:00",
                "en": "the day after tomorrow at 13:00",
            },
        }
        return mapping.get(slot_key, {}).get(language, mapping.get(slot_key, {}).get("en", "tomorrow at 11:00"))

    def get_available_slots(self, language: str) -> List[str]:
        calendar_configured = (
            self.google_calendar_client.is_configured()
            if self.google_calendar_client
            else False
        )
        logger.info(f"Calendar configured: {calendar_configured}")

        if not calendar_configured:
            return []

        now = datetime.now(self.timezone)
        candidates = [
            (
                "tomorrow_11",
                (now + timedelta(days=1)).replace(
                    hour=11,
                    minute=0,
                    second=0,
                    microsecond=0,
                ),
            ),
            (
                "tomorrow_15",
                (now + timedelta(days=1)).replace(
                    hour=15,
                    minute=0,
                    second=0,
                    microsecond=0,
                ),
            ),
            (
                "day_after_13",
                (now + timedelta(days=2)).replace(
                    hour=13,
                    minute=0,
                    second=0,
                    microsecond=0,
                ),
            ),
        ]

        available: List[str] = []

        for slot_key, start_dt in candidates:
            end_dt = start_dt + timedelta(minutes=30)

            try:
                is_available = self.google_calendar_client.is_time_available(
                    start_dt,
                    end_dt,
                )
            except Exception:
                logger.exception(
                    "Google Calendar availability check failed"
                )
                return []

            if is_available:
                available.append(
                    self._localized_slot(slot_key, language)
                )

        return available

    def check_specific_time_availability(
        self,
        start_dt: datetime,
        duration_minutes: int = 30,
    ) -> bool:
        calendar_configured = (
            self.google_calendar_client.is_configured()
            if self.google_calendar_client
            else False
        )
        logger.info(f"Calendar configured: {calendar_configured}")

        if not calendar_configured:
            return False

        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=self.timezone)

        end_dt = start_dt + timedelta(minutes=duration_minutes)

        try:
            return self.google_calendar_client.is_time_available(
                start_dt,
                end_dt,
            )
        except Exception:
            logger.exception(
                "Google Calendar specific-time availability check failed"
            )
            return False

    def create_booking_event(
        self,
        start_dt: datetime,
        duration_minutes: int = 30,
        summary: str = "Consultation call",
        description: str = "",
        attendee_emails: Optional[List[str]] = None,
    ) -> CreatedCalendarEvent:
        calendar_configured = (
            self.google_calendar_client.is_configured()
            if self.google_calendar_client
            else False
        )
        logger.info(f"Calendar configured: {calendar_configured}")
        if not calendar_configured:
            raise RuntimeError("Google Calendar is not configured for event creation.")

        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=self.timezone)

        end_dt = start_dt + timedelta(minutes=duration_minutes)

        return self.google_calendar_client.create_event(
            start_dt=start_dt,
            end_dt=end_dt,
            summary=summary,
            description=description,
            attendee_emails=attendee_emails or [],
        )

    def reschedule_event(
        self,
        event_id: str,
        start_dt: datetime,
        duration_minutes: int = 30,
    ) -> None:
        calendar_configured = (
            self.google_calendar_client.is_configured()
            if self.google_calendar_client
            else False
        )
        logger.info(f"Calendar configured: {calendar_configured}")
        if not calendar_configured:
            raise RuntimeError("Google Calendar is not configured for event reschedule.")

        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=self.timezone)

        end_dt = start_dt + timedelta(minutes=duration_minutes)

        self.google_calendar_client.update_event_time(
            event_id=event_id,
            start_dt=start_dt,
            end_dt=end_dt,
        )

    def update_booking_contact_details(self, event_id: str, description: str) -> None:
        calendar_configured = (
            self.google_calendar_client.is_configured()
            if self.google_calendar_client
            else False
        )
        logger.info(f"Calendar configured: {calendar_configured}")
        if not calendar_configured:
            raise RuntimeError("Google Calendar is not configured for event contact update.")

        self.google_calendar_client.update_event_description(
            event_id=event_id,
            description=description,
        )

    def delete_event(self, event_id: str) -> None:
        calendar_configured = (
            self.google_calendar_client.is_configured()
            if self.google_calendar_client
            else False
        )
        logger.info(f"Calendar configured: {calendar_configured}")
        if not calendar_configured:
            raise RuntimeError("Google Calendar is not configured for event deletion.")

        self.google_calendar_client.delete_event(event_id)
        
