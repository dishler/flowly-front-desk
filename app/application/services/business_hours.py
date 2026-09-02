from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import settings


logger = logging.getLogger(__name__)


def weekday_indexes_for_hours_key(raw_key: str) -> set[int]:
    aliases = {
        "mon": 0,
        "monday": 0,
        "пн": 0,
        "понеділок": 0,
        "tue": 1,
        "tuesday": 1,
        "вт": 1,
        "вівторок": 1,
        "wed": 2,
        "wednesday": 2,
        "ср": 2,
        "середа": 2,
        "thu": 3,
        "thursday": 3,
        "чт": 3,
        "четвер": 3,
        "fri": 4,
        "friday": 4,
        "пт": 4,
        "п'ятниця": 4,
        "п’ятниця": 4,
        "sat": 5,
        "saturday": 5,
        "сб": 5,
        "субота": 5,
        "sun": 6,
        "sunday": 6,
        "нд": 6,
        "неділя": 6,
    }

    parts = [part.strip().lower() for part in re.split(r"\s*[-–—]\s*", raw_key) if part.strip()]
    if len(parts) == 1:
        weekday = aliases.get(parts[0])
        return {weekday} if weekday is not None else set()
    if len(parts) == 2:
        start = aliases.get(parts[0])
        end = aliases.get(parts[1])
        if start is None or end is None:
            return set()
        if start <= end:
            return set(range(start, end + 1))
        return set(range(start, 7)).union(range(0, end + 1))
    return set()


def is_closed_working_hours_value(raw_value: Any) -> bool:
    return isinstance(raw_value, str) and raw_value.strip().lower() in {
        "closed",
        "зачинено",
        "вихідний",
        "закрито",
    }


def parse_working_hours_window(raw_value: Any) -> tuple[time, time] | None:
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip().lower()
    if is_closed_working_hours_value(value):
        return None

    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\s*", value)
    if not match:
        return None

    start_hour, start_minute, end_hour, end_minute = [int(part) for part in match.groups()]
    if not (
        0 <= start_hour <= 23
        and 0 <= end_hour <= 23
        and 0 <= start_minute <= 59
        and 0 <= end_minute <= 59
    ):
        return None

    start = time(start_hour, start_minute)
    end = time(end_hour, end_minute)
    if start >= end:
        return None
    return start, end


def business_hours_status_for_date(
    working_hours: dict[str, Any] | None,
    target_date: date,
    *,
    missing_status: str = "missing",
) -> tuple[str, tuple[time, time] | None]:
    if working_hours is None:
        return (missing_status, None)

    for raw_key, raw_value in working_hours.items():
        weekdays = weekday_indexes_for_hours_key(str(raw_key))
        if not weekdays:
            logger.warning("Ignoring malformed working-hours day key: %r", raw_key)
            continue
        if target_date.weekday() not in weekdays:
            continue
        if is_closed_working_hours_value(raw_value):
            return ("closed", None)
        window = parse_working_hours_window(raw_value)
        if window is None:
            logger.warning("Ignoring malformed working-hours window for %r: %r", raw_key, raw_value)
            return ("malformed", None)
        return ("open", window)

    return ("missing_day", None)


def format_time_window_time(value: time) -> str:
    return value.strftime("%H:%M")


def format_working_hours(working_hours: dict[str, Any]) -> str:
    return "; ".join(f"{key}: {value}" for key, value in working_hours.items())


def ukrainian_weekday_name(target_date: date) -> str:
    return [
        "понеділок",
        "вівторок",
        "середу",
        "четвер",
        "п’ятницю",
        "суботу",
        "неділю",
    ][target_date.weekday()]


def current_local_date() -> date:
    return datetime.now(ZoneInfo(settings.default_timezone)).date()


def relative_date_from_text(normalized_text: str) -> date | None:
    today = current_local_date()
    if "післязавтра" in normalized_text:
        return today + timedelta(days=2)
    if "завтра" in normalized_text:
        return today + timedelta(days=1)
    if "сьогодні" in normalized_text or "зараз" in normalized_text:
        return today
    return None


def relative_day_label(target_date: date) -> str:
    today = current_local_date()
    if target_date == today:
        return "сьогодні"
    if target_date == today + timedelta(days=1):
        return "завтра"
    if target_date == today + timedelta(days=2):
        return "післязавтра"
    return f"у {ukrainian_weekday_name(target_date)}"
