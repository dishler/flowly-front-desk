from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Dict
from zoneinfo import ZoneInfo

from app.application.services.calendar_service import CalendarService
from app.application.services.language_service import LanguageService
from app.core.config import settings
from app.domain.enums import BookingState


logger = logging.getLogger(__name__)


class BookingService:
    EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
    PHONE_RE = re.compile(r"(?:(?<=\D)|^)(\+?\d[\d\-\s\(\)]{8,}\d)(?=\D|$)")

    def __init__(
        self,
        calendar_service: CalendarService,
        language_service: LanguageService,
        booking_state_service=None,
        front_desk_config_service=None,
    ) -> None:
        self.calendar_service = calendar_service
        self.language_service = language_service
        self.booking_state_service = booking_state_service
        self.front_desk_config_service = front_desk_config_service
        self.timezone = ZoneInfo(settings.default_timezone)
        self.pending_confirmations: dict[str, dict[str, Any]] = {}
        self.captured_contacts: dict[str, dict[str, Any]] = {}
        self.completed_bookings: dict[str, dict[str, Any]] = {}

    def _booking_config(self) -> dict[str, Any]:
        if self.front_desk_config_service is None:
            return {}
        return self.front_desk_config_service.get_booking()

    def _booking_enabled(self) -> bool:
        booking = self._booking_config()
        if not booking:
            return True
        return booking.get("enabled") is True

    def _appointment_label(self) -> str:
        return str(self._booking_config().get("appointment_label") or "дзвінок")

    def _booking_duration_minutes(self) -> int:
        duration = self._booking_config().get("duration_minutes")
        return duration if isinstance(duration, int) and duration > 0 else 30

    def _booking_description_prefix(self) -> str:
        if self.front_desk_config_service is None:
            return "Booked via Flowly Meta Bot"
        business = self.front_desk_config_service.get_business()
        business_name = str(business.get("name") or "").strip()
        if business_name:
            return f"Booked via {business_name} front desk"
        return "Booked via front desk assistant"

    def _business_working_hours(self) -> dict[str, Any] | None:
        if self.front_desk_config_service is None:
            return None
        business = self.front_desk_config_service.get_business()
        working_hours = business.get("working_hours")
        return working_hours if isinstance(working_hours, dict) else None

    def _normalize_booking_text(self, text: str) -> str:
        normalized = " ".join(text.strip().lower().split())
        normalized = re.sub(r"\bпісля\s+за+втра\b", "післязавтра", normalized)
        normalized = re.sub(r"\bпісля\s+завтра\b", "післязавтра", normalized)
        normalized = re.sub(r"\bпісляза+втра\b", "післязавтра", normalized)
        return normalized

    def _weekday_indexes_for_hours_key(self, raw_key: str) -> set[int]:
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

    def _is_closed_working_hours_value(self, raw_value: Any) -> bool:
        return isinstance(raw_value, str) and raw_value.strip().lower() in {
            "closed",
            "зачинено",
            "вихідний",
            "закрито",
        }

    def _parse_working_hours_window(self, raw_value: Any) -> tuple[time, time] | None:
        if not isinstance(raw_value, str):
            return None
        value = raw_value.strip().lower()
        if self._is_closed_working_hours_value(value):
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

    def _business_hours_status_for_date(self, target_date: date) -> tuple[str, tuple[time, time] | None]:
        working_hours = self._business_working_hours()
        if working_hours is None:
            return ("legacy_open" if self.front_desk_config_service is None else "missing", None)

        for raw_key, raw_value in working_hours.items():
            weekdays = self._weekday_indexes_for_hours_key(str(raw_key))
            if not weekdays:
                logger.warning("Ignoring malformed working-hours day key: %r", raw_key)
                continue
            if target_date.weekday() not in weekdays:
                continue
            if self._is_closed_working_hours_value(raw_value):
                return ("closed", None)
            window = self._parse_working_hours_window(raw_value)
            if window is None:
                logger.warning("Ignoring malformed working-hours window for %r: %r", raw_key, raw_value)
                return ("malformed", None)
            return ("open", window)

        return ("missing_day", None)

    def _is_within_business_hours(self, start_dt: datetime, duration_minutes: int) -> bool:
        local_start = start_dt.astimezone(self.timezone)
        status, window = self._business_hours_status_for_date(local_start.date())
        if status == "legacy_open":
            return True
        if status in {"missing", "malformed", "missing_day"}:
            logger.warning(
                "Booking time rejected due to working-hours config status=%s date=%s",
                status,
                local_start.date().isoformat(),
            )
        if status != "open" or window is None:
            return False

        local_end = local_start + timedelta(minutes=duration_minutes)
        business_start = datetime.combine(local_start.date(), window[0], tzinfo=self.timezone)
        business_end = datetime.combine(local_start.date(), window[1], tzinfo=self.timezone)
        return business_start <= local_start and local_end <= business_end

    def _required_contact_fields(self) -> list[str]:
        fields = self._booking_config().get("required_contact_fields")
        if isinstance(fields, list) and fields:
            return [str(field) for field in fields]
        return ["name", "phone_or_email"]

    def _has_required_contact(self, *, customer_name: str | None, email: str | None, phone: str | None) -> bool:
        for field in self._required_contact_fields():
            if field == "name" and not customer_name:
                return False
            if field == "email" and not email:
                return False
            if field == "phone" and not phone:
                return False
            if field == "phone_or_email" and not (phone or email):
                return False
        return True

    def has_pending_confirmation(self, sender_id: str) -> bool:
        if self.booking_state_service is not None:
            return self.booking_state_service.has_pending_confirmation(sender_id)
        return sender_id in self.pending_confirmations

    def get_booking_state(self, sender_id: str) -> BookingState:
        pending = self._get_pending_confirmation(sender_id)
        if not pending:
            return BookingState.NONE

        if pending.get("is_active") is not True:
            logger.warning("Ignoring inactive booking state for sender_id=%s", sender_id)
            return BookingState.NONE

        raw_state = pending.get("state")
        if raw_state:
            try:
                return BookingState(raw_state)
            except ValueError:
                logger.warning("Unknown booking state for sender_id=%s: %r", sender_id, raw_state)

        if pending.get("stage") == "awaiting_contact":
            return BookingState.WAITING_FOR_CONTACT

        return BookingState.WAITING_FOR_TIME

    def _save_pending_confirmation(self, sender_id: str, data: dict[str, Any]) -> None:
        if self.booking_state_service is not None:
            self.booking_state_service.save_pending_confirmation(sender_id, data)
            return
        self.pending_confirmations[sender_id] = data

    def _get_pending_confirmation(self, sender_id: str) -> dict[str, Any] | None:
        if self.booking_state_service is not None:
            return self.booking_state_service.get_pending_confirmation(sender_id)
        return self.pending_confirmations.get(sender_id)

    def _clear_pending_confirmation(self, sender_id: str) -> None:
        if self.booking_state_service is not None:
            self.booking_state_service.clear_pending_confirmation(sender_id)
            return
        self.pending_confirmations.pop(sender_id, None)

    def _has_completed_booking(self, sender_id: str) -> bool:
        if self.booking_state_service is not None:
            return self.booking_state_service.has_completed_booking(sender_id)
        return sender_id in self.completed_bookings

    def _save_completed_booking(self, sender_id: str, data: dict[str, Any]) -> None:
        if self.booking_state_service is not None:
            self.booking_state_service.save_completed_booking(sender_id, data)
            return
        self.completed_bookings[sender_id] = data

    def _get_completed_booking(self, sender_id: str) -> dict[str, Any] | None:
        if self.booking_state_service is not None:
            return self.booking_state_service.get_completed_booking(sender_id)
        return self.completed_bookings.get(sender_id)

    def _clear_completed_booking(self, sender_id: str) -> None:
        if self.booking_state_service is not None:
            self.booking_state_service.clear_completed_booking(sender_id)
            return
        self.completed_bookings.pop(sender_id, None)

    def _save_booking_state(
        self,
        sender_id: str,
        *,
        state: BookingState,
        language: str,
        start_dt: datetime | None = None,
        duration_minutes: int | None = None,
        summary: str | None = None,
        description: str | None = None,
        contact_email: str | None = None,
        contact_phone: str | None = None,
        customer_name: str | None = None,
        source_channel: str | None = None,
        context_summary: str | None = None,
        requested_date: date | str | None = None,
        requested_day_label: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "is_active": True,
            "step": state.value,
            "state": state.value,
            "language": language,
            "duration_minutes": duration_minutes or self._booking_duration_minutes(),
            "summary": summary or f"{self._appointment_label().capitalize()} booking",
            "description": description or self._booking_description_prefix(),
            "contact_email": contact_email,
            "contact_phone": contact_phone,
            "customer_name": customer_name,
            "source_channel": source_channel,
            "context_summary": context_summary,
        }
        if start_dt is not None:
            payload["start_dt"] = self._serialize_pending_start_dt(start_dt)
        if requested_date is not None:
            payload["requested_date"] = (
                requested_date.isoformat() if isinstance(requested_date, date) else requested_date
            )
        if requested_day_label:
            payload["requested_day_label"] = requested_day_label
        logger.info(
            "Saving booking state sender_id=%s state=%s has_start_dt=%s has_email=%s has_phone=%s",
            sender_id,
            state.value,
            start_dt is not None,
            bool(contact_email),
            bool(contact_phone),
        )
        self._save_pending_confirmation(sender_id, payload)

    def _detect_language(self, text: str) -> str:
        if re.search(r"[А-Яа-яІіЇїЄєҐґ]", text):
            return "uk"
        return "en"

    def _is_confirmation_text(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        normalized = re.sub(r"[.!?…]+$", "", normalized).strip()
        # Strip clause-separating punctuation (e.g. the comma in "так,
        # підходить") so it doesn't stay glued to a word and defeat the
        # exact-match / word-based checks below.
        normalized = re.sub(r"[,;]+", " ", normalized)
        normalized = " ".join(normalized.split())
        normalized = re.sub(r"([а-яіїєґ])\1+", r"\1", normalized)
        confirmations = {
            "yes", "y", "yeah", "yep", "sure", "ok", "okay", "confirm",
            "так", "та", "ага", "добре", "ок", "окей", "підтверджую", "підтвердити",
            "підходить", "піідходить", "підходе", "так гуд", "good", "fine",
            "давай", "давайте", "так давай", "так давайте", "ок давай", "окей давай",
            "добре давай", "ага давай", "підходить давай",
        }
        if normalized in confirmations:
            return True

        words = normalized.split()
        if not words or len(words) > 4:
            return False

        confirmation_starts = {
            "так", "та", "ага", "добре", "ок", "окей", "yes", "ok", "okay", "sure",
            "підходить", "підходе",
        }
        confirmation_actions = {
            "давай", "давайте", "бронюй", "записуй", "підходить", "підтверджую",
        }
        return words[0] in confirmation_starts and any(word in confirmation_actions for word in words[1:])

    def _is_confirmation(self, text: str) -> bool:
        return self._is_confirmation_text(text)

    def _is_rejection(self, text: str) -> bool:
        normalized = text.strip().lower()
        rejections = {
            "no", "nope", "not now", "cancel",
            "ні", "не", "скасувати", "не треба", "не знаю",
        }
        rejection_markers = [
            "скасуйте",
            "скасувати",
            "скасуй",
            "відмінити",
            "відмініть",
            "cancel",
            "not now",
            "пізніше",
            "потім",
            "не зараз",
            "ще не знаю",
            "напишу пізніше",
            "пізніше напишу",
            "не хочу записуватись",
            "не хочу записуватися",
        ]
        if normalized in rejections or any(marker in normalized for marker in rejection_markers):
            return True
        # Bounded "changed my mind" check: only the whole message, not a
        # qualifier like "передумав щодо чистки" (service change, not cancel).
        compact = re.sub(r"[.!?…,]+$", "", normalized).strip()
        return bool(re.fullmatch(r"(?:я\s+)?передумав[аи]?", compact))

    def _looks_like_dental_pain_mention(self, text: str) -> bool:
        normalized = self._normalize_booking_text(text)
        markers = [
            "болить зуб",
            "зуб болить",
            "болять зуби",
            "зуби болять",
            "зуб ниє",
            "ниє зуб",
            "зубний біль",
            "гострий біль",
        ]
        return any(marker in normalized for marker in markers)

    def _pain_acknowledgement_sentence(self) -> str:
        return "Розумію, це неприємно. Якщо зуб болить, краще не відкладати огляд. "

    def get_pain_acknowledgement_reply(self, language: str) -> str:
        return (
            self._pain_acknowledgement_sentence()
            + "Можу допомогти підібрати найближчий вільний час, якщо хочете записатися."
        )

    def _build_unclear_time_reply(self, language: str, *, pain_mentioned: bool = False) -> str:
        prompt = "Напишіть, будь ласка, який день і приблизний час вам зручний?"
        if pain_mentioned:
            return self._pain_acknowledgement_sentence() + "Можу допомогти підібрати найближчий вільний час. " + prompt
        if self.front_desk_config_service is None:
            return "Супер, тоді можемо коротко розібрати ваш кейс зі спеціалістом. " + prompt
        return f"Супер, тоді можемо підібрати {self._appointment_label()}. " + prompt

    def _build_missing_time_reply(self, language: str, day_label: str | None = None) -> str:
        label = day_label or "цей день"
        if label in {"today", "tomorrow"}:
            label = "сьогодні" if label == "today" else "завтра"
        if label == "day after tomorrow":
            label = "післязавтра"
        return f"Добре, на {label}. Підкажіть, будь ласка, на котру годину вам зручно?"

    def _build_unavailable_reply(self, language: str) -> str:
        slots = self.calendar_service.get_available_slots(language)
        return f"На цей час слот уже зайнятий. Можу запропонувати: {', '.join(slots)}."

    def _build_availability_check_failed_reply(self, language: str) -> str:
        return "Не вдалося перевірити доступність цього часу. Напишіть, будь ласка, бажаний день і час, і ми перевіримо запис."

    def _build_outside_business_hours_reply(self, language: str) -> str:
        return "На цей час клініка не працює. Підкажіть, будь ласка, інший день або час?"

    def _display_slots(self, slots: list[datetime]) -> list[datetime]:
        return slots[:3]

    def _display_slots_by_day(
        self,
        slots_by_day: dict[str, list[datetime]],
    ) -> dict[str, list[datetime]]:
        display_slots: dict[str, list[datetime]] = {}
        remaining = 3
        for day_key, slots in slots_by_day.items():
            if remaining <= 0:
                break
            selected = slots[:remaining]
            if selected:
                display_slots[day_key] = selected
                remaining -= len(selected)
        return display_slots

    def _build_daypart_slots_reply(
        self,
        language: str,
        *,
        requested_date: date,
        requested_day_label: str | None = None,
        daypart_label: str,
        slots: list[datetime],
    ) -> str:
        day_label = requested_day_label or self._format_date_label_for_reply(requested_date, language) or "цей день"
        day_prefix = "На" if day_label in {"сьогодні", "завтра", "післязавтра"} else "У"
        times = self._format_slot_times(self._display_slots(slots), language)
        if times:
            return f"{day_prefix} {day_label} {daypart_label} можу запропонувати {times}. Який час вам зручніший?"
        return f"{day_prefix} {day_label} {daypart_label} не бачу вільних слотів. Підкажіть інший день або час?"

    def _build_time_window_slots_reply(
        self,
        language: str,
        *,
        requested_date: date,
        requested_day_label: str | None = None,
        window_label: str,
        slots: list[datetime],
    ) -> str:
        day_label = requested_day_label or self._format_date_label_for_reply(requested_date, language) or "цей день"
        day_prefix = "На" if day_label in {"сьогодні", "завтра", "післязавтра"} else "У"
        times = self._format_slot_times(self._display_slots(slots), language)
        if times:
            return f"{day_prefix} {day_label} {window_label} можу запропонувати {times}. Який час вам зручніший?"
        return f"{day_prefix} {day_label} {window_label} не бачу вільних слотів. Підкажіть інший час?"

    def _build_name_and_contact_request(self, language: str) -> str:
        fields = set(self._required_contact_fields())
        if fields == {"name", "phone"}:
            return "залиште, будь ласка, ваше ім’я та номер телефону"
        if fields == {"name", "email"}:
            return "залиште, будь ласка, ваше ім’я та email"
        if fields == {"phone"}:
            return "залиште, будь ласка, номер телефону"
        if fields == {"email"}:
            return "залиште, будь ласка, email"
        return "залиште, будь ласка, ваше ім’я та номер телефону або email"

    def _build_available_reply(self, language: str, start_dt: datetime) -> str:
        formatted = start_dt.strftime("%d.%m о %H:%M")
        return (
            f"Супер, слот {formatted} вільний. "
            f"Щоб підтвердити {self._appointment_label()}, {self._build_name_and_contact_request(language)}."
        )

    def _build_suggested_slot_accepted_reply(self, language: str, start_dt: datetime) -> str:
        return (
            f"Супер, тоді бронюємо {self._format_scheduled_time_for_reply(start_dt, language)}. "
            f"{self._build_name_and_contact_request(language).capitalize()}."
        )

    def _format_scheduled_time_for_reply(self, start_dt: datetime | None, language: str) -> str:
        if start_dt is None:
            return "домовлений час"
        local_dt = self._deserialize_pending_start_dt(start_dt)
        today = datetime.now(self.timezone).date()
        target = local_dt.date()
        if target == today:
            day_label = "сьогодні"
        elif target == today + timedelta(days=1):
            day_label = "завтра"
        elif target == today + timedelta(days=2):
            day_label = "післязавтра"
        else:
            day_label = local_dt.strftime("%d.%m")
        return f"{day_label} о {local_dt.strftime('%H:%M')}"

    def _build_confirmed_reply(
        self,
        language: str,
        start_dt: datetime | None = None,
        customer_name: str | None = None,
    ) -> str:
        label = self._appointment_label()
        if customer_name and start_dt:
            return f"Супер, {customer_name}, підтвердили {label} на {self._format_scheduled_time_for_reply(start_dt, language)} 🙌 Зв’яжемося з вами у цей час."
        if start_dt:
            return f"Супер, підтвердили {label} на {self._format_scheduled_time_for_reply(start_dt, language)} 🙌 Зв’яжемося з вами у цей час."
        if customer_name:
            return f"Супер, {customer_name}, {label} підтвердили 🙌 Зв’яжемося з вами у домовлений час."
        return f"Супер, {label} підтвердили 🙌 Зв’яжемося з вами у домовлений час."

    def _build_idempotent_already_confirmed_reply(self, language: str, start_dt: datetime) -> str:
        return f"У вас уже є підтверджений {self._appointment_label()} на {self._format_scheduled_time_for_reply(start_dt, language)}."

    def _build_missing_service_reply(self, language: str, *, pain_mentioned: bool = False) -> str:
        if pain_mentioned:
            return self._pain_acknowledgement_sentence() + "На яку послугу хочете записатися?"
        return "На яку послугу хочете записатися?"

    def _build_cancelled_reply(self, language: str) -> str:
        return "Добре, не бронюю. Якщо хочете, можете надіслати інший час."

    def _build_alternative_rejected_reply(self, language: str) -> str:
        return "Добре. Напишіть, будь ласка, інший зручний час або день."

    def _build_confirmed_cancelled_reply(self, language: str) -> str:
        return f"Добре, я скасував ваш {self._appointment_label()}. Якщо буде актуально — можемо запланувати інший час."

    def _build_cancel_handoff_reply(self, language: str) -> str:
        return f"Добре, передам команді, щоб {self._appointment_label()} скасували без зайвих дій з вашого боку."

    def _build_call_explanation_reply(self, language: str) -> str:
        if self.front_desk_config_service is None:
            return "На дзвінку ми коротко розберемо ваш кейс, задачі і підкажемо, як бот може працювати саме у вас."
        return f"На {self._appointment_label()} ми коротко уточнимо деталі і підкажемо наступний крок."

    def _build_availability_question_reply(
        self,
        language: str,
        slots_by_day: dict[str, list[datetime]],
    ) -> str:
        display_slots_by_day = self._display_slots_by_day(slots_by_day)
        tomorrow_times = self._format_slot_times(display_slots_by_day.get("tomorrow", []), language)
        day_after_times = self._format_slot_times(display_slots_by_day.get("day_after_tomorrow", []), language)

        if tomorrow_times and day_after_times:
            return (
                f"На завтра можу запропонувати {tomorrow_times}. "
                f"Також післязавтра {day_after_times}. Який варіант вам зручніший?"
            )
        if tomorrow_times:
            return f"На завтра можу запропонувати {tomorrow_times}. Підійде щось із цього чи перевірити інший день?"
        if day_after_times:
            return f"На післязавтра можу запропонувати {day_after_times}. Який час вам зручніший?"
        return (
            "Зараз не бачу підтверджених вільних слотів. "
            "Можете написати бажаний день і час, і я перевірю."
        )

    def _build_confirm_prompt_reply(self, language: str) -> str:
        return "Напишіть, будь ласка, «так», щоб підтвердити, або надішліть інший час."

    def _build_contact_retry_reply(self, language: str) -> str:
        return f"Щоб підтвердити {self._appointment_label()}, {self._build_name_and_contact_request(language)}."

    def _build_name_retry_reply(self, language: str) -> str:
        return "Дякую. А підкажіть, будь ласка, ваше ім’я?"

    def _build_contact_only_retry_reply(self, language: str, customer_name: str | None = None) -> str:
        fields = set(self._required_contact_fields())
        if fields == {"name", "phone"} or fields == {"phone"}:
            contact_label = "номер телефону"
        elif fields == {"name", "email"} or fields == {"email"}:
            contact_label = "email"
        else:
            contact_label = "номер телефону або email"
        if customer_name:
            return (
                f"Дякую, {customer_name}. А підкажіть, будь ласка, "
                f"{contact_label}?"
            )
        return f"Щоб підтвердити {self._appointment_label()}, залиште, будь ласка, {contact_label}."

    def _build_booking_status_pending_contact_reply(self, language: str) -> str:
        return (
            f"Ще ні, фінально підтверджу {self._appointment_label()} після контакту. "
            "Залиште, будь ласка, ваше ім’я та номер телефону або email."
        )

    def _build_another_time_reply(self, language: str, day_label: str | None = None) -> str:
        if day_label:
            return f"Добре, підберемо інший час. На котру іншу годину у {day_label} вам було б зручно?"
        return (
            f"Добре, підберемо інший час для {self._appointment_label()}. "
            "Підкажіть, будь ласка, який день і час вам зручний?"
        )

    def _build_unrelated_during_booking_reply(self, language: str, state: BookingState) -> str:
        if self.front_desk_config_service is None:
            if state == BookingState.WAITING_FOR_CONTACT:
                return (
                    "Коротко: це AI-бот для месенджерів, який відповідає на типові звернення "
                    "і допомагає доводити клієнтів до запису. Щоб продовжити підтвердження "
                    f"дзвінка, {self._build_name_and_contact_request(language)}."
                )
            return (
                "Коротко: це AI-бот для месенджерів, який відповідає на типові звернення "
                "і допомагає доводити клієнтів до запису. Для дзвінка підкажіть, будь ласка, "
                "зручний день і час."
            )
        safety = self.front_desk_config_service.get_safety()
        fallback = str(safety.get("unknown_fallback") or "Уточніть, будь ласка, що саме вас цікавить?")
        if state == BookingState.WAITING_FOR_CONTACT:
            return (
                f"{fallback} Щоб продовжити підтвердження "
                f"{self._appointment_label()}, {self._build_name_and_contact_request(language)}."
            )
        return (
            f"{fallback} Для {self._appointment_label()} підкажіть, будь ласка, "
            "зручний день і час."
        )

    def _looks_like_booking_status_question(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        status_markers = [
            "поставив дзвінок",
            "поставили дзвінок",
            "записав",
            "записали",
            "забронював",
            "забронювали",
            "підтвердив",
            "підтвердили",
            "дзвінок підтверджено",
            "call booked",
            "booked",
            "confirmed",
        ]
        return "?" in text and any(marker in normalized for marker in status_markers)

    def looks_like_booking_status_question(self, text: str) -> bool:
        return self._looks_like_booking_status_question(text)

    def get_confirmed_booking_status_reply(self, sender_id: str, language: str) -> str:
        completed_booking = self._get_completed_booking(sender_id) or {}
        start_dt = None
        if completed_booking.get("start_dt"):
            try:
                start_dt = self._deserialize_pending_start_dt(completed_booking["start_dt"])
            except Exception:
                logger.warning(
                    "confirmed booking start_dt deserialize failed sender_id=%s raw_start_dt=%r",
                    sender_id,
                    completed_booking.get("start_dt"),
                )
        if start_dt is not None:
            return (
                f"Так, {self._appointment_label()} підтверджено на {self._format_scheduled_time_for_reply(start_dt, language)} 🙌 "
                "Зв’яжемося з вами у цей час."
            )
        return f"Так, {self._appointment_label()} підтверджено 🙌 Зв’яжемося з вами у домовлений час."

    def _build_email_confirmed_reply(
        self,
        language: str,
        start_dt: datetime | None = None,
        customer_name: str | None = None,
    ) -> str:
        return self._build_confirmed_reply(language, start_dt, customer_name)

    def _build_phone_handoff_reply(self, language: str, start_dt: datetime | None = None) -> str:
        return self._build_confirmed_reply(language, start_dt)

    def _build_both_contacts_confirmed_reply(
        self,
        language: str,
        start_dt: datetime | None = None,
        customer_name: str | None = None,
    ) -> str:
        return self._build_confirmed_reply(language, start_dt, customer_name)

    def _build_manual_followup_reply(self, language: str) -> str:
        return "Супер, зафіксували ваш запит 🙌 Ми зв’яжемося з вами, щоб підтвердити час"

    def _build_create_failed_reply(self, language: str, start_dt: datetime | None = None) -> str:
        return self._build_manual_followup_reply(language)

    def _normalize_phone(self, raw_phone: str) -> str:
        compact = re.sub(r"[^\d+]", "", raw_phone.strip())
        if compact.startswith("++"):
            compact = compact[1:]
        return compact

    def _find_next_available_slot(self, requested_dt: datetime) -> datetime | None:
        for hours in [1, 2, 3, 4]:
            next_dt = requested_dt + timedelta(hours=hours)
            if not self._is_within_business_hours(next_dt, self._booking_duration_minutes()):
                continue
            if self.calendar_service.check_specific_time_availability(
                next_dt,
                self._booking_duration_minutes(),
            ):
                return next_dt
        return None

    def _looks_like_nearest_availability_request(self, text: str) -> bool:
        """Whether `text` is asking for the soonest bookable slot ("найближчий
        час", "коли найближче?", "earliest available") rather than naming a
        specific day/time. Distinct from `_looks_like_availability_question`,
        which answers "what times are generally free" with a couple of fixed
        illustrative candidates -- this one drives an actual forward Calendar
        search (see `_find_nearest_available_slot`), so it must win whenever
        both could match the same message.
        """
        normalized = self._normalize_booking_text(text)
        if re.search(r"\bнайближч\w*\b", normalized):
            return True
        english_markers = [
            "earliest available",
            "earliest slot",
            "earliest appointment",
            "earliest time",
            "next available",
            "as soon as possible",
        ]
        if any(marker in normalized for marker in english_markers):
            return True
        return bool(re.search(r"\basap\b", normalized))

    def _find_nearest_available_slot(
        self,
        *,
        search_from: datetime | None = None,
        horizon_days: int = 14,
    ) -> datetime | None:
        """Searches forward, day by day, through valid business hours for the
        first Calendar-verified available slot -- reusing the same per-slot
        check `_get_verified_time_window_slots` already uses, just walking
        multiple days instead of one fixed date. Closed days are skipped via
        `_business_hours_status_for_date`; a slot is never trusted without a
        live `check_specific_time_availability` call.
        """
        duration = self._booking_duration_minutes()
        now = search_from or datetime.now(self.timezone)
        for day_offset in range(horizon_days):
            target_date = now.date() + timedelta(days=day_offset)
            status, window = self._business_hours_status_for_date(target_date)
            if status == "legacy_open":
                window = (time(0, 0), time(23, 59))
            elif status != "open" or window is None:
                continue

            day_start = datetime.combine(target_date, window[0], tzinfo=self.timezone)
            day_end = datetime.combine(target_date, window[1], tzinfo=self.timezone)
            cursor = day_start
            if day_offset == 0 and now > day_start:
                elapsed_slots = int((now - day_start).total_seconds() // 1800) + 1
                cursor = day_start + timedelta(minutes=30 * elapsed_slots)

            while cursor + timedelta(minutes=duration) <= day_end:
                if self._is_within_business_hours(cursor, duration):
                    try:
                        if self.calendar_service.check_specific_time_availability(
                            cursor,
                            duration_minutes=duration,
                        ):
                            return cursor
                    except Exception:
                        logger.exception(
                            "nearest-availability check failed start_dt=%s",
                            cursor.isoformat(),
                        )
                cursor += timedelta(minutes=30)
        return None

    def _build_nearest_availability_unavailable_reply(self, language: str) -> str:
        return (
            "Наразі не бачу вільного часу найближчим часом. "
            "Підкажіть, будь ласка, зручний день і час, і ми перевіримо запис."
        )

    def handle_nearest_availability_request(
        self,
        sender_id: str,
        message_text: str,
        source_channel: str | None = None,
        current_service_id: str | None = None,
        current_service_name: str | None = None,
    ) -> Dict[str, Any]:
        """Handles "найближчий час"/"earliest available" style requests: find
        and offer the first real, Calendar-verified slot instead of asking
        the user to name a day and time themselves. Reuses the existing
        booking-state/pending-confirmation machinery so the normal
        acceptance/contact safety rules apply unchanged -- this never
        creates an event by itself.
        """
        language = self._detect_language(message_text)
        previous_pending = self._get_pending_confirmation(sender_id) or {}
        current_service_id = current_service_id or previous_pending.get("current_service_id")
        current_service_name = current_service_name or previous_pending.get("current_service_name")
        pain_mentioned = self._looks_like_dental_pain_mention(message_text)

        if self._service_required_for_booking() and not current_service_id:
            return self._save_missing_service_context(
                sender_id=sender_id,
                language=language,
                message_text=message_text,
                source_channel=source_channel or previous_pending.get("source_channel"),
                contact_details=self._empty_contact_details(),
            )

        slot = self._find_nearest_available_slot()
        if slot is None:
            self._save_booking_state(
                sender_id,
                state=BookingState.WAITING_FOR_TIME,
                language=language,
                source_channel=source_channel or previous_pending.get("source_channel"),
                context_summary=message_text[:280],
            )
            pending = self._get_pending_confirmation(sender_id) or {}
            if current_service_id:
                pending["current_service_id"] = current_service_id
            if current_service_name:
                pending["current_service_name"] = current_service_name
            self._save_pending_confirmation(sender_id, pending)
            reply_text = self._build_nearest_availability_unavailable_reply(language)
            if pain_mentioned:
                reply_text = self._pain_acknowledgement_sentence() + reply_text
            return {
                "status": "nearest_availability_unavailable",
                "reply_text": reply_text,
                "requires_confirmation": False,
                "event_created": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
            }

        early_contact_details = self._extract_trailing_contact_details(message_text)
        requested_day_label = self._format_date_label_for_reply(slot.date(), language)
        self._save_booking_state(
            sender_id,
            state=BookingState.WAITING_FOR_TIME,
            language=language,
            source_channel=source_channel or previous_pending.get("source_channel"),
            context_summary=message_text[:280],
            requested_date=slot.date(),
            requested_day_label=requested_day_label,
            customer_name=early_contact_details["customer_name"],
            contact_email=early_contact_details["email"],
            contact_phone=early_contact_details["phone"],
        )
        pending = self._get_pending_confirmation(sender_id) or {}
        day_key = "selected_day"
        pending["availability_context"] = True
        if current_service_id:
            pending["current_service_id"] = current_service_id
        if current_service_name:
            pending["current_service_name"] = current_service_name
        pending["suggested_slots"] = [
            {"day_key": day_key, "start_dt": self._serialize_pending_start_dt(slot)}
        ]
        pending["last_suggested_day"] = day_key
        self._save_pending_confirmation(sender_id, pending)

        day_label = requested_day_label or "цей день"
        day_prefix = "На" if day_label in {"сьогодні", "завтра", "післязавтра"} else "У"
        time_label = self._format_slot_times([slot], language)
        reply_text = f"{day_prefix} {day_label} найближчий вільний час {time_label}. Записати вас на цей час?"
        if pain_mentioned:
            reply_text = self._pain_acknowledgement_sentence() + reply_text

        return {
            "status": "nearest_availability_suggested",
            "reply_text": reply_text,
            "requires_confirmation": False,
            "event_created": False,
            "booking_state": BookingState.WAITING_FOR_TIME.value,
            "requested_date": slot.date().isoformat(),
            "suggested_slots": pending["suggested_slots"],
            "start_dt": slot.isoformat(),
        }

    def _extract_daypart(self, text: str) -> dict[str, Any] | None:
        normalized = self._normalize_booking_text(text)
        if "зранку" in normalized or "вранці" in normalized:
            return {"label": "зранку", "start": time(9, 0), "end": time(12, 0)}
        if "ввечері" in normalized or "увечері" in normalized or "вечір" in normalized:
            return {"label": "ввечері", "start": time(17, 0), "end": time(19, 0)}
        if "після обіду" in normalized or "по обіді" in normalized:
            return {"label": "після обіду", "start": time(12, 0), "end": time(17, 0)}
        return None

    def _parse_window_time(self, raw_hour: str, raw_minute: str | None = None) -> time | None:
        hour = int(raw_hour)
        minute = int(raw_minute) if raw_minute is not None else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
        return None

    def _format_time_window_time(self, value: time) -> str:
        return value.strftime("%H:%M")

    def _extract_time_window(self, text: str) -> dict[str, Any] | None:
        normalized = self._normalize_booking_text(text).replace("–", "-").replace("—", "-")
        time_pattern = r"(\d{1,2})(?::(\d{2}))?"

        range_patterns = [
            rf"\bз\s+{time_pattern}(?:\s+годин[иу]?)?\s+до\s+{time_pattern}\b",
            rf"\bміж\s+{time_pattern}\s+(?:і|та)\s+{time_pattern}\b",
            rf"\b{time_pattern}\s*-\s*{time_pattern}\b",
        ]
        for pattern in range_patterns:
            match = re.search(pattern, normalized)
            if match:
                start = self._parse_window_time(match.group(1), match.group(2))
                end = self._parse_window_time(match.group(3), match.group(4))
                if start is not None and end is not None and start < end:
                    return {
                        "label": f"з {self._format_time_window_time(start)} до {self._format_time_window_time(end)}",
                        "start": start,
                        "end": end,
                    }
                return None

        match = re.search(rf"\bпісля\s+{time_pattern}\b", normalized)
        if not match:
            match = re.search(rf"\bз\s+{time_pattern}\s+годин[иу]?\b", normalized)
        if match:
            start = self._parse_window_time(match.group(1), match.group(2))
            if start is not None:
                return {
                    "label": f"після {self._format_time_window_time(start)}",
                    "start": start,
                    "end": None,
                }

        match = re.search(rf"\bдо\s+{time_pattern}\b", normalized)
        if match:
            end = self._parse_window_time(match.group(1), match.group(2))
            if end is not None:
                return {
                    "label": f"до {self._format_time_window_time(end)}",
                    "start": None,
                    "end": end,
                }

        return None

    def _get_verified_time_window_slots(
        self,
        *,
        requested_date: date,
        time_window: dict[str, Any],
    ) -> tuple[str, list[datetime]]:
        status, business_window = self._business_hours_status_for_date(requested_date)
        if status != "open" or business_window is None:
            return status, []

        duration = self._booking_duration_minutes()
        requested_start = time_window.get("start") or business_window[0]
        requested_end = time_window.get("end") or business_window[1]
        window_start = max(business_window[0], requested_start)
        window_end = min(business_window[1], requested_end)
        if window_start >= window_end:
            return "outside_business_hours", []

        cursor = datetime.combine(requested_date, window_start, tzinfo=self.timezone)
        end_dt = datetime.combine(requested_date, window_end, tzinfo=self.timezone)
        slots: list[datetime] = []

        while cursor + timedelta(minutes=duration) <= end_dt:
            if self._is_within_business_hours(cursor, duration):
                try:
                    if self.calendar_service.check_specific_time_availability(
                        cursor,
                        duration_minutes=duration,
                    ):
                        slots.append(cursor)
                        if len(slots) >= 3:
                            break
                except Exception:
                    logger.exception("time window availability check failed start_dt=%s", cursor.isoformat())
            cursor += timedelta(minutes=30)

        return "open", slots

    def _get_verified_daypart_slots(
        self,
        *,
        requested_date: date,
        daypart: dict[str, Any],
    ) -> tuple[str, list[datetime]]:
        return self._get_verified_time_window_slots(
            requested_date=requested_date,
            time_window=daypart,
        )

    def _suggest_daypart_slots(
        self,
        sender_id: str,
        *,
        language: str,
        message_text: str,
        source_channel: str | None,
        requested_date: date,
        requested_day_label: str | None,
        daypart: dict[str, Any],
        current_service_id: str | None = None,
        current_service_name: str | None = None,
    ) -> Dict[str, Any]:
        status, slots = self._get_verified_daypart_slots(
            requested_date=requested_date,
            daypart=daypart,
        )

        if status != "open":
            self._save_booking_state(
                sender_id,
                state=BookingState.WAITING_FOR_TIME,
                language=language,
                source_channel=source_channel,
                context_summary=message_text[:280],
                requested_date=requested_date,
                requested_day_label=requested_day_label,
            )
            pending = self._get_pending_confirmation(sender_id) or {}
            if current_service_id:
                pending["current_service_id"] = current_service_id
            if current_service_name:
                pending["current_service_name"] = current_service_name
            pending["availability_context"] = True
            self._save_pending_confirmation(sender_id, pending)
            return {
                "status": "outside_business_hours",
                "reply_text": self._build_outside_business_hours_reply(language),
                "requires_confirmation": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
                "requested_date": requested_date.isoformat(),
            }

        early_contact_details = self._extract_trailing_contact_details(message_text)
        self._save_booking_state(
            sender_id,
            state=BookingState.WAITING_FOR_TIME,
            language=language,
            source_channel=source_channel,
            context_summary=message_text[:280],
            requested_date=requested_date,
            requested_day_label=requested_day_label,
            customer_name=early_contact_details["customer_name"],
            contact_email=early_contact_details["email"],
            contact_phone=early_contact_details["phone"],
        )
        pending = self._get_pending_confirmation(sender_id) or {}
        day_key = "selected_day"
        pending["availability_context"] = True
        if current_service_id:
            pending["current_service_id"] = current_service_id
        if current_service_name:
            pending["current_service_name"] = current_service_name
        pending["suggested_slots"] = [
            {
                "day_key": day_key,
                "start_dt": self._serialize_pending_start_dt(slot),
            }
            for slot in slots
        ]
        pending["last_suggested_day"] = day_key
        self._save_pending_confirmation(sender_id, pending)

        return {
            "status": "daypart_slots_suggested",
            "reply_text": self._build_daypart_slots_reply(
                language,
                requested_date=requested_date,
                requested_day_label=requested_day_label,
                daypart_label=daypart["label"],
                slots=slots,
            ),
            "requires_confirmation": False,
            "booking_state": BookingState.WAITING_FOR_TIME.value,
            "requested_date": requested_date.isoformat(),
            "suggested_slots": pending["suggested_slots"],
        }

    def _suggest_time_window_slots(
        self,
        sender_id: str,
        *,
        language: str,
        message_text: str,
        source_channel: str | None,
        requested_date: date,
        requested_day_label: str | None,
        time_window: dict[str, Any],
        current_service_id: str | None = None,
        current_service_name: str | None = None,
    ) -> Dict[str, Any]:
        status, slots = self._get_verified_time_window_slots(
            requested_date=requested_date,
            time_window=time_window,
        )

        if status != "open":
            self._save_booking_state(
                sender_id,
                state=BookingState.WAITING_FOR_TIME,
                language=language,
                source_channel=source_channel,
                context_summary=message_text[:280],
                requested_date=requested_date,
                requested_day_label=requested_day_label,
            )
            pending = self._get_pending_confirmation(sender_id) or {}
            if current_service_id:
                pending["current_service_id"] = current_service_id
            if current_service_name:
                pending["current_service_name"] = current_service_name
            pending["availability_context"] = True
            self._save_pending_confirmation(sender_id, pending)
            return {
                "status": "outside_business_hours",
                "reply_text": self._build_outside_business_hours_reply(language),
                "requires_confirmation": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
                "requested_date": requested_date.isoformat(),
            }

        early_contact_details = self._extract_trailing_contact_details(message_text)
        self._save_booking_state(
            sender_id,
            state=BookingState.WAITING_FOR_TIME,
            language=language,
            source_channel=source_channel,
            context_summary=message_text[:280],
            requested_date=requested_date,
            requested_day_label=requested_day_label,
            customer_name=early_contact_details["customer_name"],
            contact_email=early_contact_details["email"],
            contact_phone=early_contact_details["phone"],
        )
        pending = self._get_pending_confirmation(sender_id) or {}
        day_key = "selected_day"
        pending["availability_context"] = True
        if current_service_id:
            pending["current_service_id"] = current_service_id
        if current_service_name:
            pending["current_service_name"] = current_service_name
        pending["suggested_slots"] = [
            {
                "day_key": day_key,
                "start_dt": self._serialize_pending_start_dt(slot),
            }
            for slot in slots
        ]
        pending["last_suggested_day"] = day_key
        pending["time_window"] = {
            "label": time_window["label"],
            "start": time_window["start"].isoformat() if time_window.get("start") else None,
            "end": time_window["end"].isoformat() if time_window.get("end") else None,
        }
        self._save_pending_confirmation(sender_id, pending)

        return {
            "status": "time_window_slots_suggested",
            "reply_text": self._build_time_window_slots_reply(
                language,
                requested_date=requested_date,
                requested_day_label=requested_day_label,
                window_label=time_window["label"],
                slots=slots,
            ),
            "requires_confirmation": False,
            "booking_state": BookingState.WAITING_FOR_TIME.value,
            "requested_date": requested_date.isoformat(),
            "suggested_slots": pending["suggested_slots"],
        }

    def _extract_contact_details(self, text: str) -> Dict[str, Any]:
        emails = []
        seen_emails = set()
        for match in self.EMAIL_RE.findall(text):
            email = match.strip().lower()
            if email and email not in seen_emails:
                seen_emails.add(email)
                emails.append(email)

        phones = []
        seen_phones = set()
        for raw_phone in self.PHONE_RE.findall(text):
            phone = self._normalize_phone(raw_phone)
            digits_only = re.sub(r"\D", "", phone)
            if len(digits_only) < 9:
                continue
            if phone and phone not in seen_phones:
                seen_phones.add(phone)
                phones.append(phone)

        primary_email = emails[0] if emails else None
        primary_phone = phones[0] if phones else None
        customer_name = self._extract_customer_name(
            text=text,
            emails=emails,
            phones=phones,
        )

        return {
            "email": primary_email,
            "phone": primary_phone,
            "customer_name": customer_name,
            "emails": emails,
            "phones": phones,
            "has_email": bool(primary_email),
            "has_phone": bool(primary_phone),
            "has_name": bool(customer_name),
        }

    def extract_contact_details(self, text: str) -> Dict[str, Any]:
        return self._extract_contact_details(text)

    def _empty_contact_details(self) -> Dict[str, Any]:
        return {
            "email": None,
            "phone": None,
            "customer_name": None,
            "emails": [],
            "phones": [],
            "has_email": False,
            "has_phone": False,
            "has_name": False,
        }

    def _extract_trailing_contact_details(self, text: str) -> Dict[str, Any]:
        parts = [part.strip() for part in re.split(r"[,;\n\r]+", text) if part.strip()]
        if len(parts) < 2:
            return self._empty_contact_details()
        contact_index = next(
            (
                index
                for index, part in enumerate(parts)
                if self.PHONE_RE.search(part) or self.EMAIL_RE.search(part)
            ),
            None,
        )
        if contact_index is None:
            return self._empty_contact_details()

        contact_details = self._extract_contact_details(parts[contact_index])
        if contact_index > 0:
            name_part = parts[contact_index - 1]
            if not re.search(
                r"\b(?:хочу|запишіть|запиши|записатись|записатися|на|в|у|о|at|book)\b",
                name_part.lower(),
            ):
                name_details = self._extract_contact_details(name_part)
                if name_details["has_name"]:
                    contact_details["customer_name"] = name_details["customer_name"]
        if not contact_details["has_phone"] and not contact_details["has_email"]:
            return self._empty_contact_details()
        contact_details["has_name"] = bool(contact_details["customer_name"])
        return contact_details

    def _extract_anchored_contact_details(self, text: str) -> Dict[str, Any]:
        trailing = self._extract_trailing_contact_details(text)
        if trailing["has_phone"] or trailing["has_email"]:
            return trailing

        anchor_match = self.PHONE_RE.search(text) or self.EMAIL_RE.search(text)
        if anchor_match is None:
            return self._empty_contact_details()

        contact_details = self._extract_contact_details(anchor_match.group(0))
        before_anchor = text[:anchor_match.start()].strip(" ,;:-\n\r")
        after_anchor = text[anchor_match.end():].strip(" ,;:-\n\r")

        for candidate in (before_anchor, after_anchor):
            if not candidate:
                continue
            if re.search(r"\b(?:хочу|запишіть|запиши|записатись|записатися|на|в|у|о|at|book)\b", candidate.lower()):
                tail_name_match = re.search(
                    r"([A-ZА-ЯІЇЄҐ][A-Za-zА-Яа-яІіЇїЄєҐґ'’`-]{1,}"
                    r"(?:\s+[A-ZА-ЯІЇЄҐ][A-Za-zА-Яа-яІіЇїЄєҐґ'’`-]{1,}){0,2})$",
                    candidate,
                )
                if tail_name_match:
                    contact_details["customer_name"] = tail_name_match.group(1)
                    break
                continue
            name_details = self._extract_contact_details(candidate)
            if name_details["has_name"]:
                contact_details["customer_name"] = name_details["customer_name"]
                break

        contact_details["has_name"] = bool(contact_details["customer_name"])
        return contact_details

    def _looks_like_standalone_customer_name_input(self, text: str) -> bool:
        normalized = " ".join(text.strip().split())
        if not normalized or "?" in normalized or re.search(r"\d", normalized):
            return False
        lowered = normalized.lower()
        if (
            self._is_confirmation_text(lowered)
            or self._is_rejection(lowered)
            or self._looks_like_another_time_request(lowered)
            or self._looks_like_unrelated_question_during_booking(lowered)
            or self._parse_time_only(lowered) is not None
            or self._extract_time_window(lowered) is not None
            or self._extract_daypart(lowered) is not None
            or self._extract_requested_date(lowered) is not None
        ):
            return False
        if re.search(
            r"\b(?:хочу|запишіть|запиши|записатись|записатися|запис|можна|давайте|"
            r"не\s+можу|не\s+виходить|зайнятий|будь\s+ласка|дякую|скільки|де|"
            r"послуг|чистк|гігієн|консультац|відбілюван|карієс|прийом)\b",
            lowered,
        ):
            return False
        if re.match(r"(?i)^(?:мене\s+звати|моє\s+ім'?я|ім'?я)\s+\S+", normalized):
            return True
        if re.match(
            r"(?i)^я\s+[A-Za-zА-Яа-яІіЇїЄєҐґ'’`-]{2,}"
            r"(?:\s+[A-Za-zА-Яа-яІіЇїЄєҐґ'’`-]{2,}){0,2}$",
            normalized,
        ):
            return True
        return bool(
            re.fullmatch(
                r"[A-ZА-ЯІЇЄҐ][A-Za-zА-Яа-яІіЇїЄєҐґ'’`-]{1,}"
                r"(?:\s+[A-ZА-ЯІЇЄҐ][A-Za-zА-Яа-яІіЇїЄєҐґ'’`-]{1,}){0,2}",
                normalized,
            )
        )

    def _extract_waiting_for_contact_details(self, text: str) -> Dict[str, Any]:
        if self.PHONE_RE.search(text) or self.EMAIL_RE.search(text):
            return self._extract_anchored_contact_details(text)

        if not self._looks_like_standalone_customer_name_input(text):
            return self._empty_contact_details()

        contact_details = self._extract_contact_details(text)
        explicit_i_match = re.match(
            r"(?i)^я\s+([A-Za-zА-Яа-яІіЇїЄєҐґ'’`-]{2,}"
            r"(?:\s+[A-Za-zА-Яа-яІіЇїЄєҐґ'’`-]{2,}){0,2})$",
            " ".join(text.strip().split()),
        )
        if explicit_i_match:
            contact_details["customer_name"] = explicit_i_match.group(1)
            contact_details["has_name"] = True
        return contact_details

    def _merge_contact_details_with_pending(
        self,
        contact_details: Dict[str, Any],
        pending: dict[str, Any] | None,
        message_text: str,
    ) -> Dict[str, Any]:
        if not pending:
            return contact_details

        merged = dict(contact_details)
        if (
            not contact_details["email"]
            and not contact_details["phone"]
            and (
                self._parse_time_only(message_text) is not None
                or self._extract_suggested_slot_selection_time(message_text) is not None
                or self._extract_hour_only(message_text) is not None
            )
        ):
            merged["customer_name"] = None
        merged["customer_name"] = merged["customer_name"] or pending.get("customer_name")
        merged["email"] = contact_details["email"] or pending.get("contact_email")
        merged["phone"] = contact_details["phone"] or pending.get("contact_phone")
        merged["has_name"] = bool(merged["customer_name"])
        merged["has_email"] = bool(merged["email"])
        merged["has_phone"] = bool(merged["phone"])
        return merged

    def _is_non_customer_name_text(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        normalized = re.sub(r"[.!?…]+$", "", normalized).strip()
        normalized = re.sub(r"([аеєиіїоуюя])\1+", r"\1", normalized)
        if self._is_confirmation_text(normalized) or self._looks_like_another_time_request(normalized):
            return True
        if self._extract_hour_only(normalized) is not None:
            return True
        if self._extract_requested_date(normalized) is not None:
            # A message that is purely a date/weekday reference ("у четвер",
            # "на 3 вересня") is never a customer name, even when it arrives
            # while a name is still pending -- but a message that combines a
            # date with an actual name ("Дмитро, у четвер") must still let
            # the name through, so only short-circuit when nothing besides
            # the date phrase and common connector words remains.
            remainder = normalized
            for marker in list(self._weekday_map().keys()) + list(self._ukrainian_month_map().keys()):
                remainder = re.sub(rf"\b{re.escape(marker)}\b", " ", remainder)
            remainder = re.sub(
                r"\b(?:у|в|на|щодо|до|через|о|об|го|ого|сьогодні|завтра|післязавтра|today|tomorrow)\b",
                " ",
                remainder,
            )
            remainder = re.sub(r"[^\w\sа-яіїєґё]", " ", remainder, flags=re.IGNORECASE)
            remainder = re.sub(r"\d+", " ", remainder)
            if not re.search(r"[a-zа-яіїєґ]{2,}", remainder, flags=re.IGNORECASE):
                return True
        if self._looks_like_booking_correction(normalized) and self._extract_corrected_time(normalized):
            return True
        if self._is_rejection(normalized):
            return True
        if re.search(r"\b(?:ні|не|no|not)\b.*\b\d{1,2}(?::00)?\b", normalized):
            return True
        if re.search(r"\b(?:краще|тоді|давай|давайте)\b.*\b(?:після|до)\s+\d{1,2}(?::\d{2})?\b", normalized):
            return True
        if re.search(r"\b(?:передумав|передумала|передумали|потім|пізніше)\b", normalized):
            return True
        compact = re.sub(r"[^\w\sа-яіїєґё]", " ", normalized, flags=re.IGNORECASE)
        compact = " ".join(compact.split())
        if (
            len(compact.split()) <= 4
            and re.search(
                r"\b(?:так|ок|окей|давай|давайте|тоді)?\s*\d{1,2}(?::00)?\s*(?:норм|гуд|добре)?\b",
                compact,
            )
            and any(marker in compact for marker in ["так", "ок", "давай", "тоді", "норм", "гуд"])
        ):
            return True
        non_names = {
            "не",
            "ні",
            "не хочу",
            "краще",
            "давай",
            "давайте",
            "ну давай",
            "тоді",
            "ок",
            "окей",
            "гуд",
            "ага",
            "інший",
            "інший час",
            "інша година",
            "цікаво",
            "гаразд цікаво",
            "ок цікаво",
            "окей цікаво",
            "звучить цікаво",
            "можливо цікаво",
            "хм цікаво",
            "так цікаво",
            "тоді на буде гуд",
            "на буде гуд",
            "так норм",
            "так гуд",
            "ок норм",
            "ок гуд",
        }
        if normalized in non_names:
            return True
        service_control_markers = [
            "краще",
            "не",
            "ні",
            "не консультац",
            "не запис",
        ]
        service_words = [
            "чистк",
            "гігієн",
            "відбілюван",
            "консультац",
            "карієс",
            "дитяч",
            "протез",
            "імплант",
        ]
        return (
            len(compact.split()) <= 5
            and any(marker in compact for marker in service_control_markers)
            and any(word in compact for word in service_words)
        )

    def _extract_customer_name(
        self,
        *,
        text: str,
        emails: list[str],
        phones: list[str],
    ) -> str | None:
        if self._is_confirmation_text(text):
            return None
        if self._is_non_customer_name_text(text):
            return None

        candidate = text.strip()
        for email in emails:
            candidate = re.sub(re.escape(email), " ", candidate, flags=re.IGNORECASE)
        for phone in phones:
            candidate = candidate.replace(phone, " ")
            digits = re.sub(r"\D", "", phone)
            if digits:
                candidate = re.sub(r"[\+\d][\d\-\s\(\)]{6,}\d", " ", candidate)

        explicit_name_match = re.search(
            r"\b(?:мене\s+звати|моє\s+ім'?я|ім'?я)\s+([A-Za-zА-Яа-яІіЇїЄєҐґ'’`\-\s]{2,60})",
            candidate,
            flags=re.IGNORECASE,
        )
        if explicit_name_match:
            explicit_candidate = explicit_name_match.group(1)
            explicit_candidate = re.split(r"[.!?…,\n\r]", explicit_candidate, maxsplit=1)[0]
            explicit_candidate = re.sub(r"[^A-Za-zА-Яа-яІіЇїЄєҐґ'’`\-\s]", " ", explicit_candidate)
            explicit_candidate = " ".join(explicit_candidate.split()).strip(" -'’`")
            explicit_words = [
                word.strip("-'’`")
                for word in explicit_candidate.split()[:3]
                if len(word.strip("-'’`")) >= 2
            ]
            if explicit_words:
                return " ".join(explicit_words)

        if "?" in candidate:
            return None

        candidate = re.sub(
            r"\b(мене\s+звати|моє\s+ім'?я|ім'?я|мій\s+номер|номер|телефон|phone|email|емейл|пошта)\b",
            " ",
            candidate,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(r"[^A-Za-zА-Яа-яІіЇїЄєҐґ'’`\-\s]", " ", candidate)
        candidate = " ".join(candidate.split()).strip(" -'’`")

        if not candidate:
            return None

        words = candidate.split()
        name_words = []
        for word in words[:3]:
            if len(word.strip("-'’`")) < 2:
                continue
            name_words.append(word.strip("-'’`"))

        if not name_words:
            return None

        return " ".join(name_words)

    def _looks_like_unrelated_question_during_booking(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        if not normalized:
            return False

        markers = [
            "привіт",
            "вітаю",
            "доброго дня",
            "добрий день",
            "що це",
            "що це у вас",
            "що це за сервіс",
            "за сервіс",
            "що ви робите",
            "чим займаєтесь",
            "скільки коштує",
            "ціна",
            "вартість",
            "канали",
            "instagram",
            "facebook",
            "whatsapp",
            "telegram",
        ]
        return "?" in text or any(marker in normalized for marker in markers)

    def _looks_like_another_time_request(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        markers = [
            "інший час",
            "інший слот",
            "інший варіант",
            "давайте інший",
            "давай інший",
            "можна інший",
            "іншу годину",
            "раніше",
            "пізніше",
            "another time",
            "another slot",
            "different time",
            "earlier",
            "later",
        ]
        return any(marker in normalized for marker in markers)

    def _match_suggested_slot_acceptance(self, text: str, pending: dict[str, Any]) -> datetime | None:
        try:
            suggested_start = self._get_pending_start_dt(pending)
        except Exception:
            return None
        if suggested_start is None:
            return None

        if self.EMAIL_RE.search(text) or self.PHONE_RE.search(text):
            return None

        if self._is_confirmation_text(text):
            return suggested_start

        normalized = " ".join(text.strip().lower().split())
        normalized = re.sub(r"[^\w\sа-яіїєґё:]", " ", normalized, flags=re.IGNORECASE)
        normalized = " ".join(normalized.split())
        accept_markers = [
            "так",
            "підійде",
            "підходить",
            "підходе",
            "буде гуд",
            "гуд",
            "норм",
            "нормально",
            "добре",
            "ок",
            "окей",
            "давайте",
            "давай",
            "тоді",
            "works",
            "that works",
            "yes",
            "okay",
        ]
        if not any(marker in normalized for marker in accept_markers):
            return None

        candidates = self._parse_time_candidates(text)
        for hour_match in re.finditer(r"\b(\d{1,2})(?::00)?\b", normalized):
            hour = int(hour_match.group(1))
            if 0 <= hour <= 23:
                candidate = time(hour=hour, minute=0)
                if candidate not in candidates:
                    candidates.append(candidate)

        for candidate in candidates:
            if candidate.hour == suggested_start.hour and candidate.minute == suggested_start.minute:
                return suggested_start

        return None

    def _save_captured_contact(
        self,
        sender_id: str,
        *,
        email: str | None,
        phone: str | None,
        customer_name: str | None = None,
        start_dt: datetime | None = None,
    ) -> None:
        self.captured_contacts[sender_id] = {
            "customer_name": customer_name,
            "email": email,
            "phone": phone,
            "start_dt": self._serialize_pending_start_dt(start_dt) if start_dt else None,
        }

    def has_confirmed_booking(self, sender_id: str) -> bool:
        return self._has_completed_booking(sender_id)

    def get_call_explanation_reply(self, language: str) -> str:
        return self._build_call_explanation_reply(language)

    def get_availability_question_reply(self, language: str) -> str:
        return self._build_availability_question_reply(
            language=language,
            slots_by_day=self._get_suggested_slots_by_day(),
        )

    def handle_availability_question(
        self,
        sender_id: str,
        message_text: str,
        source_channel: str | None = None,
    ) -> Dict[str, Any]:
        language = self._detect_language(message_text)
        requested_day_key = self._detect_requested_day_key(message_text)
        verified_slots_by_day = self._get_suggested_slots_by_day()
        if requested_day_key is not None:
            slots_by_day = self._display_slots_by_day(
                {requested_day_key: verified_slots_by_day.get(requested_day_key, [])}
            )
        else:
            slots_by_day = self._display_slots_by_day(verified_slots_by_day)

        self._save_booking_state(
            sender_id,
            state=BookingState.WAITING_FOR_TIME,
            language=language,
            source_channel=source_channel,
            context_summary=message_text[:280],
        )

        pending = self._get_pending_confirmation(sender_id) or {}
        pending["availability_context"] = True
        pending["suggested_slots"] = [
            {
                "day_key": day_key,
                "start_dt": self._serialize_pending_start_dt(slot),
            }
            for day_key, slots in slots_by_day.items()
            for slot in slots
        ]
        pending["last_suggested_day"] = requested_day_key or next(iter(slots_by_day), "tomorrow")
        self._save_pending_confirmation(sender_id, pending)

        reply_text = (
            self._build_day_slots_reply(
                language,
                requested_day_key,
                slots_by_day.get(requested_day_key, []),
            )
            if requested_day_key is not None
            else self._build_availability_question_reply(language, slots_by_day)
        )

        return {
            "status": "availability_suggested",
            "reply_text": reply_text,
            "booking_state": BookingState.WAITING_FOR_TIME.value,
            "suggested_slots": pending["suggested_slots"],
        }

    def handle_active_booking_availability_question(
        self,
        sender_id: str,
        message_text: str,
        source_channel: str | None = None,
    ) -> Dict[str, Any] | None:
        pending = self._get_pending_confirmation(sender_id)
        if not pending:
            return None

        language = pending.get("language") or self._detect_language(message_text)
        partial_date = self._extract_requested_date(message_text)
        requested_day_label = None

        if partial_date is not None:
            requested_date = partial_date["date"]
            requested_day_label = partial_date.get("day_label")
        elif pending.get("requested_date"):
            try:
                requested_date = self._deserialize_pending_requested_date(
                    pending.get("requested_date")
                )
            except Exception:
                logger.warning(
                    "invalid pending requested_date ignored during availability question sender_id=%s raw_requested_date=%r",
                    sender_id,
                    pending.get("requested_date"),
                )
                return None
            requested_day_label = pending.get("requested_day_label")
        else:
            return None

        if requested_date is None:
            return None

        return self._suggest_time_window_slots(
            sender_id,
            language=language,
            message_text=message_text,
            source_channel=source_channel or pending.get("source_channel"),
            requested_date=requested_date,
            requested_day_label=requested_day_label,
            time_window={
                "label": "вільний час",
                "start": None,
                "end": None,
            },
        )

    def cancel_confirmed_booking(self, sender_id: str, message_text: str) -> Dict[str, Any]:
        language = self._detect_language(message_text)
        completed_booking = self._get_completed_booking(sender_id) or {}
        calendar_event_id = completed_booking.get("calendar_event_id") or completed_booking.get("event_id")

        if calendar_event_id:
            try:
                self.calendar_service.delete_event(calendar_event_id)
            except Exception:
                logger.exception(
                    "calendar event deletion failed sender_id=%s calendar_event_id=%s",
                    sender_id,
                    calendar_event_id,
                )
                return {
                    "status": "cancel_handoff",
                    "reply_text": self._build_cancel_handoff_reply(language),
                    "booking_state": BookingState.NONE.value,
                }

        self._clear_completed_booking(sender_id)
        self._clear_pending_confirmation(sender_id)
        return {
            "status": "cancelled",
            "reply_text": self._build_confirmed_cancelled_reply(language),
            "booking_state": BookingState.NONE.value,
        }

    def _mark_booking_completed(
        self,
        sender_id: str,
        *,
        start_dt: datetime | None,
        email: str | None,
        phone: str | None,
        customer_name: str | None = None,
        calendar_event_id: str | None = None,
        current_service_id: str | None = None,
        current_service_name: str | None = None,
    ) -> None:
        payload = {
            "start_dt": self._serialize_pending_start_dt(start_dt) if start_dt else None,
            "customer_name": customer_name,
            "email": email,
            "phone": phone,
            "calendar_event_id": calendar_event_id,
        }
        if current_service_id:
            payload["current_service_id"] = current_service_id
        if current_service_name:
            payload["current_service_name"] = current_service_name
        self._save_completed_booking(sender_id, payload)

    def _normalize_idempotency_contact(self, value: Any) -> str | None:
        if not value:
            return None
        return str(value).strip().lower()

    def _normalize_idempotency_phone(self, value: Any) -> str | None:
        if not value:
            return None
        digits = re.sub(r"\D", "", str(value))
        if len(digits) == 10 and digits.startswith("0"):
            return f"380{digits[1:]}"
        if len(digits) == 12 and digits.startswith("380"):
            return digits
        if len(digits) == 13 and digits.startswith("0380"):
            return digits[1:]
        return self._normalize_phone(str(value))

    def _completed_booking_matches_candidate(
        self,
        sender_id: str,
        *,
        start_dt: datetime,
        email: str | None,
        phone: str | None,
        current_service_id: str | None = None,
    ) -> dict[str, Any] | None:
        completed = self._get_completed_booking(sender_id)
        if not completed or not completed.get("start_dt"):
            return None

        try:
            completed_start = self._deserialize_pending_start_dt(completed["start_dt"])
        except Exception:
            logger.warning(
                "invalid completed booking start_dt ignored during idempotency check sender_id=%s raw_start_dt=%r",
                sender_id,
                completed.get("start_dt"),
            )
            return None

        if completed_start != start_dt.astimezone(self.timezone):
            return None

        completed_service_id = completed.get("current_service_id")
        if current_service_id and not completed_service_id:
            return None
        if current_service_id and completed_service_id and current_service_id != completed_service_id:
            return None

        candidate_email = self._normalize_idempotency_contact(email)
        completed_email = self._normalize_idempotency_contact(completed.get("email"))
        candidate_phone = self._normalize_idempotency_phone(phone)
        completed_phone = self._normalize_idempotency_phone(completed.get("phone"))

        contact_matched = False
        if candidate_email:
            if candidate_email != completed_email:
                return None
            contact_matched = True
        if candidate_phone:
            if candidate_phone != completed_phone:
                return None
            contact_matched = True

        if not contact_matched:
            return None

        return completed

    def _build_idempotent_confirmed_result(
        self,
        *,
        language: str,
        completed: dict[str, Any],
    ) -> Dict[str, Any]:
        start_dt = self._deserialize_pending_start_dt(completed["start_dt"])
        customer_name = completed.get("customer_name")
        event_id = completed.get("calendar_event_id") or completed.get("event_id")
        return {
            "status": "already_confirmed",
            "reply_text": self._build_idempotent_already_confirmed_reply(language, start_dt),
            "event_created": False,
            "booking_state": BookingState.NONE.value,
            "event_id": event_id,
            "event_link": completed.get("event_link"),
            "customer_name": customer_name,
            "contact_email": completed.get("email"),
            "contact_phone": completed.get("phone"),
            "idempotent": True,
        }

    def get_reschedule_reply(self, language: str) -> str:
        return f"У вас уже є підтверджений {self._appointment_label()}. Якщо хочете, можу допомогти перенести його на інший час."

    def get_reschedule_prompt_reply(self, language: str) -> str:
        return f"Так, звісно. Підкажіть, будь ласка, на який день і час вам буде зручно перенести {self._appointment_label()}?"

    def get_reschedule_handoff_reply(self, language: str) -> str:
        return f"Добре, передам команді, щоб перенесли {self._appointment_label()} без зайвих дій з вашого боку."

    def _build_reschedule_unavailable_reply(self, language: str) -> str:
        return "На цей час слот уже зайнятий. Підкажіть, будь ласка, інший день або час?"

    def handle_reschedule_request(self, sender_id: str, message_text: str) -> Dict[str, Any]:
        language = self._detect_language(message_text)
        requested_dt = self._parse_requested_datetime(message_text)

        if requested_dt is not None:
            return self._finalize_reschedule_to(sender_id, requested_dt=requested_dt, language=language)

        # No single exact time, but a date + time-window ("на п'ятницю після
        # 16") is still a concrete reschedule target -- reuse the existing
        # verified-slot suggestion machinery to offer real availability
        # instead of dropping out of the reschedule flow entirely.
        partial_date = self._extract_requested_date(message_text)
        requested_date = partial_date.get("date") if partial_date else None
        time_window = self._extract_time_window(message_text) if requested_date is not None else None
        daypart = self._extract_daypart(message_text) if requested_date is not None else None

        if requested_date is not None and (time_window is not None or daypart is not None):
            completed_booking = self._get_completed_booking(sender_id) or {}
            if time_window is not None:
                result = self._suggest_time_window_slots(
                    sender_id,
                    language=language,
                    message_text=message_text,
                    source_channel=None,
                    requested_date=requested_date,
                    requested_day_label=partial_date.get("day_label"),
                    time_window=time_window,
                    current_service_id=completed_booking.get("current_service_id"),
                    current_service_name=completed_booking.get("current_service_name"),
                )
            else:
                result = self._suggest_daypart_slots(
                    sender_id,
                    language=language,
                    message_text=message_text,
                    source_channel=None,
                    requested_date=requested_date,
                    requested_day_label=partial_date.get("day_label"),
                    daypart=daypart,
                    current_service_id=completed_booking.get("current_service_id"),
                    current_service_name=completed_booking.get("current_service_name"),
                )
            pending = self._get_pending_confirmation(sender_id) or {}
            pending["reschedule_pending"] = True
            self._save_pending_confirmation(sender_id, pending)
            return result

        return {
            "status": "reschedule_prompt",
            "reply_text": self.get_reschedule_prompt_reply(language),
            "booking_state": BookingState.NONE.value,
        }

    def _finalize_reschedule_to(
        self,
        sender_id: str,
        *,
        requested_dt: datetime,
        language: str,
    ) -> Dict[str, Any]:
        completed_booking = self._get_completed_booking(sender_id) or {}
        calendar_event_id = completed_booking.get("calendar_event_id") or completed_booking.get("event_id")
        if not calendar_event_id:
            return {
                "status": "reschedule_handoff",
                "reply_text": self.get_reschedule_handoff_reply(language),
                "booking_state": BookingState.NONE.value,
            }

        duration_minutes = self._booking_duration_minutes()
        if not self._is_within_business_hours(requested_dt, duration_minutes):
            return {
                "status": "reschedule_rejected_outside_business_hours",
                "reply_text": self._build_outside_business_hours_reply(language),
                "booking_state": BookingState.NONE.value,
                "start_dt": requested_dt.isoformat(),
            }

        try:
            is_available = self.calendar_service.check_specific_time_availability(
                start_dt=requested_dt,
                duration_minutes=duration_minutes,
            )
        except Exception:
            logger.exception(
                "calendar reschedule availability check failed sender_id=%s start_dt=%s",
                sender_id,
                requested_dt.isoformat(),
            )
            return {
                "status": "reschedule_handoff",
                "reply_text": self.get_reschedule_handoff_reply(language),
                "booking_state": BookingState.NONE.value,
                "start_dt": requested_dt.isoformat(),
            }

        if not is_available:
            return {
                "status": "reschedule_rejected_unavailable",
                "reply_text": self._build_reschedule_unavailable_reply(language),
                "booking_state": BookingState.NONE.value,
                "start_dt": requested_dt.isoformat(),
            }

        # Only mutate the calendar -- and only replace the old booking's
        # record -- after the new slot has been positively verified. Any
        # failure above or below this point leaves the old booking exactly
        # as it was.
        try:
            self.calendar_service.reschedule_event(
                event_id=calendar_event_id,
                start_dt=requested_dt,
                duration_minutes=duration_minutes,
            )
        except Exception:
            logger.exception(
                "calendar event reschedule failed sender_id=%s has_event_id=%s",
                sender_id,
                bool(calendar_event_id),
            )
            return {
                "status": "reschedule_handoff",
                "reply_text": self.get_reschedule_handoff_reply(language),
                "booking_state": BookingState.NONE.value,
            }

        self._save_completed_booking(sender_id, {
            **completed_booking,
            "start_dt": self._serialize_pending_start_dt(requested_dt),
        })
        self._clear_pending_confirmation(sender_id)

        formatted = self._format_scheduled_time_for_reply(requested_dt, "uk")
        reply_text = f"Супер, перенесли на {formatted} 🙌 Зв’яжемося з вами у цей час."

        return {
            "status": "rescheduled",
            "reply_text": reply_text,
            "booking_state": BookingState.NONE.value,
            "start_dt": requested_dt.isoformat(),
        }

    def _reschedule_offer_still_engaged(
        self,
        message_text: str,
        candidate_slots: list[datetime] | None = None,
    ) -> bool:
        """True if `message_text` plausibly continues an in-flight reschedule
        offer (a new target time, a day/window refinement, or an explicit
        accept/reject) -- the same signals `_continue_reschedule_selection`
        and the availability follow-up already act on. False means the
        message has nothing to do with the offer (e.g. an unrelated FAQ),
        which is the point at which the offer must be treated as abandoned.

        `candidate_slots`, when supplied, lets a natural offered-slot answer
        ("17 година буде зручно") count as a date/time signal too -- without
        it, this predicate would disagree with `_continue_reschedule_selection`
        (which does have the candidates) about whether such a message is a
        genuine answer, and the offer would be invalidated out from under a
        message that was about to legitimately select a slot.
        """
        if (
            self._is_confirmation_text(message_text)
            or self._is_rejection(message_text)
            or self._looks_like_another_time_request(message_text)
        ):
            # These are fixed-phrase markers ("так", "інший час", "раніше"),
            # not extracted date/time values -- they inherently have no
            # "residual content" to strip, so the check below would wrongly
            # reject them (e.g. "інший час" itself is the whole message).
            return True
        has_date_time_signal = (
            self._parse_requested_datetime(message_text) is not None
            or self._extract_requested_date(message_text) is not None
            or self._extract_daypart(message_text) is not None
            or self._extract_time_window(message_text) is not None
            or self._extract_suggested_slot_selection_time(
                message_text, candidate_slots=candidate_slots
            )
            is not None
            or self._detect_requested_day_key(message_text) is not None
        )
        if not has_date_time_signal:
            return False
        # A date/day/time reference only counts as "still engaged" when the
        # message is (close to) bare date/time content -- the same
        # residual-content check already used before finalizing a
        # reschedule. Otherwise a message that merely names a day alongside
        # unrelated content (e.g. "зустріч завтра" -- a different meeting)
        # would keep a stale offer alive instead of being invalidated, and a
        # later bare time could then resurrect it against the wrong intent.
        return not self._has_reschedule_target_residual_content(message_text)

    def _has_reschedule_target_residual_content(self, text: str) -> bool:
        """True if, after stripping recognized date/day/time wording and
        common connector words, meaningful content remains -- meaning any
        date/time in the message is incidental to some other statement
        rather than a direct answer to "which day and time?". Mirrors the
        same check MessageProcessor already applies to the pending-
        reschedule and confirmed-booking datetime-only guards.
        """
        normalized = self._normalize_booking_text(text.strip().lower())
        for marker in list(self._weekday_map().keys()) + list(self._ukrainian_month_map().keys()):
            normalized = re.sub(rf"\b{re.escape(marker)}\b", " ", normalized)
        normalized = re.sub(r"\bперенес\w*\b", " ", normalized)
        normalized = re.sub(
            r"\b(?:у|в|на|щодо|до|через|після|о|об|го|ого|сьогодні|завтра|післязавтра|"
            r"пізніше|раніше|ввечері|вранці|вдень|зранку|давайте|давай|можна|"
            r"так|добре|підходить|підійде|мені|хочу|годин\w*|год|буде|зручн\w*|"
            r"today|tomorrow|at)\b",
            " ",
            normalized,
        )
        normalized = re.sub(r"[^\w\sа-яіїєґё]", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\d+", " ", normalized)
        return bool(re.search(r"[a-zа-яіїєґ]{2,}", normalized, flags=re.IGNORECASE))

    def invalidate_stale_reschedule_offer(self, sender_id: str, message_text: str) -> None:
        """Drop a pending reschedule offer (and the slots it verified) the
        moment a message arrives that isn't clearly still acting on it --
        reusing the same invalidation principle already applied to
        suggested_slots/busy_alternative on an explicit date switch. Without
        this, an old offered slot stays matchable forever (as long as
        WAITING_FOR_TIME persists) and an unrelated later message that
        happens to name the same time can resurrect it and mutate a real,
        already-confirmed Calendar event.
        """
        pending = self._get_pending_confirmation(sender_id)
        if not pending or not pending.get("reschedule_pending"):
            return
        slots_by_day = self._suggested_slots_from_pending(pending)
        candidate_slots = [slot for slots in slots_by_day.values() for slot in slots]
        if self._reschedule_offer_still_engaged(message_text, candidate_slots=candidate_slots):
            return
        pending["reschedule_pending"] = False
        pending["suggested_slots"] = []
        pending["availability_context"] = False
        pending.pop("last_suggested_day", None)
        pending.pop("time_window", None)
        self._save_pending_confirmation(sender_id, pending)

    def invalidate_stale_busy_alternative(self, sender_id: str, message_text: str) -> None:
        """Drop a pending single-slot busy-alternative offer (and the
        availability context built around it) the moment a message arrives
        that isn't clearly still acting on it -- the same invalidation
        principle already applied to the reschedule offer via
        `invalidate_stale_reschedule_offer`. A later match against the
        alternative is always re-verified with a fresh Calendar check
        before any mutation regardless, but leaving the offer "live"
        indefinitely across unrelated turns is unnecessary and inconsistent
        with how the reschedule flow already behaves.
        """
        pending = self._get_pending_confirmation(sender_id)
        if not pending or not pending.get("busy_alternative"):
            return
        slots_by_day = self._suggested_slots_from_pending(pending)
        candidate_slots = [slot for slots in slots_by_day.values() for slot in slots]
        if self._reschedule_offer_still_engaged(message_text, candidate_slots=candidate_slots):
            return
        if self.EMAIL_RE.search(message_text) or self.PHONE_RE.search(message_text):
            # Contact info alone is the explicit missing piece this offer is
            # waiting on (unlike a reschedule, which never needs new
            # contact details) -- supplying it is a step toward completing
            # THIS offer, not an abandonment of it.
            return
        pending["busy_alternative"] = False
        pending["suggested_slots"] = []
        pending["availability_context"] = False
        pending.pop("last_suggested_day", None)
        self._save_pending_confirmation(sender_id, pending)

    def _reapply_reschedule_pending_after_refinement(
        self,
        sender_id: str,
        was_reschedule_pending: bool,
    ) -> None:
        """`_suggest_time_window_slots`/`_suggest_daypart_slots` rebuild the
        pending payload from scratch via `_save_booking_state`, which has no
        notion of `reschedule_pending` and so drops it. When a day/window
        refinement (e.g. "пізніше?", "ввечері") is requested mid-reschedule,
        the newly verified slots must stay tagged as the same reschedule
        offer -- otherwise selecting one afterwards silently starts a new
        booking instead of rescheduling the existing calendar_event_id.
        """
        if not was_reschedule_pending:
            return
        pending = self._get_pending_confirmation(sender_id)
        if not pending:
            return
        pending["reschedule_pending"] = True
        self._save_pending_confirmation(sender_id, pending)

    def _continue_reschedule_selection(
        self,
        sender_id: str,
        message_text: str,
        pending: dict[str, Any],
        source_channel: str | None,
    ) -> Dict[str, Any] | None:
        """Handles the follow-up to a reschedule window offer (e.g. picking
        "давайте 17" out of slots suggested for "на п'ятницю після 16").
        Reuses the same slot-selection matching as a normal booking, but
        finalizes via the reschedule mutation path instead of creating a
        new, unrelated booking.
        """
        if not pending.get("reschedule_pending"):
            return None

        language = pending.get("language") or self._detect_language(message_text)

        # A full datetime is only trusted as the reschedule target when the
        # message is (close to) bare date/time content -- the same
        # residual-content check already used to validate a pending-
        # reschedule target and a confirmed-booking datetime-only message.
        # Without it, an unrelated statement that merely happens to name a
        # date/time (e.g. "зустріч завтра о 17" -- a different meeting)
        # would silently reschedule the existing Calendar event.
        requested_dt = self._parse_requested_datetime(message_text)
        if requested_dt is not None and not self._has_reschedule_target_residual_content(message_text):
            return self._finalize_reschedule_to(sender_id, requested_dt=requested_dt, language=language)

        slots_by_day = self._suggested_slots_from_pending(pending)
        candidate_slots = [slot for slots in slots_by_day.values() for slot in slots]

        # A bare "так"/"добре"/"підходить" only unambiguously accepts the
        # offer when there is exactly one candidate slot -- with several
        # slots on the table it names nothing specific, so it must not pick
        # one arbitrarily (same rule the normal single-slot availability
        # accept already applies).
        if len(candidate_slots) == 1 and self._is_confirmation_text(message_text):
            return self._finalize_reschedule_to(sender_id, requested_dt=candidate_slots[0], language=language)

        requested_time = self._extract_suggested_slot_selection_time(
            message_text, candidate_slots=candidate_slots
        )
        if requested_time is None:
            return None

        matched_slot = next(
            (
                slot
                for slot in candidate_slots
                if slot.hour == requested_time.hour and slot.minute == requested_time.minute
            ),
            None,
        )
        if matched_slot is None:
            return {
                "status": "availability_time_not_offered",
                "reply_text": self._build_day_slots_reply(language, "selected_day", candidate_slots),
                "booking_state": BookingState.WAITING_FOR_TIME.value,
            }

        if self._has_reschedule_target_residual_content(message_text):
            # The message names a time that happens to match an offered
            # slot, but also carries unrelated content (e.g. "зустріч
            # завтра о 17" -- a different meeting) -- not a genuine slot
            # selection, so it must not silently reschedule the existing
            # Calendar event. _parse_time_only's marker-anchored branch
            # ("о"/"на" + hour) matches anywhere in the text, not just a
            # bare time, which is what makes this residual check necessary
            # here too, not only on the full-datetime branch above.
            return None

        return self._finalize_reschedule_to(sender_id, requested_dt=matched_slot, language=language)

    def _build_manual_followup_result(
        self,
        *,
        sender_id: str,
        language: str,
        start_dt: datetime | None,
        email: str | None,
        phone: str | None,
        customer_name: str | None,
    ) -> Dict[str, Any]:
        self._save_captured_contact(
            sender_id,
            customer_name=customer_name,
            email=email,
            phone=phone,
            start_dt=start_dt,
        )
        self._clear_pending_confirmation(sender_id)
        return {
            "status": "manual_followup",
            "reply_text": self._build_manual_followup_reply(language),
            "event_created": False,
            "booking_state": BookingState.NONE.value,
            "customer_name": customer_name,
            "contact_email": email,
            "contact_phone": phone,
        }

    def _get_suggested_slots_by_day(self) -> dict[str, list[datetime]]:
        now = datetime.now(self.timezone)
        candidates = {
            "tomorrow": [
                (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0),
                (now + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0),
            ],
            "day_after_tomorrow": [
                (now + timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0),
                (now + timedelta(days=2)).replace(hour=16, minute=0, second=0, microsecond=0),
            ],
        }

        client = self.calendar_service.google_calendar_client
        if not client or not client.is_configured():
            return {}

        checked: dict[str, list[datetime]] = {}
        for day_key, slots in candidates.items():
            checked[day_key] = []
            for slot in slots:
                try:
                    if self.calendar_service.check_specific_time_availability(
                        slot,
                        duration_minutes=self._booking_duration_minutes(),
                    ):
                        checked[day_key].append(slot)
                except Exception:
                    logger.exception("suggested slot availability check failed start_dt=%s", slot.isoformat())
                    return {}
            if not checked[day_key]:
                checked.pop(day_key, None)

        return checked

    def _format_slot_times(self, slots: list[datetime], language: str) -> str:
        times = [slot.strftime("%H:%M") for slot in self._display_slots(slots)]
        if not times:
            return ""
        if len(times) == 1:
            return f"о {times[0]}"
        if len(times) == 2:
            return f"{times[0]} або {times[1]}"
        return f"{', '.join(times[:-1])} або {times[-1]}"

    def _suggested_slots_from_pending(self, pending: dict[str, Any]) -> dict[str, list[datetime]]:
        slots_by_day: dict[str, list[datetime]] = {}
        for item in pending.get("suggested_slots", []):
            day_key = item.get("day_key")
            raw_start_dt = item.get("start_dt")
            if not day_key or not raw_start_dt:
                continue
            try:
                slots_by_day.setdefault(day_key, []).append(
                    self._deserialize_pending_start_dt(raw_start_dt)
                )
            except Exception:
                logger.warning("invalid suggested slot skipped: %r", item)
        if "suggested_slots" in pending:
            return slots_by_day
        return slots_by_day or self._get_suggested_slots_by_day()

    def _detect_requested_day_key(self, text: str) -> str | None:
        normalized = self._normalize_booking_text(text)
        if "післязавтра" in normalized or "day after tomorrow" in normalized:
            return "day_after_tomorrow"
        if "завтра" in normalized or "tomorrow" in normalized:
            return "tomorrow"
        return None

    def _date_for_day_key(
        self,
        day_key: str | None,
        slots_by_day: dict[str, list[datetime]],
    ) -> date | None:
        if not day_key:
            return None
        slots = slots_by_day.get(day_key) or []
        if slots:
            return slots[0].date()
        today = datetime.now(self.timezone).date()
        if day_key == "tomorrow":
            return today + timedelta(days=1)
        if day_key == "day_after_tomorrow":
            return today + timedelta(days=2)
        return None

    def _extract_hour_only(self, text: str) -> int | None:
        normalized = self._normalize_booking_text(text)
        match = re.fullmatch(r"(?:о|об|на|at)?\s*(\d{1,2})(?::00)?", normalized)
        if not match:
            return None
        hour = int(match.group(1))
        if 0 <= hour <= 23:
            return hour
        return None

    def _extract_selected_slot_time(self, text: str) -> time | None:
        return self._parse_time_only(text)

    def _extract_natural_suggested_slot_time(self, text: str) -> time | None:
        normalized = " ".join(text.strip().lower().split())
        if self._looks_like_unrelated_number_phrase(normalized):
            return None
        for pattern in [
            r"(?<!\d)(\d{1,2})\s*:\s*(\d{2})(?!\d)",
            r"(?<!\d)(\d{1,2})\s+(\d{2})(?!\d)",
        ]:
            match = re.search(pattern, normalized)
            if not match:
                continue
            hour = int(match.group(1))
            minute = int(match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return time(hour=hour, minute=minute)
        return None

    def _extract_suggested_slot_selection_time(
        self,
        text: str,
        candidate_slots: list[datetime] | None = None,
    ) -> time | None:
        selected = self._extract_natural_suggested_slot_time(text)
        if selected is not None:
            return selected

        rejected_time, negation_replacement_time, _wants_alternative = (
            self._extract_negated_time_context(text)
        )
        if negation_replacement_time is not None:
            return negation_replacement_time
        if rejected_time is not None:
            # A rejected time (e.g. "не о 14, а о 16" before finding the
            # replacement) must never be picked up as the selected slot.
            return None

        selected = self._extract_selected_slot_time(text)
        if selected is not None:
            return selected

        normalized = " ".join(text.strip().lower().split())
        normalized = re.sub(r"[^\w\sа-яіїєґё:]", " ", normalized, flags=re.IGNORECASE)
        normalized = " ".join(normalized.split())
        acceptance_markers = r"(?:а|давайте|давай|тоді|можна|на|о|так|да|ок|окей|добре)"
        acceptance_suffixes = r"(?:підійде|підходить|норм|нормально|добре|ок|окей)"
        patterns = [
            rf"^{acceptance_markers}\s+(\d{{1,2}})$",
            rf"^(\d{{1,2}})\s+{acceptance_suffixes}$",
            rf"^{acceptance_markers}\s+(\d{{1,2}})\s+{acceptance_suffixes}$",
        ]
        for pattern in patterns:
            match = re.fullmatch(pattern, normalized)
            if match:
                hour = int(match.group(1))
                if 0 <= hour <= 23:
                    return time(hour=hour, minute=0)

        if candidate_slots:
            selected = self._extract_offered_slot_time(text, candidate_slots)
            if selected is not None:
                return selected

        return None

    def _extract_offered_slot_time(
        self,
        text: str,
        candidate_slots: list[datetime],
    ) -> time | None:
        """A more permissive, but still safe, "the user directly answered
        our question" match: when there is an active list of offered
        slots, a message whose only numeric content is a bare hour
        matching one of those offered slots -- wrapped in ordinary
        acceptance/filler wording ("17 година буде зручно", "підходить
        17", typos included) -- is strong evidence of selecting that slot.
        Only used as a last-resort tier by `_extract_suggested_slot_selection_time`,
        after every fixed-pattern match already failed.

        Reuses `_looks_like_unrelated_number_phrase` to keep excluding
        age/headcount/duration numbers ("мені 17 років", "нас буде 10
        людей"), and the same residual-content principle already used for
        the reschedule-offer guard: any number that is incidental to some
        other statement (rather than a direct answer) leaves substantive
        text behind after stripping digits and known filler/connector
        words, and is rejected.
        """
        if self._looks_like_unrelated_number_phrase(text):
            return None
        if self.PHONE_RE.search(text) or self.EMAIL_RE.search(text):
            return None

        offered_hours = {slot.hour for slot in candidate_slots}
        normalized = self._normalize_booking_text(text)

        matching_hours = {
            int(match.group(1))
            for match in re.finditer(r"(?<!\d)(\d{1,2})(?!\d)", normalized)
            if int(match.group(1)) in offered_hours
        }
        if len(matching_hours) != 1:
            # No offered hour named, or more than one -- ambiguous, don't guess.
            return None
        selected_hour = next(iter(matching_hours))

        residual = re.sub(r"\d+", " ", normalized)
        residual = re.sub(
            r"\b(?:годин\w*|год|буде|зручн\w*|давайте|давай|тоді|"
            r"можна|мені|хочу|на|о|об|так|да|ок|окей|добре|підходить|підійде|"
            r"норм|нормально)\b",
            " ",
            residual,
        )
        residual = re.sub(r"[^\w\sа-яіїєґё]", " ", residual, flags=re.IGNORECASE)
        if re.search(r"[a-zа-яіїєґ]{2,}", residual, flags=re.IGNORECASE):
            return None

        return time(hour=selected_hour, minute=0)

    def _looks_like_unrelated_number_phrase(self, text: str) -> bool:
        normalized = self._normalize_booking_text(text)
        return bool(
            re.search(
                r"\b(?:рок(?:ів|и|у)?|люд(?:ей|ина|ини)?|ос(?:іб|оби)|хв(?:илин(?:а|и)?|))\b",
                normalized,
            )
        )

    def _deserialize_pending_time_window(self, pending: dict[str, Any]) -> dict[str, Any] | None:
        raw = pending.get("time_window")
        if not isinstance(raw, dict) or not raw.get("label"):
            return None
        try:
            return {
                "label": raw["label"],
                "start": time.fromisoformat(raw["start"]) if raw.get("start") else None,
                "end": time.fromisoformat(raw["end"]) if raw.get("end") else None,
            }
        except Exception:
            logger.warning("invalid pending time_window ignored: %r", raw)
            return None

    def _extract_relative_time_window(
        self,
        text: str,
        pending: dict[str, Any],
    ) -> tuple[date, str | None, dict[str, Any]] | None:
        normalized = " ".join(text.strip().lower().split())
        wants_earlier = "раніше" in normalized
        wants_later = "пізніше" in normalized or "пізнiше" in normalized
        if not wants_earlier and not wants_later:
            return None

        slots_by_day = self._suggested_slots_from_pending(pending)
        preferred_day = pending.get("last_suggested_day")
        slots = list(slots_by_day.get(preferred_day, [])) if preferred_day else []
        if not slots:
            slots = [
                slot
                for day_slots in slots_by_day.values()
                for slot in day_slots
            ]
        if not slots:
            return None

        slots.sort()
        requested_date = slots[0].date()
        requested_day_label = pending.get("requested_day_label")
        duration = self._booking_duration_minutes()
        if wants_earlier:
            return (
                requested_date,
                requested_day_label,
                {
                    "label": f"до {self._format_time_window_time(slots[0].timetz().replace(tzinfo=None))}",
                    "start": None,
                    "end": slots[0].timetz().replace(tzinfo=None),
                },
            )

        later_start_dt = slots[-1] + timedelta(minutes=duration)
        return (
            requested_date,
            requested_day_label,
            {
                "label": f"після {self._format_time_window_time(slots[-1].timetz().replace(tzinfo=None))}",
                "start": later_start_dt.timetz().replace(tzinfo=None),
                "end": None,
            },
        )

    def _build_day_slots_reply(self, language: str, day_key: str, slots: list[datetime]) -> str:
        if slots:
            day_label_uk = self._format_date_label_for_reply(slots[0].date(), "uk") or (
                "завтра" if day_key == "tomorrow" else "післязавтра"
            )
        else:
            day_label_uk = "завтра" if day_key == "tomorrow" else "післязавтра"
        times = self._format_slot_times(self._display_slots(slots), language)
        if times:
            return f"Добре, {day_label_uk} можемо запропонувати {times}. Який час вам зручніший?"
        return f"На {day_label_uk} вільного часу не бачу. Можете написати інший день або конкретний час, і я перевірю."

    def handle_greeting_during_active_offer(
        self,
        sender_id: str,
        message_text: str,
    ) -> Dict[str, Any] | None:
        """A harmless greeting ("Привіт") while slots are already on offer
        must not be treated as if it answered or reset the pending
        question -- it carries no date/time/service content at all, so
        every existing parser correctly finds nothing in it and the flow
        would otherwise fall through to the generic "which time?" prompt,
        silently dropping the fact that specific slots were just offered.
        Read-only: does not touch pending state, so it cannot weaken any
        stale-offer/reschedule safety gate -- those are unaffected because
        this never fires for messages that carry real content.
        """
        pending = self._get_pending_confirmation(sender_id)
        if not pending or not pending.get("availability_context"):
            return None

        slots_by_day = self._suggested_slots_from_pending(pending)
        day_key = pending.get("last_suggested_day")
        slots = slots_by_day.get(day_key, []) if day_key else []
        if not slots:
            slots = [slot for day_slots in slots_by_day.values() for slot in day_slots]
        if not slots:
            return None

        language = pending.get("language") or self._detect_language(message_text)
        offer_reply = self._build_day_slots_reply(language, day_key or "selected_day", slots)
        return {
            "status": "greeting_during_active_offer",
            "reply_text": "Вітаю 🙂 Ми якраз підбирали для вас час. " + offer_reply,
            "event_created": False,
            "requires_confirmation": False,
            "booking_state": BookingState.WAITING_FOR_TIME.value,
        }

    def _process_availability_followup(
        self,
        sender_id: str,
        message_text: str,
        pending: dict[str, Any],
        source_channel: str | None,
    ) -> Dict[str, Any] | None:
        # Captured up front: a day/window refinement below regenerates slots
        # via _suggest_time_window_slots/_suggest_daypart_slots, which drop
        # this flag when they rebuild the pending payload. When it was set,
        # it must be reapplied afterwards so the refined slots stay part of
        # the same reschedule offer instead of silently becoming a new booking.
        was_reschedule_pending = bool(pending.get("reschedule_pending"))

        requested_dt = self._parse_requested_datetime(message_text)
        if requested_dt is not None:
            return self.start_booking_flow(
                sender_id=sender_id,
                message_text=message_text,
                source_channel=source_channel or pending.get("source_channel"),
                current_service_id=pending.get("current_service_id"),
                current_service_name=pending.get("current_service_name"),
            )

        language = pending.get("language") or self._detect_language(message_text)
        normalized = message_text.strip().lower()

        partial_date = self._extract_requested_date(message_text)
        requested_day_key = self._detect_requested_day_key(message_text)
        if (
            partial_date is not None
            and requested_day_key is None
            and pending.get("requested_date") != partial_date["date"].isoformat()
        ):
            # The user explicitly switched to a different date -- any
            # previously suggested slots were offered for the old date and
            # must not be treated as valid candidates (or matched against)
            # for the new one, in this turn or any later one. Re-anchor the
            # requested date right away so a bare time reply (possibly in a
            # later turn) evaluates against the new day, not a stale one.
            # (A canonical "tomorrow"/"day after tomorrow" reference is not
            # a switch away from the existing suggestion buckets -- those
            # are matched by day key below, so leave them intact.)
            pending["suggested_slots"] = []
            pending.pop("last_suggested_day", None)
            pending["requested_date"] = partial_date["date"].isoformat()
            pending["requested_day_label"] = partial_date.get("day_label")
            self._save_pending_confirmation(sender_id, pending)

        slots_by_day = self._suggested_slots_from_pending(pending)
        preferred_day = pending.get("last_suggested_day") or "tomorrow"
        if requested_day_key:
            preferred_day = requested_day_key
        candidate_slots = slots_by_day.get(preferred_day, [])

        pending_requested_date = None
        if pending.get("requested_date"):
            try:
                pending_requested_date = self._deserialize_pending_requested_date(
                    pending.get("requested_date")
                )
            except Exception:
                pending_requested_date = None
        requested_date = (
            partial_date.get("date")
            if partial_date
            else pending_requested_date or self._date_for_day_key(preferred_day, slots_by_day)
        )
        requested_day_label = (
            partial_date.get("day_label")
            if partial_date
            else pending.get("requested_day_label")
        )
        if requested_day_key:
            pending["last_suggested_day"] = requested_day_key
            self._save_pending_confirmation(sender_id, pending)

        time_window = self._extract_time_window(message_text)
        daypart = self._extract_daypart(message_text)
        if time_window is not None and requested_date is not None:
            result = self._suggest_time_window_slots(
                sender_id,
                language=language,
                message_text=message_text,
                source_channel=source_channel or pending.get("source_channel"),
                requested_date=requested_date,
                requested_day_label=requested_day_label,
                time_window=time_window,
            )
            self._reapply_reschedule_pending_after_refinement(sender_id, was_reschedule_pending)
            return result
        if daypart is not None and requested_date is not None:
            result = self._suggest_daypart_slots(
                sender_id,
                language=language,
                message_text=message_text,
                source_channel=source_channel or pending.get("source_channel"),
                requested_date=requested_date,
                requested_day_label=requested_day_label,
                daypart=daypart,
            )
            self._reapply_reschedule_pending_after_refinement(sender_id, was_reschedule_pending)
            return result

        relative_window = self._extract_relative_time_window(message_text, pending)
        if relative_window is not None:
            requested_date, requested_day_label, time_window = relative_window
            result = self._suggest_time_window_slots(
                sender_id,
                language=language,
                message_text=message_text,
                source_channel=source_channel or pending.get("source_channel"),
                requested_date=requested_date,
                requested_day_label=requested_day_label,
                time_window=time_window,
            )
            self._reapply_reschedule_pending_after_refinement(sender_id, was_reschedule_pending)
            return result

        if self._looks_like_another_time_request(message_text) and not pending.get("time_window"):
            self._save_booking_state(
                sender_id,
                state=BookingState.WAITING_FOR_TIME,
                language=language,
                source_channel=source_channel or pending.get("source_channel"),
                context_summary=pending.get("context_summary"),
                requested_date=requested_date,
                requested_day_label=requested_day_label,
                customer_name=pending.get("customer_name"),
                contact_email=pending.get("contact_email"),
                contact_phone=pending.get("contact_phone"),
            )
            return {
                "status": "waiting_for_time",
                "reply_text": self._build_another_time_reply(language, requested_day_label),
                "event_created": False,
                "requires_confirmation": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
                "requested_date": requested_date.isoformat() if requested_date else None,
            }

        if self._is_confirmation_text(normalized) and len(candidate_slots) == 1:
            matched_slot = candidate_slots[0]
            if self._has_required_contact(
                customer_name=pending.get("customer_name"),
                email=pending.get("contact_email"),
                phone=pending.get("contact_phone"),
            ):
                # Contact was already captured on an earlier turn (e.g. the
                # busy-alternative flow) -- reuse it instead of discarding it
                # and asking again, and finalize immediately since nothing
                # else is outstanding.
                contact_message_text = " ".join(
                    part
                    for part in [
                        pending.get("customer_name"),
                        pending.get("contact_email"),
                        pending.get("contact_phone"),
                    ]
                    if part
                )
                return self.start_booking_flow(
                    sender_id=sender_id,
                    message_text=contact_message_text,
                    source_channel=source_channel or pending.get("source_channel"),
                    requested_dt_override=matched_slot,
                    current_service_id=pending.get("current_service_id"),
                    current_service_name=pending.get("current_service_name"),
                )
            self._save_booking_state(
                sender_id,
                state=BookingState.WAITING_FOR_CONTACT,
                language=language,
                start_dt=matched_slot,
                source_channel=source_channel or pending.get("source_channel"),
                context_summary=pending.get("context_summary"),
            )
            accepted_pending = self._get_pending_confirmation(sender_id) or {}
            if pending.get("current_service_id"):
                accepted_pending["current_service_id"] = pending.get("current_service_id")
            if pending.get("current_service_name"):
                accepted_pending["current_service_name"] = pending.get("current_service_name")
            self._save_pending_confirmation(sender_id, accepted_pending)
            return {
                "status": "waiting_for_contact",
                "reply_text": self._build_suggested_slot_accepted_reply(language, matched_slot),
                "requires_confirmation": False,
                "requires_contact": True,
                "booking_state": BookingState.WAITING_FOR_CONTACT.value,
                "start_dt": matched_slot.isoformat(),
            }

        if requested_day_key:
            return {
                "status": "availability_day_selected",
                "reply_text": self._build_day_slots_reply(
                    language,
                    requested_day_key,
                    slots_by_day.get(requested_day_key, []),
                ),
                "booking_state": BookingState.WAITING_FOR_TIME.value,
            }

        requested_time = self._extract_suggested_slot_selection_time(
            message_text, candidate_slots=candidate_slots
        )
        if requested_time is None:
            contact_details = (
                self._extract_anchored_contact_details(message_text)
                if self.PHONE_RE.search(message_text) or self.EMAIL_RE.search(message_text)
                else self._extract_contact_details(message_text)
            )
            if self._has_required_contact(
                customer_name=contact_details["customer_name"] or pending.get("customer_name"),
                email=contact_details["email"] or pending.get("contact_email"),
                phone=contact_details["phone"] or pending.get("contact_phone"),
            ):
                pending["customer_name"] = contact_details["customer_name"] or pending.get("customer_name")
                pending["contact_email"] = contact_details["email"] or pending.get("contact_email")
                pending["contact_phone"] = contact_details["phone"] or pending.get("contact_phone")
                self._save_pending_confirmation(sender_id, pending)
                # No offered slots to list for the currently anchored day
                # (e.g. right after an explicit day switch invalidated the
                # old bucket) -- ask for a time on that day instead of
                # reciting an unrelated (or empty) slot list.
                if candidate_slots:
                    reply_text = self._build_day_slots_reply(language, preferred_day, candidate_slots)
                    reply_requested_date = candidate_slots[0].date().isoformat()
                else:
                    reply_text = self._build_missing_time_reply(language, requested_day_label)
                    reply_requested_date = requested_date.isoformat() if requested_date else None
                return {
                    "status": "waiting_for_time",
                    "reply_text": reply_text,
                    "event_created": False,
                    "requires_confirmation": False,
                    "booking_state": BookingState.WAITING_FOR_TIME.value,
                    "requested_date": reply_requested_date,
                }
            return None

        matched_slot = next(
            (
                slot
                for slot in candidate_slots
                if slot.hour == requested_time.hour and slot.minute == requested_time.minute
            ),
            None,
        )
        if matched_slot is None:
            for day_key, slots in slots_by_day.items():
                matched_slot = next(
                    (
                        slot
                        for slot in slots
                        if slot.hour == requested_time.hour and slot.minute == requested_time.minute
                    ),
                    None,
                )
                if matched_slot is not None:
                    preferred_day = day_key
                    break

        if matched_slot is None:
            # A busy-alternative suggestion (as opposed to a multi-slot
            # day/window list) isn't a fixed menu -- naming a different
            # explicit time here is a correction, so it must get a fresh
            # availability check rather than being rejected outright. The
            # same applies when there is nothing offered for the currently
            # anchored day at all (e.g. right after an explicit day switch
            # invalidated the previous suggestions) -- there is no stale
            # list to reject against, so check the named time directly.
            if (pending.get("busy_alternative") or not candidate_slots) and requested_date is not None:
                corrected_dt = self._combine_requested_date_and_time(
                    requested_date,
                    requested_time,
                )
                return self.start_booking_flow(
                    sender_id=sender_id,
                    message_text=message_text,
                    source_channel=source_channel or pending.get("source_channel"),
                    requested_dt_override=corrected_dt,
                    current_service_id=pending.get("current_service_id"),
                    current_service_name=pending.get("current_service_name"),
                )
            return {
                "status": "availability_time_not_offered",
                "reply_text": self._build_day_slots_reply(
                    language,
                    preferred_day,
                    candidate_slots,
                ),
                "booking_state": BookingState.WAITING_FOR_TIME.value,
            }

        contact_message_text = message_text
        if self._has_required_contact(
            customer_name=pending.get("customer_name"),
            email=pending.get("contact_email"),
            phone=pending.get("contact_phone"),
        ):
            contact_message_text = " ".join(
                part
                for part in [
                    pending.get("customer_name"),
                    pending.get("contact_email"),
                    pending.get("contact_phone"),
                ]
                if part
            )

        return self.start_booking_flow(
            sender_id=sender_id,
            message_text=contact_message_text,
            source_channel=source_channel or pending.get("source_channel"),
            requested_dt_override=matched_slot,
            current_service_id=pending.get("current_service_id"),
            current_service_name=pending.get("current_service_name"),
        )

    def _serialize_pending_start_dt(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.timezone)
        return value.isoformat()

    def _deserialize_pending_start_dt(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=self.timezone)
            return value.astimezone(self.timezone)

        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=self.timezone)
            return parsed.astimezone(self.timezone)

        raise ValueError(f"Unsupported pending start_dt type: {type(value)!r}")

    def _weekday_map(self) -> dict[str, int]:
        return {
            "monday": 0,
            "пн": 0,
            "понеділок": 0,
            "понеділка": 0,
            "вт": 1,
            "вівторок": 1,
            "вівторка": 1,
            "tuesday": 1,
            "ср": 2,
            "середа": 2,
            "середу": 2,
            "wednesday": 2,
            "чт": 3,
            "четвер": 3,
            "четверг": 3,
            "thursday": 3,
            "пт": 4,
            "п'ятниц": 4,
            "п'ятниця": 4,
            "п'ятницю": 4,
            "п’ятниц": 4,
            "п’ятниця": 4,
            "п’ятницю": 4,
            "пятниця": 4,
            "пятницю": 4,
            "friday": 4,
            "сб": 5,
            "субота": 5,
            "суботу": 5,
            "saturday": 5,
            "нд": 6,
            "неділя": 6,
            "неділю": 6,
            "sunday": 6,
        }

    def _find_weekday_marker(self, text: str) -> tuple[str, int] | None:
        token_boundary = r"0-9A-Za-zА-Яа-яІіЇїЄєҐґЁё"
        for marker, weekday in sorted(self._weekday_map().items(), key=lambda item: len(item[0]), reverse=True):
            pattern = rf"(?<![{token_boundary}]){re.escape(marker)}(?![{token_boundary}])"
            if re.search(pattern, text):
                return marker, weekday
        return None

    def _ukrainian_month_map(self) -> dict[str, tuple[int, str]]:
        return {
            "січень": (1, "січня"),
            "січня": (1, "січня"),
            "сiчень": (1, "січня"),
            "сiчня": (1, "січня"),
            "лютий": (2, "лютого"),
            "лютого": (2, "лютого"),
            "березень": (3, "березня"),
            "березня": (3, "березня"),
            "квітень": (4, "квітня"),
            "квітня": (4, "квітня"),
            "квiтень": (4, "квітня"),
            "квiтня": (4, "квітня"),
            "травень": (5, "травня"),
            "травня": (5, "травня"),
            "червень": (6, "червня"),
            "червня": (6, "червня"),
            "липень": (7, "липня"),
            "липня": (7, "липня"),
            "серпень": (8, "серпня"),
            "серпня": (8, "серпня"),
            "вересень": (9, "вересня"),
            "вересня": (9, "вересня"),
            "жовтень": (10, "жовтня"),
            "жовтня": (10, "жовтня"),
            "листопад": (11, "листопада"),
            "листопада": (11, "листопада"),
            "грудень": (12, "грудня"),
            "грудня": (12, "грудня"),
        }

    def _month_name_pattern(self) -> str:
        month_names = self._ukrainian_month_map()
        return "|".join(re.escape(name) for name in sorted(month_names, key=len, reverse=True))

    def _next_calendar_date(self, *, day: int, month: int, explicit_year: int | None = None) -> date | None:
        now = datetime.now(self.timezone).date()
        year = explicit_year or now.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if explicit_year is None and candidate < now:
            try:
                candidate = date(year + 1, month, day)
            except ValueError:
                return None
        return candidate

    def _extract_absolute_calendar_date(self, text: str) -> dict[str, Any] | None:
        normalized = self._normalize_booking_text(text)
        month_names = self._ukrainian_month_map()
        month_pattern = self._month_name_pattern()

        month_match = re.search(
            rf"(?<![\dA-Za-zА-Яа-яІіЇїЄєҐґЁё])"
            rf"(\d{{1,2}})(?:\s*(?:-?\s*(?:го|ого)))?\s+({month_pattern})"
            rf"(?:\s+(\d{{4}}))?"
            rf"(?![A-Za-zА-Яа-яІіЇїЄєҐґЁё])",
            normalized,
        )
        if month_match:
            day = int(month_match.group(1))
            month, month_label = month_names[month_match.group(2)]
            explicit_year = int(month_match.group(3)) if month_match.group(3) else None
            target = self._next_calendar_date(day=day, month=month, explicit_year=explicit_year)
            if target is None:
                return None
            return {"date": target, "day_label": f"{day} {month_label}"}

        numeric_match = re.search(
            r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?!\d)",
            normalized,
        )
        if numeric_match:
            day = int(numeric_match.group(1))
            month = int(numeric_match.group(2))
            explicit_year = int(numeric_match.group(3)) if numeric_match.group(3) else None
            target = self._next_calendar_date(day=day, month=month, explicit_year=explicit_year)
            if target is None:
                return None
            return {"date": target, "day_label": f"{day:02d}.{month:02d}"}

        return None

    def _format_day_label_for_reply(self, value: str | None) -> str | None:
        if not value:
            return None
        labels = {
            "today": "сьогодні",
            "tomorrow": "завтра",
            "day after tomorrow": "післязавтра",
            "пн": "понеділок",
            "вт": "вівторок",
            "ср": "середу",
            "чт": "четвер",
            "пт": "п’ятницю",
            "сб": "суботу",
            "нд": "неділю",
            "monday": "понеділок",
            "tuesday": "вівторок",
            "wednesday": "середу",
            "thursday": "четвер",
            "friday": "п’ятницю",
            "saturday": "суботу",
            "sunday": "неділю",
            "понеділка": "понеділок",
            "вівторка": "вівторок",
            "середа": "середу",
            "четверг": "четвер",
            "п'ятниц": "п’ятницю",
            "п'ятниця": "п’ятницю",
            "п'ятницю": "п’ятницю",
            "п’ятниц": "п’ятницю",
            "п’ятниця": "п’ятницю",
            "п’ятницю": "п’ятницю",
            "пятниця": "п’ятницю",
            "пятницю": "п’ятницю",
            "субота": "суботу",
            "неділя": "неділю",
        }
        return labels.get(value, value)

    def _format_date_label_for_reply(self, value: date | None, language: str) -> str | None:
        if value is None:
            return None
        today = datetime.now(self.timezone).date()
        if value == today:
            return "сьогодні" if language == "uk" else "today"
        if value == today + timedelta(days=1):
            return "завтра" if language == "uk" else "tomorrow"
        if value == today + timedelta(days=2):
            return "післязавтра" if language == "uk" else "day after tomorrow"
        labels_uk = {
            0: "понеділок",
            1: "вівторок",
            2: "середу",
            3: "четвер",
            4: "п’ятницю",
            5: "суботу",
            6: "неділю",
        }
        labels_en = {
            0: "Monday",
            1: "Tuesday",
            2: "Wednesday",
            3: "Thursday",
            4: "Friday",
            5: "Saturday",
            6: "Sunday",
        }
        return (labels_uk if language == "uk" else labels_en).get(value.weekday())

    def _extract_requested_date(self, text: str) -> dict[str, Any] | None:
        now = datetime.now(self.timezone)
        normalized = self._normalize_booking_text(text)

        if "післязавтра" in normalized or "day after tomorrow" in normalized:
            target = now.date() + timedelta(days=2)
            return {"date": target, "day_label": "післязавтра"}
        if "завтра" in normalized or "tomorrow" in normalized:
            target = now.date() + timedelta(days=1)
            return {"date": target, "day_label": "завтра"}
        if "сьогодні" in normalized or "today" in normalized:
            return {"date": now.date(), "day_label": "сьогодні"}

        absolute_date = self._extract_absolute_calendar_date(normalized)
        if absolute_date is not None:
            return absolute_date

        weekday_match = self._find_weekday_marker(normalized)
        if weekday_match is not None:
            marker, weekday = weekday_match
            days_ahead = (weekday - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target = now.date() + timedelta(days=days_ahead)
            return {
                "date": target,
                "day_label": self._format_day_label_for_reply(marker),
            }

        return None

    def _resolve_hour_minute(self, hour_str: str, minute_str: str | None) -> time | None:
        hour = int(hour_str)
        if not (0 <= hour <= 23):
            return None
        if minute_str is None:
            return time(hour=hour, minute=0)
        # An explicit minute token was present but isn't exactly two digits
        # (e.g. "14 3" or "14 300") -- reject rather than silently falling
        # back to the bare hour, which would lose the user's intended minutes.
        if len(minute_str) != 2:
            return None
        minute = int(minute_str)
        if not (0 <= minute <= 59):
            return None
        return time(hour=hour, minute=minute)

    def _parse_time_only(self, text: str) -> time | None:
        normalized = self._normalize_booking_text(text)

        # HH:MM or HH: MM anywhere in the text (colon is an unambiguous time marker).
        colon_match = re.search(r"\b(\d{1,2})\s*:\s*(\d{1,3})\b", normalized)
        if colon_match:
            return self._resolve_hour_minute(colon_match.group(1), colon_match.group(2))

        # о/на/at HH [MM] -- marker-anchored, space-separated minutes optional.
        # Skip matches that are actually "<day> <month name>" date phrases
        # (e.g. "на 3 вересня"), so the day number isn't mistaken for an hour.
        month_pattern = self._month_name_pattern()
        for marker_match in re.finditer(
            r"\b(?:о|об|на|at)\s*(\d{1,2})(?:\s+(\d{1,3}))?\b",
            normalized,
        ):
            tail = normalized[marker_match.end():].lstrip(" ,.-")
            if re.match(rf"(?:го|ого)?\s*(?:{month_pattern})\b", tail):
                continue
            return self._resolve_hour_minute(marker_match.group(1), marker_match.group(2))

        # Bare "HH" or "HH MM" -- only when it is the entire message, so an
        # unrelated number elsewhere in a sentence is never mistaken for a time.
        bare_match = re.fullmatch(r"\s*(\d{1,2})(?:\s+(\d{1,3}))?\s*", normalized)
        if bare_match:
            return self._resolve_hour_minute(bare_match.group(1), bare_match.group(2))

        return None

    def _extract_negated_time_context(
        self, text: str
    ) -> tuple[time | None, time | None, bool]:
        """Detects a rejected time, an explicit replacement, and whether the
        user is asking for an alternative -- regardless of whether the
        rejection cue appears before or after the time it negates.

        Returns (rejected_time, replacement_time, wants_alternative):
        - rejected_time: the time the user explicitly said is unavailable or
          unwanted ("не о 14", "о 14 не можу", "о 14 зайнятий",
          "о 14 не підходить", "на 14 не встигаю", ...), or None.
        - replacement_time: the explicitly stated positive replacement time
          when one is given (e.g. "о 14 не можу, а о 16 можу",
          "о 14 зайнятий, давайте 16", "14 не вийде, 16 підійде"
          -> time(16, 0)); otherwise None.
        - wants_alternative: True when the user rejects a time and asks for
          another one ("є інший час?", "щось пізніше?") without naming a
          specific replacement.
        """
        normalized = " ".join(text.strip().lower().split())

        time_tokens: list[tuple[int, int, time]] = []
        for match in re.finditer(
            r"\b(?:о|на|at)?\s*(\d{1,2})(?:\s*:\s*(\d{1,3})|\s+(\d{1,3}))?\b",
            normalized,
        ):
            minute_str = match.group(2) or match.group(3)
            resolved = self._resolve_hour_minute(match.group(1), minute_str)
            if resolved is not None:
                time_tokens.append((match.start(), match.end(), resolved))
        if not time_tokens:
            return None, None, False

        replacement_marker_positions = [
            match.start()
            for match in re.finditer(r"\b(?:а|тоді|краще|давай|давайте|можна)\b", normalized)
        ]
        positive_suffix_spans = [
            (match.start(), match.end())
            for match in re.finditer(r"\b(?:підійде|норм|нормально)\b", normalized)
        ]

        def _nearest_time_index(position: int) -> int | None:
            best_index = None
            best_distance = None
            for index, (start, end, _resolved) in enumerate(time_tokens):
                distance = min(abs(start - position), abs(end - position))
                if best_distance is None or distance < best_distance:
                    best_distance = distance
                    best_index = index
            return best_index

        def _next_time_index_at_or_after(position: int) -> int | None:
            for index, (start, _end, _resolved) in enumerate(time_tokens):
                if start >= position:
                    return index
            return None

        def _previous_time_index_at_or_before(position: int) -> int | None:
            result = None
            for index, (_start, end, _resolved) in enumerate(time_tokens):
                if end <= position:
                    result = index
            return result

        rejected_index = None

        # Bare "не" directly attached to a time ("не 14", "не о 14",
        # "не на 14") -- tightly bound to the number immediately following
        # it, not merely the nearest one in the sentence.
        for match in re.finditer(r"\bне\s+(?:(?:о|на)\s+)?(\d{1,2})\b", normalized):
            index = _nearest_time_index(match.start(1))
            if index is not None:
                rejected_index = index

        # Broader rejection cues (verb/adjective forms), associated with the
        # nearest time regardless of whether the cue precedes or follows it
        # (e.g. "о 14 не можу", "о 14 зайнятий", "на 14 не встигаю").
        for match in re.finditer(
            r"не\s*(?:можу|зможу|вийде|виходить|підходить|встигаю|буду|буде|"
            r"прийду|прийти|дуже|зручно)"
            r"|зайнят(?:ий|а|і)",
            normalized,
        ):
            index = _nearest_time_index((match.start() + match.end()) // 2)
            if index is not None:
                rejected_index = index

        if rejected_index is None:
            return None, None, False

        replacement_index = None
        # "а"/"тоді"/"краще"/"давай"/"давайте"/"можна" precede the time they
        # introduce, so look forward for the replacement they name.
        for position in replacement_marker_positions:
            index = _next_time_index_at_or_after(position)
            if index is not None and index != rejected_index:
                replacement_index = index
        # "підійде"/"норм"/"нормально" follow the time they confirm, so look
        # backward for it.
        for start, end in positive_suffix_spans:
            index = _previous_time_index_at_or_before(end)
            if index is not None and index != rejected_index:
                replacement_index = index

        rejected_time = time_tokens[rejected_index][2]
        replacement_time = (
            time_tokens[replacement_index][2] if replacement_index is not None else None
        )

        wants_alternative = replacement_time is None and bool(
            re.search(
                r"\b(?:пізніше|інший|інше|іншого|варіант|варіанти|ще)\b",
                normalized,
            )
        )

        return rejected_time, replacement_time, wants_alternative

    def _looks_like_booking_correction(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        # Standalone-word markers: matched only as a whole token, so they
        # never fire inside an unrelated word that merely contains them as a
        # substring (e.g. "ні" inside "мені"/"гривні"/"днів", "краще" inside
        # "покращення").
        standalone_correction_markers = [
            "краще",
            "ні",
        ]
        # Phrase markers: multi-word or already-specific enough that raw
        # substring matching doesn't collide with unrelated text.
        phrase_correction_markers = [
            "не хочу",
            "мав на увазі",
            "мала на увазі",
            "маю на увазі",
            "я мав на увазі",
            "я мала на увазі",
            "не ",
            "не о",
            "не на",
            "rather",
            "instead",
            "i mean",
            "meant",
        ]
        if any(
            re.search(rf"\b{re.escape(marker)}\b", normalized)
            for marker in standalone_correction_markers
        ):
            return True
        if any(marker in normalized for marker in phrase_correction_markers):
            return True
        normalized_for_time = re.sub(
            r"[^\w\sа-яіїєґё:]", " ", normalized, flags=re.IGNORECASE
        )
        normalized_for_time = " ".join(normalized_for_time.split())
        bounded_correction_patterns = [
            r"^(?:а|тоді|давай|давайте|можна)\s+(?:(?:о|на)\s+)?\d{1,2}(?::\d{2})?$",
            r"^(?:а|тоді|давай|давайте|можна)\s+(?:(?:о|на)\s+)?\d{1,2}\s+\d{2}$",
        ]
        if any(re.fullmatch(pattern, normalized_for_time) for pattern in bounded_correction_patterns):
            return True
        return bool(re.search(r"\bnot\s+(?:at\s+)?\d{1,2}\b", normalized))

    def _parse_time_candidates(self, text: str) -> list[time]:
        normalized = text.strip().lower()
        candidates: list[time] = []

        for match in re.finditer(r"\b(\d{1,2}):(\d{2})\b", normalized):
            hour = int(match.group(1))
            minute = int(match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                candidates.append(time(hour=hour, minute=minute))

        for match in re.finditer(r"\b(?:о|на|at)\s*(\d{1,2})(?::00)?\b", normalized):
            hour = int(match.group(1))
            if 0 <= hour <= 23:
                candidate = time(hour=hour, minute=0)
                if candidate not in candidates:
                    candidates.append(candidate)

        return candidates

    def _parse_loose_hour_candidates(self, text: str) -> list[time]:
        candidates = self._parse_time_candidates(text)
        normalized = text.strip().lower()
        # Skip a bare number that is actually a "<day> <month name>" date
        # phrase (e.g. "4 вересня") -- the same exclusion _parse_time_only
        # already applies, so a date-only correction like "краще 4 вересня"
        # doesn't misread the day-of-month digit as an hour.
        month_pattern = self._month_name_pattern()
        # Same for a numeric "DD.MM"/"DD.MM.YYYY" date (e.g. "04.09") -- both
        # the day and month components must not be read as bare hours.
        numeric_date_spans = [
            (match.start(), match.end())
            for match in re.finditer(r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b", normalized)
        ]
        for hour_match in re.finditer(r"\b(\d{1,2})(?::00)?\b", normalized):
            tail = normalized[hour_match.end():].lstrip(" ,.-")
            if re.match(rf"(?:го|ого)?\s*(?:{month_pattern})\b", tail):
                continue
            if any(start <= hour_match.start() < end for start, end in numeric_date_spans):
                continue
            hour = int(hour_match.group(1))
            if 0 <= hour <= 23:
                candidate = time(hour=hour, minute=0)
                if candidate not in candidates:
                    candidates.append(candidate)
        return candidates

    def _extract_corrected_time(self, text: str) -> time | None:
        if not self._looks_like_booking_correction(text):
            return None

        normalized = text.strip().lower()
        bounded_normalized = re.sub(
            r"[^\w\sа-яіїєґё:]", " ", normalized, flags=re.IGNORECASE
        )
        bounded_normalized = " ".join(bounded_normalized.split())
        bounded_match = re.fullmatch(
            r"(?:а|тоді|давай|давайте|можна)\s+(?:(?:о|на)\s+)?(\d{1,2})(?::(\d{2}))?",
            bounded_normalized,
        )
        if bounded_match:
            hour = int(bounded_match.group(1))
            minute = int(bounded_match.group(2) or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return time(hour=hour, minute=minute)

        bounded_spaced_match = re.fullmatch(
            r"(?:а|тоді|давай|давайте|можна)\s+(?:(?:о|на)\s+)?(\d{1,2})\s+(\d{2})",
            bounded_normalized,
        )
        if bounded_spaced_match:
            hour = int(bounded_spaced_match.group(1))
            minute = int(bounded_spaced_match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return time(hour=hour, minute=minute)

        # "а" is matched as a standalone word so it never fires on the
        # trailing "а" of "на" (e.g. "не на 14" must not be sliced into a
        # fake "а 14" replacement clause).
        standalone_a_matches = list(re.finditer(r"\bа\b", normalized))
        marker_positions = [
            standalone_a_matches[-1].start() if standalone_a_matches else -1,
            normalized.rfind("ні"),
            normalized.rfind("краще"),
            normalized.rfind("тоді"),
            normalized.rfind("давай"),
            normalized.rfind("давайте"),
            normalized.rfind("можна"),
            normalized.rfind("мав на увазі"),
            normalized.rfind("мала на увазі"),
            normalized.rfind("маю на увазі"),
            normalized.rfind("rather"),
            normalized.rfind("instead"),
            normalized.rfind("mean"),
            normalized.rfind("meant"),
        ]
        last_marker = max(marker_positions)
        if last_marker >= 0:
            parsed = self._parse_time_only(normalized[last_marker:])
            if parsed is not None:
                return parsed
            candidates = self._parse_loose_hour_candidates(normalized[last_marker:])
            if candidates:
                return candidates[-1]

        candidates = self._parse_loose_hour_candidates(text)
        has_negative_time_replacement = any(
            marker in normalized for marker in ["не ", "not "]
        ) and len(candidates) >= 2
        return candidates[-1] if has_negative_time_replacement else None

    def handle_service_correction(
        self,
        sender_id: str,
        message_text: str,
        service: dict[str, Any],
    ) -> Dict[str, Any] | None:
        pending = self._get_pending_confirmation(sender_id)
        if not pending:
            return None

        language = pending.get("language") or self._detect_language(message_text)
        service_id = service.get("id")
        service_name = service.get("name")
        if service_id:
            pending["service_id"] = str(service_id)
            pending["current_service_id"] = str(service_id)
        if service_name:
            pending["service_name"] = str(service_name)
            pending["current_service_name"] = str(service_name)
        pending["context_summary"] = message_text[:280]
        pending["customer_name"] = None
        self._save_pending_confirmation(sender_id, pending)

        return {
            "status": "waiting_for_contact",
            "reply_text": self._build_contact_retry_reply(language),
            "event_created": False,
            "requires_contact": True,
            "booking_state": BookingState.WAITING_FOR_CONTACT.value,
            "service_id": str(service_id) if service_id else None,
            "start_dt": pending.get("start_dt"),
        }

    def _deserialize_pending_requested_date(self, value: Any) -> date | None:
        if isinstance(value, datetime):
            return self._deserialize_pending_start_dt(value).date()
        if isinstance(value, date):
            return value
        if isinstance(value, str) and value.strip():
            return date.fromisoformat(value)
        return None

    def _combine_requested_date_and_time(self, requested_date: date, requested_time: time) -> datetime:
        return datetime.combine(requested_date, requested_time, tzinfo=self.timezone)

    def _preserve_pending_date_context_after_time_rejection(
        self,
        sender_id: str,
        *,
        previous_pending: dict[str, Any] | None,
        language: str,
        source_channel: str | None,
    ) -> date | None:
        if not previous_pending:
            return None

        previous_pending_state = previous_pending.get("state")
        if previous_pending_state not in {
            BookingState.WAITING_FOR_TIME.value,
            BookingState.WAITING_FOR_CONTACT.value,
        }:
            return None

        if not previous_pending.get("requested_date") and not previous_pending.get("start_dt"):
            return None

        try:
            if previous_pending.get("requested_date"):
                preserved_requested_date = self._deserialize_pending_requested_date(
                    previous_pending.get("requested_date")
                )
            else:
                preserved_requested_date = self._get_pending_start_dt(previous_pending).date()
        except Exception:
            logger.warning(
                "invalid pending date ignored while preserving time rejection sender_id=%s",
                sender_id,
            )
            return None

        self._save_booking_state(
            sender_id,
            state=BookingState.WAITING_FOR_TIME,
            language=previous_pending.get("language") or language,
            source_channel=source_channel or previous_pending.get("source_channel"),
            context_summary=previous_pending.get("context_summary"),
            requested_date=preserved_requested_date,
            requested_day_label=previous_pending.get("requested_day_label"),
            customer_name=previous_pending.get("customer_name"),
            contact_email=previous_pending.get("contact_email"),
            contact_phone=previous_pending.get("contact_phone"),
        )
        preserved_pending = self._get_pending_confirmation(sender_id) or {}
        if previous_pending.get("current_service_id"):
            preserved_pending["current_service_id"] = previous_pending.get("current_service_id")
        if previous_pending.get("current_service_name"):
            preserved_pending["current_service_name"] = previous_pending.get("current_service_name")
        self._save_pending_confirmation(sender_id, preserved_pending)

        return preserved_requested_date

    def _get_pending_start_dt(self, pending: dict[str, Any]) -> datetime | None:
        raw_start_dt = pending.get("start_dt")
        if not raw_start_dt:
            return None
        return self._deserialize_pending_start_dt(raw_start_dt)

    def _offer_verified_alternative_after_rejection(
        self,
        *,
        sender_id: str,
        pending: dict[str, Any],
        language: str,
        state: BookingState,
        source_channel: str | None,
        partial_date: dict[str, Any] | None,
        rejected_time: time,
    ) -> Dict[str, Any] | None:
        """Offers a Calendar-verified slot after the user rejects a time,
        without ever checking the rejected time itself, and without
        clearing the pending booking."""
        pending_start_dt = None
        if state == BookingState.WAITING_FOR_CONTACT:
            try:
                pending_start_dt = self._get_pending_start_dt(pending)
            except Exception:
                pending_start_dt = None

        pending_requested_date = None
        if pending.get("requested_date"):
            try:
                pending_requested_date = self._deserialize_pending_requested_date(
                    pending.get("requested_date")
                )
            except Exception:
                pending_requested_date = None

        target_date = partial_date["date"] if partial_date else None
        if target_date is None:
            target_date = (
                pending_start_dt.date() if pending_start_dt is not None else pending_requested_date
            )
        if target_date is None:
            return None

        rejected_dt = self._combine_requested_date_and_time(target_date, rejected_time)
        try:
            next_slot = self._find_next_available_slot(rejected_dt)
        except Exception:
            logger.exception(
                "alternative-slot lookup failed after rejection sender_id=%s rejected_dt=%s",
                sender_id,
                rejected_dt.isoformat(),
            )
            return None
        if next_slot is None:
            return None

        self._save_booking_state(
            sender_id,
            state=BookingState.WAITING_FOR_TIME,
            language=language,
            source_channel=source_channel or pending.get("source_channel"),
            context_summary=pending.get("context_summary"),
            requested_date=target_date,
            requested_day_label=pending.get("requested_day_label"),
            customer_name=pending.get("customer_name"),
            contact_email=pending.get("contact_email"),
            contact_phone=pending.get("contact_phone"),
        )
        refreshed_pending = self._get_pending_confirmation(sender_id) or {}
        if pending.get("current_service_id"):
            refreshed_pending["current_service_id"] = pending.get("current_service_id")
        if pending.get("current_service_name"):
            refreshed_pending["current_service_name"] = pending.get("current_service_name")
        refreshed_pending["availability_context"] = True
        refreshed_pending["suggested_slots"] = [
            {
                "day_key": "selected_day",
                "start_dt": self._serialize_pending_start_dt(next_slot),
            }
        ]
        refreshed_pending["last_suggested_day"] = "selected_day"
        self._save_pending_confirmation(sender_id, refreshed_pending)

        return {
            "status": "slot_suggested",
            "reply_text": (
                f"Добре, {self._format_scheduled_time_for_reply(rejected_dt, language)} "
                f"прибираю. Як щодо {self._format_scheduled_time_for_reply(next_slot, language)}?"
            ),
            "booking_state": BookingState.WAITING_FOR_TIME.value,
            "requires_contact": False,
            "suggested_slots": refreshed_pending["suggested_slots"],
            "start_dt": next_slot.isoformat(),
        }

    def _merge_booking_correction(
        self,
        *,
        sender_id: str,
        message_text: str,
        pending: dict[str, Any],
        language: str,
        state: BookingState,
        source_channel: str | None,
    ) -> Dict[str, Any] | None:
        if state == BookingState.WAITING_FOR_TIME and pending.get("availability_context"):
            return None

        looks_like_correction = self._looks_like_booking_correction(message_text)
        corrected_time = self._extract_corrected_time(message_text) if looks_like_correction else None
        rejected_time, negation_replacement_time, wants_alternative = (
            self._extract_negated_time_context(message_text)
        )
        if corrected_time is None and negation_replacement_time is not None:
            corrected_time = negation_replacement_time
        if (
            corrected_time is None
            and state == BookingState.WAITING_FOR_CONTACT
            and rejected_time is None
        ):
            # A negation-blind fallback here would otherwise let a purely
            # rejected time (e.g. "точно не о 14") become the selected one.
            corrected_time = self._parse_time_only(message_text)

        partial_date = self._extract_requested_date(message_text)

        if corrected_time is None and rejected_time is not None and wants_alternative:
            # "о 14 не можу, є інший час?" -- the rejected time must never be
            # checked/selected, and this must not fall through to generic
            # postpone/cancellation handling (e.g. a bare "пізніше" marker).
            alternative_result = self._offer_verified_alternative_after_rejection(
                sender_id=sender_id,
                pending=pending,
                language=language,
                state=state,
                source_channel=source_channel,
                partial_date=partial_date,
                rejected_time=rejected_time,
            )
            if alternative_result is not None:
                return alternative_result

        if not looks_like_correction and corrected_time is None:
            return None

        if partial_date is None and corrected_time is None:
            return None

        pending_start_dt = None
        if state == BookingState.WAITING_FOR_CONTACT:
            try:
                pending_start_dt = self._get_pending_start_dt(pending)
            except Exception:
                logger.warning(
                    "invalid pending start_dt ignored during correction sender_id=%s raw_start_dt=%r",
                    sender_id,
                    pending.get("start_dt"),
                )

        pending_requested_date = None
        if pending.get("requested_date"):
            try:
                pending_requested_date = self._deserialize_pending_requested_date(
                    pending.get("requested_date")
                )
            except Exception:
                logger.warning(
                    "invalid pending requested_date ignored during correction sender_id=%s raw_requested_date=%r",
                    sender_id,
                    pending.get("requested_date"),
                )

        target_date = partial_date["date"] if partial_date else None
        if target_date is None:
            if pending_start_dt is not None:
                target_date = pending_start_dt.date()
            else:
                target_date = pending_requested_date

        target_time = corrected_time
        if target_time is None and pending_start_dt is not None:
            target_time = pending_start_dt.timetz().replace(tzinfo=None)

        if target_date is not None:
            pending["requested_date"] = target_date.isoformat()
        if partial_date is not None:
            pending["requested_day_label"] = partial_date.get("day_label")

        if target_date is not None and target_time is not None:
            requested_dt = self._combine_requested_date_and_time(target_date, target_time)
            return self.start_booking_flow(
                sender_id=sender_id,
                message_text=message_text,
                source_channel=source_channel or pending.get("source_channel"),
                requested_dt_override=requested_dt,
                current_service_id=pending.get("current_service_id"),
                current_service_name=pending.get("current_service_name"),
            )

        self._save_pending_confirmation(sender_id, pending)
        if target_date is not None:
            return {
                "status": "waiting_for_time",
                "reply_text": self._build_missing_time_reply(
                    language,
                    pending.get("requested_day_label"),
                ),
                "event_created": False,
                "requires_confirmation": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
                "requested_date": pending.get("requested_date"),
            }

        return None

    def _parse_requested_datetime(self, text: str) -> datetime | None:
        now = datetime.now(self.timezone)
        normalized = self._normalize_booking_text(text)

        match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ ,T]+(\d{1,2}):(\d{2})", normalized)
        if match:
            year, month, day, hour, minute = map(int, match.groups())
            return datetime(year, month, day, hour, minute, tzinfo=self.timezone)

        match = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?[ ,]+(\d{1,2}):(\d{2})", normalized)
        if match:
            day = int(match.group(1))
            month = int(match.group(2))
            parsed_date = self._next_calendar_date(
                day=day,
                month=month,
                explicit_year=int(match.group(3)) if match.group(3) else None,
            )
            if parsed_date is None:
                return None
            hour = int(match.group(4))
            minute = int(match.group(5))
            return datetime.combine(parsed_date, time(hour, minute), tzinfo=self.timezone)

        time_match = re.search(r"(\d{1,2}):(\d{2})", normalized)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))

            if "післязавтра" in normalized or "day after tomorrow" in normalized:
                base = now + timedelta(days=2)
                return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if "завтра" in normalized or "tomorrow" in normalized:
                base = now + timedelta(days=1)
                return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if "сьогодні" in normalized or "today" in normalized:
                return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        requested_time = self._parse_time_only(normalized)
        if requested_time is None:
            for marker in ["післязавтра", "day after tomorrow", "завтра", "tomorrow", "сьогодні", "today"]:
                marker_index = normalized.find(marker)
                if marker_index < 0:
                    continue
                suffix = normalized[marker_index + len(marker) :]
                suffix_time = self._parse_time_only(suffix.strip(" ,.;"))
                if suffix_time is not None:
                    requested_time = suffix_time
                    break
        weekday_match = self._find_weekday_marker(normalized)
        matched_weekday = weekday_match[1] if weekday_match is not None else None
        if requested_time is None and weekday_match is not None:
            weekday_marker = weekday_match[0]
            marker_index = normalized.find(weekday_marker)
            suffix = normalized[marker_index + len(weekday_marker) :] if marker_index >= 0 else ""
            suffix_time = self._parse_time_only(suffix.strip(" ,.;"))
            if suffix_time is not None:
                requested_time = suffix_time

        if requested_time is not None and matched_weekday is not None:
            days_ahead = (matched_weekday - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            base = now + timedelta(days=days_ahead)
            return base.replace(
                hour=requested_time.hour,
                minute=requested_time.minute,
                second=0,
                microsecond=0,
            )

        absolute_date = self._extract_absolute_calendar_date(normalized)
        if requested_time is not None and absolute_date is not None:
            return datetime.combine(
                absolute_date["date"],
                requested_time,
                tzinfo=self.timezone,
            )

        if requested_time is not None and (
            "завтра" in normalized
            or "tomorrow" in normalized
            or "післязавтра" in normalized
            or "day after tomorrow" in normalized
            or "сьогодні" in normalized
            or "today" in normalized
        ):
            if "післязавтра" in normalized or "day after tomorrow" in normalized:
                base = now + timedelta(days=2)
                return base.replace(
                    hour=requested_time.hour,
                    minute=requested_time.minute,
                    second=0,
                    microsecond=0,
                )

            if "завтра" in normalized or "tomorrow" in normalized:
                base = now + timedelta(days=1)
                return base.replace(
                    hour=requested_time.hour,
                    minute=requested_time.minute,
                    second=0,
                    microsecond=0,
                )

            if "сьогодні" in normalized or "today" in normalized:
                return now.replace(
                    hour=requested_time.hour,
                    minute=requested_time.minute,
                    second=0,
                    microsecond=0,
                )

        return None

    def handle_booking_request(self, sender_id: str, message_text: str) -> Dict[str, Any]:
        return self.start_booking_flow(sender_id=sender_id, message_text=message_text)

    def _service_required_for_booking(self) -> bool:
        return self.front_desk_config_service is not None

    def _text_has_service_hint(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        service_markers = [
            "чистк",
            "гігієн",
            "консультац",
            "карієс",
            "пломб",
            "відбілюван",
            "протез",
            "імплант",
            "дитяч",
        ]
        return any(marker in normalized for marker in service_markers)

    def _save_missing_service_context(
        self,
        *,
        sender_id: str,
        language: str,
        message_text: str,
        source_channel: str | None,
        requested_dt: datetime | None = None,
        requested_date: date | None = None,
        requested_day_label: str | None = None,
        contact_details: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        contact_details = contact_details or self._empty_contact_details()
        preserved_date = requested_date or (requested_dt.date() if requested_dt else None)
        self._save_booking_state(
            sender_id,
            state=BookingState.WAITING_FOR_TIME,
            language=language,
            start_dt=requested_dt,
            source_channel=source_channel,
            context_summary=message_text[:280],
            requested_date=preserved_date,
            requested_day_label=requested_day_label,
            customer_name=contact_details["customer_name"],
            contact_email=contact_details["email"],
            contact_phone=contact_details["phone"],
        )
        pending = self._get_pending_confirmation(sender_id) or {}
        pending["missing_service"] = True
        self._save_pending_confirmation(sender_id, pending)
        return {
            "status": "waiting_for_service",
            "reply_text": self._build_missing_service_reply(
                language, pain_mentioned=self._looks_like_dental_pain_mention(message_text)
            ),
            "requires_confirmation": False,
            "event_created": False,
            "booking_state": BookingState.WAITING_FOR_TIME.value,
            "start_dt": requested_dt.isoformat() if requested_dt else None,
            "requested_date": preserved_date.isoformat() if preserved_date else None,
        }

    def start_booking_flow(
        self,
        sender_id: str,
        message_text: str,
        source_channel: str | None = None,
        requested_dt_override: datetime | None = None,
        current_service_id: str | None = None,
        current_service_name: str | None = None,
    ) -> Dict[str, Any]:
        language = self._detect_language(message_text)
        time_window = self._extract_time_window(message_text)
        requested_dt = requested_dt_override or (None if time_window else self._parse_requested_datetime(message_text))
        partial_date = self._extract_requested_date(message_text)
        daypart = self._extract_daypart(message_text)
        if requested_dt_override is None and requested_dt is not None:
            rejected_time, negation_replacement_time, _wants_alternative = (
                self._extract_negated_time_context(message_text)
            )
            if rejected_time is not None and negation_replacement_time is None:
                requested_dt = None

        if not self._booking_enabled():
            return {
                "status": "booking_disabled",
                "reply_text": "Передам команді, щоб підібрали зручний час.",
                "requires_confirmation": False,
                "booking_state": BookingState.NONE.value,
                "start_dt": requested_dt.isoformat() if requested_dt else None,
            }

        logger.info("Entered start_booking_flow sender_id=%s", sender_id)
        logger.info(
            "booking request sender_id=%s has_text=%s has_parsed_dt=%s",
            sender_id,
            bool(message_text),
            requested_dt is not None,
        )
        service_known = bool(current_service_id)

        idempotency_contact_details = (
            self._extract_anchored_contact_details(message_text)
            if requested_dt is not None and (self.PHONE_RE.search(message_text) or self.EMAIL_RE.search(message_text))
            else self._empty_contact_details()
        )
        if requested_dt is not None and (
            idempotency_contact_details["email"] or idempotency_contact_details["phone"]
        ):
            completed_match = self._completed_booking_matches_candidate(
                sender_id,
                start_dt=requested_dt,
                email=idempotency_contact_details["email"],
                phone=idempotency_contact_details["phone"],
                current_service_id=current_service_id,
            )
            if completed_match is not None:
                logger.info(
                    "Booking semantic retry skipped before service/availability sender_id=%s start_dt=%s",
                    sender_id,
                    requested_dt.isoformat(),
                )
                self._clear_pending_confirmation(sender_id)
                return self._build_idempotent_confirmed_result(
                    language=language,
                    completed=completed_match,
                )

        if (
            self._service_required_for_booking()
            and not service_known
        ):
            requested_date = partial_date.get("date") if partial_date else None
            requested_day_label = partial_date.get("day_label") if partial_date else None
            early_contact_details = (
                idempotency_contact_details
                if self.PHONE_RE.search(message_text) or self.EMAIL_RE.search(message_text)
                else self._empty_contact_details()
            )
            return self._save_missing_service_context(
                sender_id=sender_id,
                language=language,
                message_text=message_text,
                source_channel=source_channel,
                requested_dt=requested_dt,
                requested_date=requested_date,
                requested_day_label=requested_day_label,
                contact_details=early_contact_details,
            )

        if requested_dt is None:
            requested_date = partial_date.get("date") if partial_date else None
            requested_day_label = partial_date.get("day_label") if partial_date else None
            previous_pending = self._get_pending_confirmation(sender_id) or {}
            fresh_contact_details = (
                self._extract_anchored_contact_details(message_text)
                if self.PHONE_RE.search(message_text) or self.EMAIL_RE.search(message_text)
                else self._extract_trailing_contact_details(message_text)
            )
            early_contact_details = self._merge_contact_details_with_pending(
                fresh_contact_details,
                previous_pending,
                message_text,
            )
            requested_date = requested_date or (
                self._deserialize_pending_requested_date(previous_pending.get("requested_date"))
                if previous_pending.get("requested_date")
                else None
            )
            requested_day_label = requested_day_label or previous_pending.get("requested_day_label")
            if requested_date is None and self._looks_like_nearest_availability_request(message_text):
                return self.handle_nearest_availability_request(
                    sender_id=sender_id,
                    message_text=message_text,
                    source_channel=source_channel or previous_pending.get("source_channel"),
                    current_service_id=current_service_id or previous_pending.get("current_service_id"),
                    current_service_name=current_service_name or previous_pending.get("current_service_name"),
                )
            if requested_date is not None and time_window is not None:
                return self._suggest_time_window_slots(
                    sender_id,
                    language=language,
                    message_text=message_text,
                    source_channel=source_channel,
                    requested_date=requested_date,
                    requested_day_label=requested_day_label,
                    time_window=time_window,
                    current_service_id=current_service_id,
                    current_service_name=current_service_name,
                )
            if requested_date is not None and daypart is not None:
                return self._suggest_daypart_slots(
                    sender_id,
                    language=language,
                    message_text=message_text,
                    source_channel=source_channel,
                    requested_date=requested_date,
                    requested_day_label=requested_day_label,
                    daypart=daypart,
                    current_service_id=current_service_id,
                    current_service_name=current_service_name,
                )
            self._save_booking_state(
                sender_id,
                state=BookingState.WAITING_FOR_TIME,
                language=language,
                source_channel=source_channel or previous_pending.get("source_channel"),
                context_summary=previous_pending.get("context_summary") or message_text[:280],
                requested_date=requested_date,
                requested_day_label=requested_day_label,
                customer_name=early_contact_details["customer_name"],
                contact_email=early_contact_details["email"],
                contact_phone=early_contact_details["phone"],
            )
            pending = self._get_pending_confirmation(sender_id) or {}
            if current_service_id:
                pending["current_service_id"] = current_service_id
            elif previous_pending.get("current_service_id"):
                pending["current_service_id"] = previous_pending["current_service_id"]
            if current_service_name:
                pending["current_service_name"] = current_service_name
            elif previous_pending.get("current_service_name"):
                pending["current_service_name"] = previous_pending["current_service_name"]
            if pending.get("current_service_id") or pending.get("current_service_name"):
                self._save_pending_confirmation(sender_id, pending)
            return {
                "status": "waiting_for_time",
                "reply_text": (
                    self._build_missing_time_reply(language, requested_day_label)
                    if requested_date
                    else self._build_unclear_time_reply(
                        language, pain_mentioned=self._looks_like_dental_pain_mention(message_text)
                    )
                ),
                "requires_confirmation": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
                "start_dt": None,
                "requested_date": requested_date.isoformat() if requested_date else None,
            }

        previous_pending = self._get_pending_confirmation(sender_id)
        duration_minutes = self._booking_duration_minutes()

        if not self._is_within_business_hours(requested_dt, duration_minutes):
            preserved_requested_date = None
            if requested_dt_override is not None:
                preserved_requested_date = self._preserve_pending_date_context_after_time_rejection(
                    sender_id,
                    previous_pending=previous_pending,
                    language=language,
                    source_channel=source_channel or (
                        previous_pending.get("source_channel") if previous_pending else None
                    ),
                )
            if preserved_requested_date is None:
                if previous_pending or current_service_id:
                    self._save_booking_state(
                        sender_id,
                        state=BookingState.WAITING_FOR_TIME,
                        language=language,
                        source_channel=source_channel or (
                            previous_pending.get("source_channel") if previous_pending else None
                        ),
                        context_summary=(
                            previous_pending.get("context_summary")
                            if previous_pending
                            else message_text[:280]
                        ),
                        requested_date=requested_dt.date(),
                        requested_day_label=self._format_date_label_for_reply(
                            requested_dt.date(),
                            language,
                        ),
                        customer_name=(
                            previous_pending.get("customer_name") if previous_pending else None
                        ),
                        contact_email=(
                            previous_pending.get("contact_email") if previous_pending else None
                        ),
                        contact_phone=(
                            previous_pending.get("contact_phone") if previous_pending else None
                        ),
                    )
                    pending = self._get_pending_confirmation(sender_id) or {}
                    if current_service_id:
                        pending["current_service_id"] = current_service_id
                    elif previous_pending and previous_pending.get("current_service_id"):
                        pending["current_service_id"] = previous_pending["current_service_id"]
                    if current_service_name:
                        pending["current_service_name"] = current_service_name
                    elif previous_pending and previous_pending.get("current_service_name"):
                        pending["current_service_name"] = previous_pending["current_service_name"]
                    if pending.get("current_service_id") or pending.get("current_service_name"):
                        self._save_pending_confirmation(sender_id, pending)
                    preserved_requested_date = requested_dt.date()
                else:
                    self._clear_pending_confirmation(sender_id)
            return {
                "status": "outside_business_hours",
                "reply_text": self._build_outside_business_hours_reply(language),
                "requires_confirmation": False,
                "booking_state": (
                    BookingState.WAITING_FOR_TIME.value
                    if preserved_requested_date is not None
                    else BookingState.NONE.value
                ),
                "start_dt": requested_dt.isoformat(),
                "requested_date": (
                    preserved_requested_date.isoformat()
                    if preserved_requested_date is not None
                    else None
                ),
            }

        early_contact_details = self._merge_contact_details_with_pending(
            self._extract_anchored_contact_details(message_text),
            previous_pending,
            message_text,
        )
        if self._has_required_contact(
            customer_name=early_contact_details["customer_name"],
            email=early_contact_details["email"],
            phone=early_contact_details["phone"],
        ):
            completed_match = self._completed_booking_matches_candidate(
                sender_id,
                start_dt=requested_dt,
                email=early_contact_details["email"],
                phone=early_contact_details["phone"],
                current_service_id=current_service_id,
            )
            if completed_match is not None:
                logger.info(
                    "Booking semantic retry skipped before availability check sender_id=%s start_dt=%s",
                    sender_id,
                    requested_dt.isoformat(),
                )
                self._clear_pending_confirmation(sender_id)
                return self._build_idempotent_confirmed_result(
                    language=language,
                    completed=completed_match,
                )

        try:
            is_available = self.calendar_service.check_specific_time_availability(
                start_dt=requested_dt,
                duration_minutes=duration_minutes,
            )
        except Exception:
            logger.exception(
                "booking availability check failed sender_id=%s start_dt=%s",
                sender_id,
                requested_dt.isoformat(),
            )
            preserved_requested_date = None
            if requested_dt_override is not None:
                preserved_requested_date = self._preserve_pending_date_context_after_time_rejection(
                    sender_id,
                    previous_pending=previous_pending,
                    language=language,
                    source_channel=source_channel or (
                        previous_pending.get("source_channel") if previous_pending else None
                    ),
                )
            return {
                "status": "availability_check_failed",
                "reply_text": self._build_availability_check_failed_reply(language),
                "requires_confirmation": False,
                "event_created": False,
                "booking_state": (
                    BookingState.WAITING_FOR_TIME.value
                    if preserved_requested_date is not None
                    else previous_pending.get("state")
                    if previous_pending
                    else BookingState.NONE.value
                ),
                "start_dt": requested_dt.isoformat(),
                "requested_date": (
                    preserved_requested_date.isoformat()
                    if preserved_requested_date is not None
                    else None
                ),
            }

        logger.info(
            "booking availability sender_id=%s start_dt=%s is_available=%s",
            sender_id,
            requested_dt.isoformat(),
            is_available,
        )

        if not is_available:
            try:
                next_slot = self._find_next_available_slot(requested_dt)
            except Exception:
                logger.exception(
                    "booking next-slot availability check failed sender_id=%s start_dt=%s",
                    sender_id,
                    requested_dt.isoformat(),
                )
                return {
                    "status": "availability_check_failed",
                    "reply_text": self._build_availability_check_failed_reply(language),
                    "requires_confirmation": False,
                    "event_created": False,
                    "booking_state": (
                        previous_pending.get("state")
                        if previous_pending
                        else BookingState.NONE.value
                    ),
                    "start_dt": requested_dt.isoformat(),
                }
            self._clear_pending_confirmation(sender_id)
            if next_slot:
                previous_pending_state = previous_pending.get("state") if previous_pending else None
                is_pending_time_selection = (
                    requested_dt_override is not None
                    and previous_pending_state == BookingState.WAITING_FOR_TIME.value
                )
                is_selected_time_correction = (
                    requested_dt_override is not None
                    and previous_pending_state == BookingState.WAITING_FOR_CONTACT.value
                )
                if is_selected_time_correction:
                    preserved_requested_date = self._preserve_pending_date_context_after_time_rejection(
                        sender_id,
                        previous_pending=previous_pending,
                        language=language,
                        source_channel=source_channel or (
                            previous_pending.get("source_channel") if previous_pending else None
                        ),
                    )
                    pending = self._get_pending_confirmation(sender_id) or {}
                    if preserved_requested_date is None:
                        self._save_booking_state(
                            sender_id,
                            state=BookingState.WAITING_FOR_TIME,
                            language=language,
                            source_channel=source_channel,
                            context_summary=message_text[:280],
                            requested_date=requested_dt.date(),
                            customer_name=(
                                previous_pending.get("customer_name") if previous_pending else None
                            ),
                            contact_email=(
                                previous_pending.get("contact_email") if previous_pending else None
                            ),
                            contact_phone=(
                                previous_pending.get("contact_phone") if previous_pending else None
                            ),
                        )
                        pending = self._get_pending_confirmation(sender_id) or {}
                elif is_pending_time_selection:
                    self._save_booking_state(
                        sender_id,
                        state=BookingState.WAITING_FOR_TIME,
                        language=language,
                        source_channel=source_channel or (
                            previous_pending.get("source_channel") if previous_pending else None
                        ),
                        context_summary=(
                            previous_pending.get("context_summary")
                            if previous_pending
                            else message_text[:280]
                        ),
                        requested_date=(
                            previous_pending.get("requested_date")
                            if previous_pending
                            else requested_dt.date()
                        ),
                        requested_day_label=(
                            previous_pending.get("requested_day_label")
                            if previous_pending
                            else None
                        ),
                        customer_name=(
                            previous_pending.get("customer_name") if previous_pending else None
                        ),
                        contact_email=(
                            previous_pending.get("contact_email") if previous_pending else None
                        ),
                        contact_phone=(
                            previous_pending.get("contact_phone") if previous_pending else None
                        ),
                    )
                    pending = self._get_pending_confirmation(sender_id) or {}
                else:
                    self._save_booking_state(
                        sender_id,
                        state=BookingState.WAITING_FOR_TIME,
                        language=language,
                        source_channel=source_channel,
                        context_summary=message_text[:280],
                        requested_date=requested_dt.date(),
                        requested_day_label=self._format_date_label_for_reply(
                            requested_dt.date(),
                            language,
                        ),
                        customer_name=(
                            early_contact_details["customer_name"] if previous_pending else None
                        ),
                        contact_email=early_contact_details["email"] if previous_pending else None,
                        contact_phone=early_contact_details["phone"] if previous_pending else None,
                    )
                    pending = self._get_pending_confirmation(sender_id) or {}

                pending["availability_context"] = True
                pending["busy_alternative"] = True
                pending["suggested_slots"] = [
                    {
                        "day_key": (
                            "selected_day"
                            if (is_selected_time_correction or not is_pending_time_selection)
                            else "tomorrow"
                        ),
                        "start_dt": self._serialize_pending_start_dt(next_slot),
                    }
                ]
                pending["last_suggested_day"] = (
                    "selected_day"
                    if (is_selected_time_correction or not is_pending_time_selection)
                    else "tomorrow"
                )
                self._save_pending_confirmation(sender_id, pending)

                return {
                    "status": "slot_suggested",
                    "reply_text": f"На жаль, {self._format_scheduled_time_for_reply(requested_dt, language)} зайнятий. Як щодо {self._format_scheduled_time_for_reply(next_slot, language)}?",
                    "booking_state": BookingState.WAITING_FOR_TIME.value,
                    "requires_contact": False,
                    "suggested_slots": pending["suggested_slots"],
                    "start_dt": next_slot.isoformat(),
                }

            return {
                "status": "unavailable",
                "reply_text": self._build_unavailable_reply(language),
                "requires_confirmation": False,
                "event_created": False,
                "start_dt": requested_dt.isoformat(),
            }

        contact_details = early_contact_details
        if (
            self._service_required_for_booking()
            and not service_known
            and (
                (previous_pending and previous_pending.get("missing_service"))
                or contact_details["email"]
                or contact_details["phone"]
            )
        ):
            self._save_booking_state(
                sender_id,
                state=BookingState.WAITING_FOR_TIME,
                language=language,
                start_dt=requested_dt,
                source_channel=source_channel,
                context_summary=message_text[:280],
                requested_date=requested_dt.date(),
                customer_name=contact_details["customer_name"],
                contact_email=contact_details["email"],
                contact_phone=contact_details["phone"],
            )
            return {
                "status": "waiting_for_service",
                "reply_text": self._build_missing_service_reply(language),
                "requires_confirmation": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
                "start_dt": requested_dt.isoformat(),
            }

        self._clear_pending_confirmation(sender_id)

        if self._has_required_contact(
            customer_name=contact_details["customer_name"],
            email=contact_details["email"],
            phone=contact_details["phone"],
        ):
            try:
                description_parts = [self._booking_description_prefix()]
                if contact_details["customer_name"]:
                    description_parts.append(f"Customer name: {contact_details['customer_name']}")
                description_parts.append(f"Sender ID: {sender_id}")
                if source_channel:
                    description_parts.append(f"Source: {source_channel}")
                description_parts.append(f"Context: {message_text[:280]}")

                contact_parts = []
                if contact_details["email"]:
                    contact_parts.append(f"Email: {contact_details['email']}")
                if contact_details["phone"]:
                    contact_parts.append(f"Phone: {contact_details['phone']}")
                if contact_parts:
                    description_parts.append("Contact: " + " | ".join(contact_parts))

                description = "\n".join(description_parts)

                completed_match = self._completed_booking_matches_candidate(
                    sender_id,
                    start_dt=requested_dt,
                    email=contact_details["email"],
                    phone=contact_details["phone"],
                    current_service_id=current_service_id,
                )
                if completed_match is not None:
                    logger.info(
                        "Immediate booking semantic retry skipped sender_id=%s start_dt=%s",
                        sender_id,
                        requested_dt.isoformat(),
                    )
                    return self._build_idempotent_confirmed_result(
                        language=language,
                        completed=completed_match,
                    )

                calendar_configured = bool(
                    self.calendar_service.google_calendar_client
                    and self.calendar_service.google_calendar_client.is_configured()
                )
                logger.info("Calendar configured: %s", calendar_configured)

                if calendar_configured:
                    created = self.calendar_service.create_booking_event(
                        start_dt=requested_dt,
                        duration_minutes=self._booking_duration_minutes(),
                        summary=f"{self._appointment_label().capitalize()} booking",
                        description=description,
                        attendee_emails=[],
                    )
                    self._save_captured_contact(
                        sender_id,
                        customer_name=contact_details["customer_name"],
                        email=contact_details["email"],
                        phone=contact_details["phone"],
                        start_dt=requested_dt,
                    )
                    self._mark_booking_completed(
                        sender_id,
                        start_dt=requested_dt,
                        customer_name=contact_details["customer_name"],
                        email=contact_details["email"],
                        phone=contact_details["phone"],
                        calendar_event_id=created.event_id,
                        current_service_id=current_service_id,
                        current_service_name=current_service_name,
                    )
                    logger.info(
                        "Calendar event created for immediate booking sender_id=%s event_id=%s",
                        sender_id,
                        created.event_id,
                    )
                    return {
                        "status": "confirmed",
                        "reply_text": self._build_both_contacts_confirmed_reply(
                            language,
                            requested_dt,
                            contact_details["customer_name"],
                        ),
                        "event_created": True,
                        "booking_state": BookingState.NONE.value,
                        "event_id": created.event_id,
                        "event_link": created.html_link,
                        "customer_name": contact_details["customer_name"],
                        "contact_email": contact_details["email"],
                        "contact_phone": contact_details["phone"],
                    }
                else:
                    logger.warning(
                        "Google Calendar is not configured; switching immediate booking to manual follow-up sender_id=%s",
                        sender_id,
                    )
                    return self._build_manual_followup_result(
                        sender_id=sender_id,
                        language=language,
                        start_dt=requested_dt,
                        customer_name=contact_details["customer_name"],
                        email=contact_details["email"],
                        phone=contact_details["phone"],
                    )

            except Exception:
                logger.exception("Immediate booking creation failed")
                return self._build_manual_followup_result(
                    sender_id=sender_id,
                    language=language,
                    start_dt=requested_dt,
                    customer_name=contact_details["customer_name"],
                    email=contact_details["email"],
                    phone=contact_details["phone"],
                )

        self._save_booking_state(
            sender_id,
            state=BookingState.WAITING_FOR_CONTACT,
            language=language,
            start_dt=requested_dt,
            source_channel=source_channel,
            context_summary=message_text[:280],
            customer_name=(
                contact_details["customer_name"]
                if (
                    contact_details["has_phone"]
                    or contact_details["has_email"]
                    or (previous_pending and previous_pending.get("customer_name"))
                )
                else None
            ),
            contact_email=contact_details["email"],
            contact_phone=contact_details["phone"],
        )
        pending = self._get_pending_confirmation(sender_id) or {}
        if current_service_id:
            pending["current_service_id"] = current_service_id
        if current_service_name:
            pending["current_service_name"] = current_service_name
        if current_service_id or current_service_name:
            self._save_pending_confirmation(sender_id, pending)

        return {
            "status": "waiting_for_contact",
            "reply_text": self._build_available_reply(language, requested_dt),
            "requires_confirmation": False,
            "requires_contact": True,
            "booking_state": BookingState.WAITING_FOR_CONTACT.value,
            "start_dt": requested_dt.isoformat(),
        }

    def handle_booking_confirmation(self, sender_id: str, message_text: str) -> Dict[str, Any] | None:
        return self.process_booking_message(sender_id=sender_id, message_text=message_text)

    def process_booking_message(
        self,
        sender_id: str,
        message_text: str,
        source_channel: str | None = None,
    ) -> Dict[str, Any] | None:
        pending = self._get_pending_confirmation(sender_id)
        if not pending:
            return None

        logger.info("Entered process_booking_message sender_id=%s", sender_id)
        language = pending["language"]
        state = self.get_booking_state(sender_id)
        logger.info("Booking state: %s", state.value)

        correction_result = self._merge_booking_correction(
            sender_id=sender_id,
            message_text=message_text,
            pending=pending,
            language=language,
            state=state,
            source_channel=source_channel,
        )
        if correction_result is not None:
            return correction_result

        alternative_time_request = pending.get("availability_context") and self._looks_like_another_time_request(
            message_text
        )
        # A bare rejection ("ні") of a single busy-alternative offer means
        # "not that time" -- not "cancel the whole booking". The service and
        # any contact already captured stay intact; only the stale
        # alternative is dropped so it can't be matched against later.
        # Excludes "another time" phrasing ("пізніше"/"раніше") -- those are
        # already handled as a refinement request, not a flat rejection,
        # and "пізніше" is also (confusingly) one of the _is_rejection
        # markers for the unrelated "not now" postponement sense.
        rejects_busy_alternative = (
            bool(pending.get("busy_alternative"))
            and self._is_rejection(message_text)
            and not alternative_time_request
        )
        if rejects_busy_alternative:
            pending["busy_alternative"] = False
            pending["suggested_slots"] = []
            pending.pop("last_suggested_day", None)
            self._save_pending_confirmation(sender_id, pending)
            return {
                "status": "alternative_rejected",
                "reply_text": self._build_alternative_rejected_reply(language),
                "event_created": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
            }

        if self._is_rejection(message_text) and not alternative_time_request:
            self._clear_pending_confirmation(sender_id)
            return {
                "status": "cancelled",
                "reply_text": self._build_cancelled_reply(language),
                "event_created": False,
                "booking_state": BookingState.NONE.value,
            }

        if state == BookingState.WAITING_FOR_TIME:
            if (
                not pending.get("reschedule_pending")
                and not pending.get("busy_alternative")
                and self._looks_like_nearest_availability_request(message_text)
            ):
                return self.handle_nearest_availability_request(
                    sender_id=sender_id,
                    message_text=message_text,
                    source_channel=source_channel or pending.get("source_channel"),
                    current_service_id=pending.get("current_service_id"),
                    current_service_name=pending.get("current_service_name"),
                )

            reschedule_result = self._continue_reschedule_selection(
                sender_id=sender_id,
                message_text=message_text,
                pending=pending,
                source_channel=source_channel,
            )
            if reschedule_result is not None:
                return reschedule_result

            if pending.get("availability_context"):
                availability_result = self._process_availability_followup(
                    sender_id=sender_id,
                    message_text=message_text,
                    pending=pending,
                    source_channel=source_channel,
                )
                if availability_result is not None:
                    return availability_result

            contact_details = (
                self._extract_anchored_contact_details(message_text)
                if self.PHONE_RE.search(message_text) or self.EMAIL_RE.search(message_text)
                else self._extract_waiting_for_contact_details(message_text)
            )
            if contact_details["customer_name"]:
                pending["customer_name"] = contact_details["customer_name"]
            if contact_details["email"]:
                pending["contact_email"] = contact_details["email"]
            if contact_details["phone"]:
                pending["contact_phone"] = contact_details["phone"]
            if contact_details["has_name"] or contact_details["has_email"] or contact_details["has_phone"]:
                self._save_pending_confirmation(sender_id, pending)

            partial_date = self._extract_requested_date(message_text)
            rejected_time, negation_replacement_time, _wants_alternative = (
                self._extract_negated_time_context(message_text)
            )
            if negation_replacement_time is not None:
                requested_time = negation_replacement_time
            elif rejected_time is not None:
                # A rejected time (e.g. "не о 14") must never be treated as
                # the time the user is now requesting.
                requested_time = None
            else:
                requested_time = self._parse_time_only(message_text)
            requested_dt_from_message = None
            if requested_time is None:
                requested_dt_from_message = self._parse_requested_datetime(message_text)
                if requested_dt_from_message is not None:
                    requested_time = requested_dt_from_message.timetz().replace(tzinfo=None)
            daypart = self._extract_daypart(message_text)
            time_window = self._extract_time_window(message_text)
            pending_requested_date = None
            if pending.get("requested_date"):
                try:
                    pending_requested_date = self._deserialize_pending_requested_date(
                        pending.get("requested_date")
                    )
                except Exception:
                    logger.warning(
                        "invalid pending requested_date ignored sender_id=%s raw_requested_date=%r",
                        sender_id,
                        pending.get("requested_date"),
                    )

            if (
                requested_time is None
                and rejected_time is None
                and pending_requested_date is not None
            ):
                requested_time = self._extract_suggested_slot_selection_time(message_text)

            if partial_date:
                pending_requested_date = partial_date["date"]
                if (
                    self._detect_requested_day_key(message_text) is None
                    and pending.get("requested_date") != pending_requested_date.isoformat()
                ):
                    # The user explicitly switched to a different date --
                    # any previously suggested slots were offered for the
                    # old date and must not be treated as valid candidates
                    # (or matched against) for the new one. (A canonical
                    # "tomorrow"/"day after tomorrow" reference matches an
                    # existing suggestion bucket by day key, so it isn't a
                    # switch away from it -- leave that bucket intact.)
                    pending["suggested_slots"] = []
                    pending.pop("last_suggested_day", None)
                pending["requested_date"] = pending_requested_date.isoformat()
                pending["requested_day_label"] = partial_date.get("day_label")
                self._save_pending_confirmation(sender_id, pending)
            elif requested_dt_from_message is not None:
                pending_requested_date = requested_dt_from_message.date()
                pending["requested_date"] = pending_requested_date.isoformat()
                pending["requested_day_label"] = self._format_date_label_for_reply(
                    pending_requested_date,
                    language,
                )
                self._save_pending_confirmation(sender_id, pending)

            if self._service_required_for_booking() and not pending.get("current_service_id"):
                if contact_details["customer_name"]:
                    pending["customer_name"] = contact_details["customer_name"]
                if contact_details["email"]:
                    pending["contact_email"] = contact_details["email"]
                if contact_details["phone"]:
                    pending["contact_phone"] = contact_details["phone"]
                preserved_start_dt = None
                if requested_time is not None and pending_requested_date is not None:
                    preserved_start_dt = self._combine_requested_date_and_time(
                        pending_requested_date,
                        requested_time,
                    )
                    pending["start_dt"] = self._serialize_pending_start_dt(preserved_start_dt)
                    pending["requested_date"] = pending_requested_date.isoformat()
                elif pending.get("start_dt"):
                    try:
                        preserved_start_dt = self._deserialize_pending_start_dt(pending["start_dt"])
                    except Exception:
                        preserved_start_dt = None
                pending["missing_service"] = True
                self._save_pending_confirmation(sender_id, pending)
                return {
                    "status": "waiting_for_service",
                    "reply_text": self._build_missing_service_reply(language),
                    "event_created": False,
                    "requires_confirmation": False,
                    "booking_state": BookingState.WAITING_FOR_TIME.value,
                    "start_dt": preserved_start_dt.isoformat() if preserved_start_dt else None,
                    "requested_date": (
                        pending_requested_date.isoformat()
                        if pending_requested_date is not None
                        else pending.get("requested_date")
                    ),
                }

            if time_window is None and partial_date and pending.get("time_window"):
                time_window = self._deserialize_pending_time_window(pending)

            if time_window is not None and pending_requested_date is not None:
                return self._suggest_time_window_slots(
                    sender_id,
                    language=language,
                    message_text=message_text,
                    source_channel=source_channel or pending.get("source_channel"),
                    requested_date=pending_requested_date,
                    requested_day_label=pending.get("requested_day_label"),
                    time_window=time_window,
                    current_service_id=pending.get("current_service_id"),
                    current_service_name=pending.get("current_service_name"),
                )

            if daypart is not None and pending_requested_date is not None:
                return self._suggest_daypart_slots(
                    sender_id,
                    language=language,
                    message_text=message_text,
                    source_channel=source_channel or pending.get("source_channel"),
                    requested_date=pending_requested_date,
                    requested_day_label=pending.get("requested_day_label"),
                    daypart=daypart,
                    current_service_id=pending.get("current_service_id"),
                    current_service_name=pending.get("current_service_name"),
                )

            if requested_time is not None and pending_requested_date is not None:
                requested_dt = self._combine_requested_date_and_time(
                    pending_requested_date,
                    requested_time,
                )
                return self.start_booking_flow(
                    sender_id=sender_id,
                    message_text=message_text,
                    source_channel=source_channel or pending.get("source_channel"),
                    requested_dt_override=requested_dt,
                    current_service_id=pending.get("current_service_id"),
                    current_service_name=pending.get("current_service_name"),
                )

            if partial_date:
                return {
                    "status": "waiting_for_time",
                    "reply_text": self._build_missing_time_reply(
                        language,
                        pending.get("requested_day_label"),
                    ),
                    "event_created": False,
                    "requires_confirmation": False,
                    "booking_state": BookingState.WAITING_FOR_TIME.value,
                    "requested_date": pending.get("requested_date"),
                }

            if pending_requested_date is not None:
                return {
                    "status": "waiting_for_time",
                    "reply_text": self._build_missing_time_reply(
                        language,
                        pending.get("requested_day_label"),
                    ),
                    "event_created": False,
                    "requires_confirmation": False,
                    "booking_state": BookingState.WAITING_FOR_TIME.value,
                    "requested_date": pending.get("requested_date"),
                }

            if self._looks_like_unrelated_question_during_booking(message_text):
                return {
                    "status": "booking_unrelated_question",
                    "reply_text": self._build_unrelated_during_booking_reply(language, state),
                    "event_created": False,
                    "booking_state": BookingState.WAITING_FOR_TIME.value,
                }

            return self.start_booking_flow(
                sender_id=sender_id,
                message_text=message_text,
                source_channel=source_channel or pending.get("source_channel"),
                current_service_id=pending.get("current_service_id"),
                current_service_name=pending.get("current_service_name"),
            )

        if state == BookingState.WAITING_FOR_CONTACT:
            accepted_start_dt = (
                self._match_suggested_slot_acceptance(message_text, pending)
                if pending.get("availability_context")
                else None
            )
            if accepted_start_dt is not None:
                pending["customer_name"] = None
                self._save_pending_confirmation(sender_id, pending)
                return {
                    "status": "waiting_for_contact",
                    "reply_text": self._build_suggested_slot_accepted_reply(
                        language,
                        accepted_start_dt,
                    ),
                    "event_created": False,
                    "requires_contact": True,
                    "booking_state": BookingState.WAITING_FOR_CONTACT.value,
                    "start_dt": accepted_start_dt.isoformat(),
                }

            if pending.get("availability_context") and self._looks_like_another_time_request(message_text):
                requested_date = None
                requested_day_label = pending.get("requested_day_label")
                try:
                    accepted_start_dt = self._deserialize_pending_start_dt(pending.get("start_dt"))
                    requested_date = accepted_start_dt.date()
                    requested_day_label = requested_day_label or self._format_date_label_for_reply(
                        requested_date,
                        language,
                    )
                except Exception:
                    logger.warning(
                        "suggested slot date preservation failed sender_id=%s raw_start_dt=%r",
                        sender_id,
                        pending.get("start_dt"),
                    )

                self._save_booking_state(
                    sender_id,
                    state=BookingState.WAITING_FOR_TIME,
                    language=language,
                    source_channel=source_channel or pending.get("source_channel"),
                    context_summary=pending.get("context_summary"),
                    requested_date=requested_date,
                    requested_day_label=requested_day_label,
                )
                return {
                    "status": "waiting_for_time",
                    "reply_text": self._build_another_time_reply(language, requested_day_label),
                    "event_created": False,
                    "requires_confirmation": False,
                    "booking_state": BookingState.WAITING_FOR_TIME.value,
                    "requested_date": requested_date.isoformat() if requested_date else None,
                }

            contact_details = self._extract_waiting_for_contact_details(message_text)

            if (
                not contact_details["has_name"]
                and not contact_details["has_phone"]
                and not contact_details["has_email"]
                and self._looks_like_booking_status_question(message_text)
            ):
                return {
                    "status": "booking_pending_contact_status_question",
                    "reply_text": self._build_booking_status_pending_contact_reply(language),
                    "event_created": False,
                    "requires_contact": True,
                    "booking_state": BookingState.WAITING_FOR_CONTACT.value,
                }

            if (
                not contact_details["has_name"]
                and not contact_details["has_phone"]
                and not contact_details["has_email"]
                and self._looks_like_unrelated_question_during_booking(message_text)
            ):
                return {
                    "status": "booking_unrelated_question",
                    "reply_text": self._build_unrelated_during_booking_reply(language, state),
                    "event_created": False,
                    "requires_contact": True,
                    "booking_state": BookingState.WAITING_FOR_CONTACT.value,
                }

            customer_name = contact_details["customer_name"] or pending.get("customer_name")
            contact_email = contact_details["email"] or pending.get("contact_email")
            contact_phone = contact_details["phone"] or pending.get("contact_phone")

            logger.info(
                "booking contact received sender_id=%s has_name=%s has_email=%s has_phone=%s",
                sender_id,
                bool(customer_name),
                bool(contact_email),
                bool(contact_phone),
            )

            pending["customer_name"] = customer_name
            pending["contact_email"] = contact_email
            pending["contact_phone"] = contact_phone

            if not self._has_required_contact(
                customer_name=customer_name,
                email=contact_email,
                phone=contact_phone,
            ):
                self._save_pending_confirmation(sender_id, pending)
                if not customer_name and not contact_email and not contact_phone:
                    return {
                        "status": "waiting_for_contact",
                        "reply_text": self._build_contact_retry_reply(language),
                        "event_created": False,
                        "requires_contact": True,
                        "booking_state": BookingState.WAITING_FOR_CONTACT.value,
                    }
                if not customer_name and "name" in self._required_contact_fields():
                    return {
                        "status": "waiting_for_name",
                        "reply_text": self._build_name_retry_reply(language),
                        "event_created": False,
                        "requires_contact": True,
                        "booking_state": BookingState.WAITING_FOR_CONTACT.value,
                    }
                return {
                    "status": "waiting_for_contact",
                    "reply_text": self._build_contact_only_retry_reply(language, customer_name),
                    "event_created": False,
                    "requires_contact": True,
                    "booking_state": BookingState.WAITING_FOR_CONTACT.value,
                }

            pending["state"] = BookingState.CONFIRMATION.value

        elif state == BookingState.CONFIRMATION:
            pass
        else:
            return None

        try:
            start_dt = self._deserialize_pending_start_dt(pending["start_dt"])
        except Exception:
            logger.exception(
                "booking pending datetime deserialize failed sender_id=%s raw_start_dt=%r",
                sender_id,
                pending.get("start_dt"),
            )
            self._clear_pending_confirmation(sender_id)
            return {
                "status": "create_failed",
                "reply_text": self._build_create_failed_reply(language),
                "event_created": False,
                "booking_state": BookingState.NONE.value,
            }

        if self._service_required_for_booking() and not pending.get("current_service_id"):
            pending["missing_service"] = True
            self._save_pending_confirmation(sender_id, pending)
            return {
                "status": "waiting_for_service",
                "reply_text": self._build_missing_service_reply(language),
                "event_created": False,
                "requires_contact": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
                "start_dt": start_dt.isoformat(),
            }

        try:
            if not self._is_within_business_hours(start_dt, pending["duration_minutes"]):
                still_available = False
            else:
                still_available = self.calendar_service.check_specific_time_availability(
                    start_dt=start_dt,
                    duration_minutes=pending["duration_minutes"],
                )
        except Exception:
            logger.exception(
                "booking availability recheck failed sender_id=%s start_dt=%s",
                sender_id,
                start_dt.isoformat(),
            )
            self._clear_pending_confirmation(sender_id)
            return {
                "status": "create_failed",
                "reply_text": self._build_create_failed_reply(language, start_dt),
                "event_created": False,
                "booking_state": BookingState.NONE.value,
            }

        if not still_available:
            self._clear_pending_confirmation(sender_id)
            return {
                "status": "unavailable",
                "reply_text": self._build_unavailable_reply(language),
                "event_created": False,
                "booking_state": BookingState.NONE.value,
            }

        calendar_configured = bool(
            self.calendar_service.google_calendar_client
            and self.calendar_service.google_calendar_client.is_configured()
        )
        logger.info("Calendar configured: %s", calendar_configured)

        if not self.calendar_service.google_calendar_client:
            return self._build_manual_followup_result(
                sender_id=sender_id,
                language=language,
                start_dt=start_dt,
                customer_name=pending.get("customer_name"),
                email=pending.get("contact_email"),
                phone=pending.get("contact_phone"),
            )

        if not calendar_configured:
            logger.warning(
                "Google Calendar is not configured; switching to manual follow-up sender_id=%s",
                sender_id,
            )
            return self._build_manual_followup_result(
                sender_id=sender_id,
                language=language,
                start_dt=start_dt,
                customer_name=pending.get("customer_name"),
                email=pending.get("contact_email"),
                phone=pending.get("contact_phone"),
            )

        try:
            description_parts = [pending["description"]]
            if pending.get("customer_name"):
                description_parts.append(f"Customer name: {pending['customer_name']}")
            description_parts.append(f"Sender ID: {sender_id}")
            if pending.get("source_channel"):
                description_parts.append(f"Source: {pending['source_channel']}")
            if pending.get("context_summary"):
                description_parts.append(f"Context: {pending['context_summary']}")

            contact_parts = []
            if pending.get("contact_email"):
                contact_parts.append(f"Email: {pending['contact_email']}")
            if pending.get("contact_phone"):
                contact_parts.append(f"Phone: {pending['contact_phone']}")
            if contact_parts:
                description_parts.append("Contact: " + " | ".join(contact_parts))

            description = "\n".join(description_parts)

            completed_match = self._completed_booking_matches_candidate(
                sender_id,
                start_dt=start_dt,
                email=pending.get("contact_email"),
                phone=pending.get("contact_phone"),
                current_service_id=pending.get("current_service_id"),
            )
            if completed_match is not None:
                logger.info(
                    "Booking semantic retry skipped sender_id=%s start_dt=%s",
                    sender_id,
                    start_dt.isoformat(),
                )
                self._clear_pending_confirmation(sender_id)
                return self._build_idempotent_confirmed_result(
                    language=language,
                    completed=completed_match,
                )

            created = self.calendar_service.create_booking_event(
                start_dt=start_dt,
                duration_minutes=pending["duration_minutes"],
                summary=pending["summary"],
                description=description,
                attendee_emails=[],
            )
            logger.info(
                "Calendar event created sender_id=%s event_id=%s",
                sender_id,
                created.event_id,
            )
        except Exception as exc:
            logger.warning("Booking creation failed error_type=%s", type(exc).__name__)
            logger.warning(
                "booking create_event failed sender_id=%s state=%s has_start_dt=%s has_email=%s has_phone=%s error_type=%s",
                sender_id,
                state.value,
                start_dt is not None,
                bool(pending.get("contact_email")),
                bool(pending.get("contact_phone")),
                type(exc).__name__,
            )
            return self._build_manual_followup_result(
                sender_id=sender_id,
                language=language,
                start_dt=start_dt,
                customer_name=pending.get("customer_name"),
                email=pending.get("contact_email"),
                phone=pending.get("contact_phone"),
            )

        self._save_captured_contact(
            sender_id,
            customer_name=pending.get("customer_name"),
            email=pending.get("contact_email"),
            phone=pending.get("contact_phone"),
            start_dt=start_dt,
        )
        self._mark_booking_completed(
            sender_id,
            start_dt=start_dt,
            customer_name=pending.get("customer_name"),
            email=pending.get("contact_email"),
            phone=pending.get("contact_phone"),
            calendar_event_id=created.event_id,
            current_service_id=pending.get("current_service_id"),
            current_service_name=pending.get("current_service_name"),
        )
        self._clear_pending_confirmation(sender_id)

        has_email = bool(pending.get("contact_email"))
        has_phone = bool(pending.get("contact_phone"))
        customer_name = pending.get("customer_name")

        if has_email and has_phone:
            reply_text = self._build_both_contacts_confirmed_reply(language, start_dt, customer_name)
        elif has_email:
            reply_text = self._build_email_confirmed_reply(language, start_dt, customer_name)
        else:
            reply_text = self._build_confirmed_reply(language, start_dt, customer_name)

        return {
            "status": "confirmed",
            "reply_text": reply_text,
            "event_created": True,
            "booking_state": BookingState.NONE.value,
            "event_id": created.event_id,
            "event_link": created.html_link,
            "customer_name": pending.get("customer_name"),
            "contact_email": pending.get("contact_email"),
            "contact_phone": pending.get("contact_phone"),
        }
