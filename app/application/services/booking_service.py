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
        ]
        return normalized in rejections or any(marker in normalized for marker in rejection_markers)

    def _build_unclear_time_reply(self, language: str) -> str:
        if self.front_desk_config_service is None:
            return (
                "Супер, тоді можемо коротко розібрати ваш кейс зі спеціалістом. "
                "Напишіть, будь ласка, який день і приблизний час вам зручний?"
            )
        return (
            f"Супер, тоді можемо підібрати {self._appointment_label()}. "
            "Напишіть, будь ласка, який день і приблизний час вам зручний?"
        )

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

    def _build_cancelled_reply(self, language: str) -> str:
        return "Добре, не бронюю. Якщо хочете, можете надіслати інший час."

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
        tomorrow_times = self._format_slot_times(slots_by_day.get("tomorrow", []), language)
        day_after_times = self._format_slot_times(slots_by_day.get("day_after_tomorrow", []), language)

        if tomorrow_times and day_after_times:
            return (
                f"Можемо запропонувати кілька варіантів: завтра {tomorrow_times}, "
                f"а також післязавтра {day_after_times}. Який день і час вам найзручніший?"
            )
        if tomorrow_times:
            return f"Можемо запропонувати завтра {tomorrow_times}. Який час вам найзручніший?"
        if day_after_times:
            return f"Можемо запропонувати післязавтра {day_after_times}. Який час вам найзручніший?"
        return f"Можемо підібрати час для {self._appointment_label()}. Підкажіть, будь ласка, який день вам зручний?"

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
            if self.calendar_service.check_specific_time_availability(
                next_dt,
                self._booking_duration_minutes(),
            ):
                return next_dt
        return None

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

    def _is_non_customer_name_text(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        normalized = re.sub(r"[.!?…]+$", "", normalized).strip()
        normalized = re.sub(r"([аеєиіїоуюя])\1+", r"\1", normalized)
        if self._is_confirmation_text(normalized) or self._looks_like_another_time_request(normalized):
            return True
        if self._extract_hour_only(normalized) is not None:
            return True
        non_names = {
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
        }
        return normalized in non_names

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
        accept_markers = [
            "підійде",
            "підходить",
            "підходе",
            "буде гуд",
            "гуд",
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
        slots_by_day = self._get_suggested_slots_by_day()

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
        pending["last_suggested_day"] = "tomorrow"
        self._save_pending_confirmation(sender_id, pending)

        return {
            "status": "availability_suggested",
            "reply_text": self._build_availability_question_reply(language, slots_by_day),
            "booking_state": BookingState.WAITING_FOR_TIME.value,
            "suggested_slots": pending["suggested_slots"],
        }

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
    ) -> None:
        self._save_completed_booking(sender_id, {
            "start_dt": self._serialize_pending_start_dt(start_dt) if start_dt else None,
            "customer_name": customer_name,
            "email": email,
            "phone": phone,
            "calendar_event_id": calendar_event_id,
        })

    def get_reschedule_reply(self, language: str) -> str:
        return f"У вас уже є підтверджений {self._appointment_label()}. Якщо хочете, можу допомогти перенести його на інший час."

    def get_reschedule_prompt_reply(self, language: str) -> str:
        return f"Так, звісно. Підкажіть, будь ласка, на який день і час вам буде зручно перенести {self._appointment_label()}?"

    def get_reschedule_handoff_reply(self, language: str) -> str:
        return f"Добре, передам команді, щоб перенесли {self._appointment_label()} без зайвих дій з вашого боку."

    def handle_reschedule_request(self, sender_id: str, message_text: str) -> Dict[str, Any]:
        language = self._detect_language(message_text)
        requested_dt = self._parse_requested_datetime(message_text)

        if requested_dt is None:
            return {
                "status": "reschedule_prompt",
                "reply_text": self.get_reschedule_prompt_reply(language),
                "booking_state": BookingState.NONE.value,
            }

        completed_booking = self._get_completed_booking(sender_id) or {}
        calendar_event_id = completed_booking.get("calendar_event_id") or completed_booking.get("event_id")
        if not calendar_event_id:
            return {
                "status": "reschedule_handoff",
                "reply_text": self.get_reschedule_handoff_reply(language),
                "booking_state": BookingState.NONE.value,
            }

        try:
            self.calendar_service.reschedule_event(
                event_id=calendar_event_id,
                start_dt=requested_dt,
                duration_minutes=self._booking_duration_minutes(),
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

        formatted = self._format_scheduled_time_for_reply(requested_dt, "uk")
        reply_text = f"Супер, перенесли на {formatted} 🙌 Зв’яжемося з вами у цей час."

        return {
            "status": "rescheduled",
            "reply_text": reply_text,
            "booking_state": BookingState.NONE.value,
            "start_dt": requested_dt.isoformat(),
        }

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
            return candidates

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
            if not checked[day_key]:
                checked.pop(day_key, None)

        return checked or candidates

    def _format_slot_times(self, slots: list[datetime], language: str) -> str:
        times = [slot.strftime("%H:%M") for slot in slots]
        if not times:
            return ""
        if len(times) == 1:
            return f"о {times[0]}"
        return "о " + " або ".join(times)

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
        return slots_by_day or self._get_suggested_slots_by_day()

    def _detect_requested_day_key(self, text: str) -> str | None:
        normalized = text.strip().lower()
        if "післязавтра" in normalized or "day after tomorrow" in normalized:
            return "day_after_tomorrow"
        if "завтра" in normalized or "tomorrow" in normalized:
            return "tomorrow"
        return None

    def _extract_hour_only(self, text: str) -> int | None:
        normalized = text.strip().lower()
        match = re.fullmatch(r"(?:о|на|at)?\s*(\d{1,2})(?::00)?", normalized)
        if not match:
            return None
        hour = int(match.group(1))
        if 0 <= hour <= 23:
            return hour
        return None

    def _build_day_slots_reply(self, language: str, day_key: str, slots: list[datetime]) -> str:
        day_label_uk = "завтра" if day_key == "tomorrow" else "післязавтра"
        times = self._format_slot_times(slots, language)
        if times:
            return f"Добре, {day_label_uk} можемо запропонувати {times}. Який час вам зручніший?"
        return f"Добре, підкажіть, будь ласка, який час {day_label_uk} вам зручний?"

    def _process_availability_followup(
        self,
        sender_id: str,
        message_text: str,
        pending: dict[str, Any],
        source_channel: str | None,
    ) -> Dict[str, Any] | None:
        requested_dt = self._parse_requested_datetime(message_text)
        if requested_dt is not None:
            return self.start_booking_flow(
                sender_id=sender_id,
                message_text=message_text,
                source_channel=source_channel or pending.get("source_channel"),
            )

        language = pending.get("language") or self._detect_language(message_text)
        normalized = message_text.strip().lower()

        if self._is_confirmation_text(normalized):
            preferred_day = pending.get("last_suggested_day") or "tomorrow"
            candidate_slots = self._suggested_slots_from_pending(pending).get(preferred_day, [])
            if candidate_slots:
                matched_slot = candidate_slots[0]
                self._save_booking_state(
                    sender_id,
                    state=BookingState.WAITING_FOR_CONTACT,
                    language=language,
                    start_dt=matched_slot,
                    source_channel=source_channel or pending.get("source_channel"),
                    context_summary=pending.get("context_summary"),
                )
                return {
                    "status": "waiting_for_contact",
                    "reply_text": self._build_suggested_slot_accepted_reply(language, matched_slot),
                    "requires_confirmation": False,
                    "requires_contact": True,
                    "booking_state": BookingState.WAITING_FOR_CONTACT.value,
                    "start_dt": matched_slot.isoformat(),
                }

        slots_by_day = self._suggested_slots_from_pending(pending)
        requested_day_key = self._detect_requested_day_key(message_text)
        if requested_day_key:
            pending["last_suggested_day"] = requested_day_key
            self._save_pending_confirmation(sender_id, pending)
            return {
                "status": "availability_day_selected",
                "reply_text": self._build_day_slots_reply(
                    language,
                    requested_day_key,
                    slots_by_day.get(requested_day_key, []),
                ),
                "booking_state": BookingState.WAITING_FOR_TIME.value,
            }

        requested_hour = self._extract_hour_only(message_text)
        if requested_hour is None:
            return None

        preferred_day = pending.get("last_suggested_day") or "tomorrow"
        candidate_slots = slots_by_day.get(preferred_day, [])
        matched_slot = next((slot for slot in candidate_slots if slot.hour == requested_hour), None)
        if matched_slot is None:
            for day_key, slots in slots_by_day.items():
                matched_slot = next((slot for slot in slots if slot.hour == requested_hour), None)
                if matched_slot is not None:
                    preferred_day = day_key
                    break

        if matched_slot is None:
            return {
                "status": "availability_time_not_offered",
                "reply_text": self._build_day_slots_reply(
                    language,
                    preferred_day,
                    candidate_slots,
                ),
                "booking_state": BookingState.WAITING_FOR_TIME.value,
            }

        day_text_by_language = {
            "uk": {
                "tomorrow": "завтра",
                "day_after_tomorrow": "післязавтра",
            },
            "en": {
                "tomorrow": "tomorrow",
                "day_after_tomorrow": "day after tomorrow",
            },
        }
        day_text = day_text_by_language.get(language, day_text_by_language["en"]).get(
            preferred_day,
            "tomorrow",
        )
        booking_text = f"{day_text} о {matched_slot.hour}" if language == "uk" else f"{day_text} at {matched_slot.hour}"
        return self.start_booking_flow(
            sender_id=sender_id,
            message_text=booking_text,
            source_channel=source_channel or pending.get("source_channel"),
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
            "понеділок": 0,
            "понеділка": 0,
            "вівторок": 1,
            "вівторка": 1,
            "tuesday": 1,
            "середа": 2,
            "середу": 2,
            "wednesday": 2,
            "четвер": 3,
            "четверг": 3,
            "thursday": 3,
            "п'ятниц": 4,
            "п’ятниц": 4,
            "friday": 4,
        }

    def _format_day_label_for_reply(self, value: str | None) -> str | None:
        if not value:
            return None
        labels = {
            "today": "сьогодні",
            "tomorrow": "завтра",
            "day after tomorrow": "післязавтра",
            "monday": "понеділок",
            "tuesday": "вівторок",
            "wednesday": "середу",
            "thursday": "четвер",
            "friday": "п’ятницю",
            "понеділка": "понеділок",
            "вівторка": "вівторок",
            "середа": "середу",
            "четверг": "четвер",
            "п'ятниц": "п’ятницю",
            "п’ятниц": "п’ятницю",
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
        normalized = text.strip().lower()

        if "післязавтра" in normalized or "day after tomorrow" in normalized:
            target = now.date() + timedelta(days=2)
            return {"date": target, "day_label": "післязавтра"}
        if "завтра" in normalized or "tomorrow" in normalized:
            target = now.date() + timedelta(days=1)
            return {"date": target, "day_label": "завтра"}
        if "сьогодні" in normalized or "today" in normalized:
            return {"date": now.date(), "day_label": "сьогодні"}

        for marker, weekday in self._weekday_map().items():
            if marker in normalized:
                days_ahead = (weekday - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                target = now.date() + timedelta(days=days_ahead)
                return {
                    "date": target,
                    "day_label": self._format_day_label_for_reply(marker),
                }

        return None

    def _parse_time_only(self, text: str) -> time | None:
        normalized = text.strip().lower()

        time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", normalized)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return time(hour=hour, minute=minute)

        hour_match = re.search(r"\b(?:о|на|at)\s*(\d{1,2})(?::00)?\b", normalized)
        if not hour_match:
            hour_match = re.fullmatch(r"\s*(\d{1,2})(?::00)?\s*", normalized)
        if hour_match:
            hour = int(hour_match.group(1))
            if 0 <= hour <= 23:
                return time(hour=hour, minute=0)

        return None

    def _looks_like_booking_correction(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        correction_markers = [
            "краще",
            "мав на увазі",
            "мала на увазі",
            "маю на увазі",
            "я мав на увазі",
            "я мала на увазі",
            "не о",
            "не на",
            "а о",
            "а на",
            "rather",
            "instead",
            "i mean",
            "meant",
        ]
        return any(marker in normalized for marker in correction_markers)

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

    def _extract_corrected_time(self, text: str) -> time | None:
        if not self._looks_like_booking_correction(text):
            return None

        normalized = text.strip().lower()
        marker_positions = [
            normalized.rfind(marker)
            for marker in [" а ", "краще", "мав на увазі", "мала на увазі", "маю на увазі", "rather", "instead", "mean", "meant"]
        ]
        last_marker = max(marker_positions)
        if last_marker >= 0:
            parsed = self._parse_time_only(normalized[last_marker:])
            if parsed is not None:
                return parsed

        candidates = self._parse_time_candidates(text)
        return candidates[-1] if candidates else None

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

    def _get_pending_start_dt(self, pending: dict[str, Any]) -> datetime | None:
        raw_start_dt = pending.get("start_dt")
        if not raw_start_dt:
            return None
        return self._deserialize_pending_start_dt(raw_start_dt)

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
        if not self._looks_like_booking_correction(message_text):
            return None

        partial_date = self._extract_requested_date(message_text)
        corrected_time = self._extract_corrected_time(message_text)
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
        normalized = text.strip().lower()

        match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ ,T]+(\d{1,2}):(\d{2})", normalized)
        if match:
            year, month, day, hour, minute = map(int, match.groups())
            return datetime(year, month, day, hour, minute, tzinfo=self.timezone)

        match = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?[ ,]+(\d{1,2}):(\d{2})", normalized)
        if match:
            day = int(match.group(1))
            month = int(match.group(2))
            year = int(match.group(3)) if match.group(3) else now.year
            hour = int(match.group(4))
            minute = int(match.group(5))
            return datetime(year, month, day, hour, minute, tzinfo=self.timezone)

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

        hour_match = re.search(r"\b(\d{1,2})\b", normalized)
        matched_weekday = None
        for marker, weekday in self._weekday_map().items():
            if marker in normalized:
                matched_weekday = weekday
                break

        if hour_match and matched_weekday is not None:
            hour = int(hour_match.group(1))
            if 0 <= hour <= 23:
                days_ahead = (matched_weekday - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                base = now + timedelta(days=days_ahead)
                return base.replace(hour=hour, minute=0, second=0, microsecond=0)

        if hour_match and (
            "завтра" in normalized
            or "tomorrow" in normalized
            or "післязавтра" in normalized
            or "day after tomorrow" in normalized
            or "сьогодні" in normalized
            or "today" in normalized
        ):
            hour = int(hour_match.group(1))
            minute = 0

            if 0 <= hour <= 23:
                if "післязавтра" in normalized or "day after tomorrow" in normalized:
                    base = now + timedelta(days=2)
                    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

                if "завтра" in normalized or "tomorrow" in normalized:
                    base = now + timedelta(days=1)
                    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

                if "сьогодні" in normalized or "today" in normalized:
                    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        return None

    def handle_booking_request(self, sender_id: str, message_text: str) -> Dict[str, Any]:
        return self.start_booking_flow(sender_id=sender_id, message_text=message_text)

    def start_booking_flow(
        self,
        sender_id: str,
        message_text: str,
        source_channel: str | None = None,
        requested_dt_override: datetime | None = None,
    ) -> Dict[str, Any]:
        language = self._detect_language(message_text)
        requested_dt = requested_dt_override or self._parse_requested_datetime(message_text)
        partial_date = self._extract_requested_date(message_text)

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

        if requested_dt is None:
            requested_date = partial_date.get("date") if partial_date else None
            requested_day_label = partial_date.get("day_label") if partial_date else None
            self._save_booking_state(
                sender_id,
                state=BookingState.WAITING_FOR_TIME,
                language=language,
                source_channel=source_channel,
                context_summary=message_text[:280],
                requested_date=requested_date,
                requested_day_label=requested_day_label,
            )
            return {
                "status": "waiting_for_time",
                "reply_text": (
                    self._build_missing_time_reply(language, requested_day_label)
                    if requested_date
                    else self._build_unclear_time_reply(language)
                ),
                "requires_confirmation": False,
                "booking_state": BookingState.WAITING_FOR_TIME.value,
                "start_dt": None,
                "requested_date": requested_date.isoformat() if requested_date else None,
            }

        self._clear_pending_confirmation(sender_id)

        is_available = self.calendar_service.check_specific_time_availability(
            start_dt=requested_dt,
            duration_minutes=self._booking_duration_minutes(),
        )

        logger.info(
            "booking availability sender_id=%s start_dt=%s is_available=%s",
            sender_id,
            requested_dt.isoformat(),
            is_available,
        )

        if not is_available:
            next_slot = self._find_next_available_slot(requested_dt)
            if next_slot:
                self._save_booking_state(
                    sender_id,
                    state=BookingState.WAITING_FOR_CONTACT,
                    language=language,
                    start_dt=next_slot,
                    source_channel=source_channel,
                    context_summary=message_text[:280],
                )

                pending = self._get_pending_confirmation(sender_id) or {}
                pending["availability_context"] = True
                pending["suggested_slots"] = [
                    {
                        "day_key": "tomorrow",
                        "start_dt": self._serialize_pending_start_dt(next_slot),
                    }
                ]
                pending["last_suggested_day"] = "tomorrow"
                self._save_pending_confirmation(sender_id, pending)

                return {
                    "status": "slot_suggested",
                    "reply_text": f"На жаль, {self._format_scheduled_time_for_reply(requested_dt, language)} зайнятий. Як щодо {self._format_scheduled_time_for_reply(next_slot, language)}?",
                    "booking_state": BookingState.WAITING_FOR_CONTACT.value,
                    "requires_contact": True,
                    "suggested_slots": pending["suggested_slots"],
                    "start_dt": next_slot.isoformat(),
                }

            return {
                "status": "unavailable",
                "reply_text": self._build_unavailable_reply(language),
                "requires_confirmation": False,
                "start_dt": requested_dt.isoformat(),
            }

        contact_details = self._extract_contact_details(message_text)

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
                if contact_details["has_phone"] or contact_details["has_email"]
                else None
            ),
            contact_email=contact_details["email"],
            contact_phone=contact_details["phone"],
        )

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
        if self._is_rejection(message_text) and not alternative_time_request:
            self._clear_pending_confirmation(sender_id)
            return {
                "status": "cancelled",
                "reply_text": self._build_cancelled_reply(language),
                "event_created": False,
                "booking_state": BookingState.NONE.value,
            }

        if state == BookingState.WAITING_FOR_TIME:
            if pending.get("availability_context"):
                availability_result = self._process_availability_followup(
                    sender_id=sender_id,
                    message_text=message_text,
                    pending=pending,
                    source_channel=source_channel,
                )
                if availability_result is not None:
                    return availability_result

            partial_date = self._extract_requested_date(message_text)
            requested_time = self._parse_time_only(message_text)
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

            if partial_date:
                pending_requested_date = partial_date["date"]
                pending["requested_date"] = pending_requested_date.isoformat()
                pending["requested_day_label"] = partial_date.get("day_label")
                self._save_pending_confirmation(sender_id, pending)

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

            contact_details = self._extract_contact_details(message_text)

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

        try:
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
