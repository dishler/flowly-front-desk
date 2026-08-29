from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.application.dto.normalized_message import NormalizedMessage
from app.application.services.ai_service import AIService
from app.application.services.knowledge_service import KnowledgeService
from app.application.services.memory_service import MemoryService
from app.domain.enums import IntentType

logger = logging.getLogger(__name__)


class ReplyService:
    def __init__(
        self,
        ai_service: AIService,
        memory_service: MemoryService,
        knowledge_service: KnowledgeService,
        front_desk_config_service: Any | None = None,
    ) -> None:
        self.ai_service = ai_service
        self.memory_service = memory_service
        self.knowledge_service = knowledge_service
        self.front_desk_config_service = front_desk_config_service

    def _detect_language(self, text: str) -> str:
        has_cyrillic = bool(re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", text))
        has_latin = bool(re.search(r"[A-Za-z]", text))
        if has_latin and not has_cyrillic:
            return "en"
        if has_cyrillic:
            return "uk"
        return "uk"

    def _contains_russian(self, text: str) -> bool:
        lowered = text.lower()
        if any(ch in lowered for ch in ("ё", "ъ", "ы", "э")):
            return True
        russian_markers = [
            "что",
            "это",
            "работает",
            "стоит",
            "входит",
            "только",
            "можем",
            "давайте",
        ]
        return any(re.search(rf"\b{re.escape(marker)}\b", lowered) for marker in russian_markers)

    def _fallback_for_intent(self, intent: IntentType, language: str) -> str:
        if language == "en":
            if intent == IntentType.PRICE:
                return "Зазвичай старт від 200$, далі залежить від каналів, сценарію й інтеграцій. Можу зорієнтувати точніше, якщо напишете, які канали потрібні й що бот має робити."
            if intent == IntentType.CHANNELS:
                return "Працюємо з Instagram, Facebook, Telegram, WhatsApp і Viber. Найчастіше починаємо з каналу, де вже є найбільше звернень, а далі підключаємо інші."
            if intent == IntentType.INDUSTRIES:
                return self._get_industries_reply(language)
            if intent == IntentType.USE_CASES:
                return self._get_use_cases_reply(language)
            if intent == IntentType.SERVICE_DESCRIPTION:
                return self._get_service_description_fallback_reply(language)
            if intent in {IntentType.BOOKING_REQUEST, IntentType.CONSULTATION_INTEREST}:
                return "Так, можемо коротко обговорити. На дзвінку швидко розберемо процес і підкажемо, що варто автоматизувати першим."
            if intent == IntentType.REJECTION:
                return self._get_rejection_reply(language)
            if intent == IntentType.FRUSTRATED:
                return self._get_frustrated_reply(language)
            if intent == IntentType.AGGRESSIVE_OBJECTION:
                return self._get_aggressive_objection_reply(language)
            return self._get_unknown_fallback_reply(language)

        if intent == IntentType.PRICE:
            return "Зазвичай старт від 200$, далі залежить від каналів, сценарію й інтеграцій. Можу зорієнтувати точніше, якщо напишете, які канали потрібні й що бот має робити."
        if intent == IntentType.CHANNELS:
            return "Працюємо з Instagram, Facebook, Telegram, WhatsApp і Viber. Найчастіше починаємо з каналу, де вже є найбільше звернень, а далі підключаємо інші."
        if intent == IntentType.INDUSTRIES:
            return self._get_industries_reply(language)
        if intent == IntentType.USE_CASES:
            return self._get_use_cases_reply(language)
        if intent == IntentType.SERVICE_DESCRIPTION:
            return self._get_service_description_fallback_reply(language)
        if intent in {IntentType.BOOKING_REQUEST, IntentType.CONSULTATION_INTEREST}:
            return "Так, можемо коротко обговорити. На дзвінку швидко розберемо процес і підкажемо, що варто автоматизувати першим."
        if intent == IntentType.REJECTION:
            return self._get_rejection_reply(language)
        if intent == IntentType.FRUSTRATED:
            return self._get_frustrated_reply(language)
        if intent == IntentType.AGGRESSIVE_OBJECTION:
            return self._get_aggressive_objection_reply(language)
        return self._get_unknown_fallback_reply(language)

    def get_escalation_reply(self, language: str) -> str:
        if self.front_desk_config_service is not None and not self._is_legacy_flowly_kb():
            return self._get_unknown_fallback_reply(language)
        return (
            "Тут краще коротко уточнити деталі, щоб не відповісти повз ваш запит. "
            "Можете написати, що саме цікавить: як працює бот, для яких бізнесів підходить, ціна чи запис?"
        )

    def _is_complex_query(self, normalized: str) -> bool:
        complex_markers = [
            "crm",
            "api",
            "інтеграц",
            "integration",
            "custom",
            "кастом",
            "філі",
            "branches",
            "branch",
            "enterprise",
            "логік",
            "logic",
            "кілька",
            "multiple",
            "system",
            "workflow",
        ]
        return self._contains_any(normalized, complex_markers)

    def get_contextual_complex_reply(self, user_text: str, language: str) -> str:
        if self.front_desk_config_service is not None and not self._is_legacy_flowly_kb():
            return self._get_unknown_fallback_reply(language)

        normalized = self._normalize(user_text)
        if self._is_client_questions_query(normalized):
            return self._get_client_questions_reply(language)
        if self._is_repair_price_query(normalized):
            return self._get_repair_price_reply(language)
        if self._is_price_objection(normalized):
            return self._get_price_objection_reply(language)
        if self._is_guarantee_query(normalized):
            return self._get_guarantee_reply(language)
        if any(marker in normalized for marker in ["філі", "адміністратор", "адміністратори", "branches"]):
            return (
                "Так, це вирішується логікою маршрутизації: бот може уточнювати філію, "
                "напрям або послугу і передавати заявку потрібному адміністратору. Щоб сказати "
                "точніше, краще коротко розібрати ваш процес і правила розподілу заявок."
            )
        if self._is_complex_query(normalized):
            if language == "en":
                return (
                    "Такі сценарії зазвичай вирішуються через логіку бота, CRM та правила передачі заявок. Щоб відповісти точніше, напишіть, яка система або процес у вас зараз."
                )
            return (
                "Такі сценарії зазвичай вирішуються через логіку бота, CRM та правила передачі заявок. "
                "Щоб відповісти точніше, напишіть, яка система або процес у вас зараз."
            )
        return self.get_escalation_reply(language)

    def get_safe_fallback_reply(self, language: str) -> str:
        return self._get_unknown_fallback_reply(language)

    def get_contextual_fallback_reply(self, user_text: str, history: List[str], language: str) -> str:
        if self.front_desk_config_service is not None and not self._is_legacy_flowly_kb():
            return self._get_unknown_fallback_reply(language)

        _ = user_text
        assistant_history = [
            item.removeprefix("assistant:").strip().lower()
            for item in history
            if item.startswith("assistant:")
        ]
        recent = " ".join(assistant_history[-4:])

        if "instagram" in recent or "інстаграм" in recent:
            return (
                "Можу детальніше пояснити, як це працює саме для Instagram: бот відповідає в DM, "
                "уточнює запит, збирає контакт або бажаний час і передає команді вже готовішу заявку. "
                "Що у вас частіше пишуть в Instagram: ціни, запис чи питання по послугах?"
            )

        if "старт від 200" in recent or "вартість стартує" in recent:
            return (
                "По ціні орієнтир лишається від 200$, але точніше залежить від каналу, сценарію "
                "і того, що саме бот має робити. Напишіть, будь ласка, з якого каналу хочете почати."
            )

        if "ai-бот" in recent or "бот відповідає" in recent:
            return (
                "Якщо коротко по суті: бот бере перші типові повідомлення, уточнює деталі "
                "і передає менеджеру вже теплішу заявку. Найпростіше розібрати на вашому каналі: "
                "де зараз найбільше звернень?"
            )

        return self._get_unknown_fallback_reply(language)

    def get_language_request_reply(self, language: str) -> str:
        return "Можу відповідати українською або англійською."

    def detect_user_language(self, text: str) -> str:
        return self._detect_language(text)

    def get_grounded_front_desk_reply(self, user_text: str, language: Optional[str] = None) -> Optional[str]:
        if self.front_desk_config_service is None or self._is_legacy_flowly_kb():
            return None
        resolved_language = language or self._detect_language(user_text)
        return self._get_business_fact_reply(self._normalize(user_text), resolved_language)

    def get_contextual_front_desk_reply(self, message: NormalizedMessage, intent: Optional[IntentType] = None) -> Optional[str]:
        if self.front_desk_config_service is None or self._is_legacy_flowly_kb():
            return None

        text = message.user_message.strip()
        language = self._detect_language(text)
        normalized = self._normalize(text)
        context = self.memory_service.get_context(message.sender_id) if self.memory_service else {}
        service = (
            self.knowledge_service.find_confident_service(normalized)
            if self.knowledge_service
            else None
        )
        if service is None and self.knowledge_service:
            service = self.knowledge_service.find_bounded_consultation_service(normalized)
        price_requested = (
            self._looks_like_price_query(normalized)
            or intent == IntentType.PRICE
            or (service is not None and self._looks_like_how_much_marker(normalized))
        )
        service_context_id = context.get("current_service_id")

        if self._looks_like_anesthesia_question(normalized) and (
            self._looks_like_price_query(normalized) or self._looks_like_how_much_marker(normalized)
        ):
            # "скільки анестезія?" asks anesthesia's own price, not whether
            # some other procedure uses it -- if the KB has a dedicated
            # anesthesia-topic entry with a real price, answer with that
            # instead of the generic per-procedure deflection below. Found
            # generically (by re-running the same anesthesia-question check
            # against each service's own text), not by a hardcoded id, so
            # any KB that models an anesthesia topic benefits the same way.
            # (Checked directly rather than via price_requested, which
            # requires an already-matched `service` -- circular here, since
            # the anesthesia topic deliberately has no bare alias to match.)
            anesthesia_service = self._find_anesthesia_topic_service()
            if anesthesia_service is not None:
                anesthesia_price_reply = self._format_price_options_reply(anesthesia_service)
                if anesthesia_price_reply is None and anesthesia_service.get("price_note"):
                    anesthesia_price_reply = str(anesthesia_service["price_note"])
                if anesthesia_price_reply is not None:
                    # Deliberately does not call _remember_front_desk_context
                    # here: asking anesthesia's price is a tangent, not a
                    # topic switch -- the procedure actually being discussed
                    # (if any) must stay the remembered context.
                    return anesthesia_price_reply

        if self._looks_like_anesthesia_question(normalized):
            # "це з анестезією?" -- asking whether a *procedure* uses
            # anesthesia -- defers to the dentist, referencing whatever
            # procedure was last discussed.
            target_service = service
            if target_service is None and service_context_id:
                target_service = self.knowledge_service.get_service_by_id(str(service_context_id))
            if target_service is not None:
                self._remember_front_desk_context(
                    message.sender_id,
                    current_service_id=str(target_service.get("id") or ""),
                    question_context="pending_anesthesia",
                )
                return (
                    f"Щодо «{target_service.get('name')}»: у базі немає точного правила про анестезію. "
                    "Це краще підтвердити з адміністратором або лікарем перед візитом."
                )
        if context.get("question_context") == "pending_anesthesia" and service is not None:
            self._remember_front_desk_context(
                message.sender_id,
                current_service_id=str(service.get("id") or ""),
                question_context="pending_anesthesia",
            )
            return (
                f"Щодо «{service.get('name')}»: у базі немає точного правила про анестезію. "
                "Це краще підтвердити з адміністратором або лікарем перед візитом."
            )

        if self._looks_like_directions_question(normalized):
            return self._reply_with_grounded_directions(message.sender_id)

        if service is None and self._looks_like_service_availability_question(normalized):
            return "Не хочу дати вам неточну інформацію. Це краще підтвердити з адміністратором або лікарем. Можу допомогти записатися на консультацію або передати питання."

        if self._looks_like_business_fact_question(normalized):
            fact_reply = self._get_business_fact_reply(normalized, language)
            if fact_reply:
                self._remember_front_desk_context(
                    message.sender_id,
                    question_context="services" if self._looks_like_services_question(normalized) else "faq",
                )
                return fact_reply

        compact = re.sub(r"[?!.,…]+", " ", normalized)
        compact = " ".join(compact.split())
        compact = re.sub(r"^(а\s+по|а|по|і|и)\s+", "", compact).strip()
        compact = re.sub(r"^(?:для|у|в)\s+", "", compact).strip()
        if compact.startswith("доросл"):
            pricing_followup_reply = self._get_contextual_pricing_followup_reply(
                message,
                language,
                normalized,
            )
            if pricing_followup_reply is not None:
                return pricing_followup_reply

        if service_context_id:
            context_service = self.knowledge_service.get_service_by_id(str(service_context_id))
            same_resolved_service = (
                service is not None
                and service.get("id") == context_service.get("id") if context_service else False
            )
            different_weak_detail_match = (
                service is not None
                and context_service is not None
                and service.get("id") != context_service.get("id")
                and bool(self.knowledge_service.unknown_detail_tokens_for_service(normalized, service))
                and not self.knowledge_service._looks_like_contextual_relation_followup(normalized)
                and not self.knowledge_service._is_explicit_service_switch(
                    normalized,
                    context_service,
                    service,
                )
            )
            if different_weak_detail_match:
                unknown_detail_reply = self.get_unknown_service_detail_reply(
                    message.sender_id,
                    normalized,
                    service,
                )
                if unknown_detail_reply is not None:
                    return unknown_detail_reply

            unknown_context_detail = False
            short_variant_question = False
            if context_service is not None:
                if same_resolved_service:
                    unknown_context_detail = self._looks_like_service_availability_question(normalized)
                elif service is None:
                    service_like_unknown = any(
                        marker in normalized
                        for marker in ["зуб", "зуби", "лікування", "процедур", "брекет", "імплант", "коронк"]
                    )
                    relation_followup = self.knowledge_service._looks_like_contextual_relation_followup(normalized)
                    scheduling_followup = any(
                        marker in normalized
                        for marker in ["зранку", "вранці", "ввечері", "увечері", "після", "до "]
                    )
                    short_variant_question = (
                        len(normalized.split()) <= 4
                        and "?" in text
                        and bool(
                            re.search(r"(?<![A-Za-zА-Яа-яІіЇїЄєҐґ])є(?![A-Za-zА-Яа-яІіЇїЄєҐґ])", normalized)
                            or "робите" in normalized
                            or "ставите" in normalized
                        )
                        and bool(
                            self.knowledge_service.unknown_detail_tokens_for_service(
                                normalized,
                                context_service,
                            )
                        )
                    )
                    unknown_context_detail = (
                        (service_like_unknown or short_variant_question)
                        and "?" in text
                        and not relation_followup
                        and not scheduling_followup
                    )

            if unknown_context_detail:
                if service is None and not short_variant_question:
                    if self.memory_service is not None:
                        self.memory_service.update_context(
                            message.sender_id,
                            question_context="unknown_detail_pending",
                            current_service_id=None,
                            current_service_name=None,
                        )
                    return (
                        f"Щодо «{text}»: у базі немає підтвердження саме цієї послуги або деталі. "
                        "Краще уточнити це з адміністратором або лікарем перед записом."
                    )
                else:
                    unknown_detail_reply = self.get_unknown_service_detail_reply(
                        message.sender_id,
                        normalized,
                        context_service,
                    )
                    if unknown_detail_reply is not None:
                        return unknown_detail_reply

        contextual_followup = self._reply_with_contextual_service_followup(
            message.sender_id,
            normalized=normalized,
            language=language,
            remembered_service_id=str(service_context_id) if service_context_id else None,
            resolved_service=service,
        )
        if contextual_followup is not None:
            return contextual_followup

        if service_context_id and self._looks_like_yes_or_no_followup(normalized):
            context_service = self.knowledge_service.get_service_by_id(str(service_context_id))
            if context_service is not None:
                return self._reply_with_service_summary(
                    message.sender_id,
                    context_service,
                    availability_confirmation=True,
                )

        unknown_detail_reply = self.get_unknown_service_detail_reply(
            message.sender_id,
            normalized,
            service,
        )
        if unknown_detail_reply is not None:
            return unknown_detail_reply

        if service is not None and price_requested:
            return self._reply_with_service_price(message.sender_id, service)

        if service is not None and context.get("question_context") == "pricing":
            return self._reply_with_service_price(message.sender_id, service)

        if price_requested and service_context_id:
            context_service = self.knowledge_service.get_service_by_id(str(service_context_id))
            if context_service is not None:
                return self._reply_with_service_price(message.sender_id, context_service)

        if service is not None and self._looks_like_option_list_question(normalized):
            option_reply = self._format_price_options_reply(service)
            if option_reply is not None:
                self._remember_front_desk_context(
                    message.sender_id,
                    current_service_id=str(service.get("id") or ""),
                    question_context="services",
                )
                return option_reply

        if service is not None and context.get("question_context") == "services":
            return self._reply_with_service_summary(
                message.sender_id,
                service,
                availability_confirmation=self._looks_like_known_service_availability_question(normalized),
            )

        if service is not None:
            return self._reply_with_service_summary(
                message.sender_id,
                service,
                availability_confirmation=self._looks_like_known_service_availability_question(normalized),
            )

        if (
            service_context_id
            and (
                context.get("question_context") == "pricing"
                or (
                    context.get("question_context") == "services"
                    and self._looks_like_short_acknowledgement(normalized, strict=True)
                )
            )
            and self._looks_like_short_acknowledgement(normalized)
        ):
            self._remember_front_desk_context(
                message.sender_id,
                current_service_id=str(service_context_id),
                question_context=str(context.get("question_context") or "pricing"),
            )
            return "Добре."

        fact_reply = self._get_business_fact_reply(normalized, language)
        if fact_reply:
            self._remember_front_desk_context(
                message.sender_id,
                question_context="services" if self._looks_like_services_question(normalized) else "faq",
            )
            return fact_reply

        return None

    def _looks_like_short_acknowledgement(self, normalized: str, *, strict: bool = False) -> bool:
        compact = re.sub(r"[.!?…,]+$", "", normalized).strip()
        if strict:
            return compact in {"ясно", "зрозуміло", "зрозумів", "зрозуміла"}
        return compact in {
            "ага",
            "ясно",
            "зрозуміло",
            "зрозумів",
            "зрозуміла",
            "ок",
            "окей",
            "добре",
        }

    def _looks_like_known_service_availability_question(self, normalized: str) -> bool:
        if self._looks_like_price_query(normalized):
            return False
        return any(
            marker in normalized
            for marker in [
                "ви робите",
                "робите",
                "ставите",
                "лікуєте",
                "видаляєте",
                "є у вас",
                "можна зробити",
                "можна поставити",
            ]
        )

    def _looks_like_contextual_pronoun_availability_question(self, normalized: str) -> bool:
        return bool(
            re.search(
                r"\b(?:робите|ставите|лікуєте|видаляєте)\s+(?:це|таке|цю\s+процедуру|цю\s+послугу)\b",
                normalized,
            )
        )

    def _looks_like_yes_or_no_followup(self, normalized: str) -> bool:
        compact = re.sub(r"[.!?…]+$", "", normalized).strip()
        compact = re.sub(r"[,;]+", " ", compact)
        compact = " ".join(compact.split())
        return compact in {
            "так чи ні",
            "так або ні",
            "да чи ні",
            "то так чи ні",
            "таки так чи ні",
        }

    def evaluate_escalation(self, user_text: str, history: List[str]) -> Tuple[bool, str]:
        """
        Escalate only for non-standard / complex cases. Standard FAQ intents are handled
        elsewhere and must not hit this path.
        """
        normalized = self._normalize(user_text)

        integration_markers = [
            "api",
            "webhook",
            "sdk",
            "endpoint",
            "інтеграція",
            "integration",
            "crm",
        ]
        technical_markers = [
            "техніч",
            "technical",
        ]
        legal_markers = [
            "contract",
            "legal",
            "гаранті",
            "догов",
            "sla",
            "угод",
        ]
        enterprise_markers = [
            "кастом",
            "custom",
            "філій",
            "філія",
            "філіями",
            "ip-телефон",
            "ip телефон",
            "внутрішн",
            "внутрішня система",
            "мережа клінік",
            "мережу клінік",
            "мережею клінік",
            "складна логіка",
            "branches",
            "multi-location",
            "franchise",
            "кілька філій",
            "5 філій",
        ]

        if any(marker in normalized for marker in integration_markers):
            return True, "integration_or_technical_stack"
        if any(marker in normalized for marker in technical_markers):
            return True, "technical_question"
        if any(marker in normalized for marker in legal_markers):
            return True, "legal_or_contract"
        if any(marker in normalized for marker in enterprise_markers):
            return True, "enterprise_or_custom_setup"

        question_marks = user_text.count("?")
        very_long = len(user_text) > 420
        multipart_heavy = question_marks >= 2 and len(user_text) > 140

        if very_long:
            return True, "very_long_message"
        if multipart_heavy:
            return True, "multipart_question"

        user_turns = sum(1 for line in history if line.startswith("user:"))
        if user_turns >= 4:
            return True, "many_turns_unresolved"

        return False, ""

    def should_escalate(self, user_text: str, history: List[str]) -> bool:
        should, _ = self.evaluate_escalation(user_text, history)
        return should

    def enforce_response_policy(self, reply_text: str, user_text: str, intent: IntentType) -> str:
        language = self._detect_language(user_text)
        if self._contains_russian(reply_text):
            logger.warning("Russian output detected, applying hard language guard")
            return self._fallback_for_intent(intent, language if language == "en" else "uk")
        return reply_text

    def _normalize(self, text: str) -> str:
        normalized = " ".join(text.lower().strip().split())
        return re.sub(r"([аеєиіїоуюя])\1+", r"\1", normalized)

    def _contains_any(self, text: str, markers: List[str]) -> bool:
        return any(marker in text for marker in markers)

    def _get_faq_answer(self, question_uk: str, language: str) -> Optional[str]:
        if self.knowledge_service is None:
            return None
        faq_items = self.knowledge_service.get_all_faq() or []
        for item in faq_items:
            if item.get("question") == question_uk:
                answer = item.get("answer_uk")
                if answer:
                    return str(answer)
        return None

    def _is_legacy_flowly_kb(self) -> bool:
        if self.knowledge_service is None:
            return False
        company = self.knowledge_service.get_company() or {}
        return str(company.get("name", "")).strip().lower() == "flowly"

    def _get_primary_service(self) -> dict[str, Any]:
        if self.knowledge_service is None:
            return {}
        service = self.knowledge_service.get_service_by_id("ai_dm_bot")
        if service:
            return service
        service = self.knowledge_service.get_service_by_id("ai_messaging_automation")
        if service:
            return service
        return self.knowledge_service.get_primary_service()

    def _get_business_fact_reply(self, normalized: str, language: str) -> Optional[str]:
        if self.knowledge_service is None:
            return None

        if self._looks_like_special_date_hours_question(normalized):
            return "Святковий або спеціальний графік потрібно уточнити в клініки. У базі є лише звичайний тижневий графік."

        faq_answer = self.knowledge_service.find_faq_answer(normalized, language)
        if faq_answer:
            return faq_answer

        business = self.knowledge_service.get_business() or {}
        if any(marker in normalized for marker in ["де ви", "адрес", "локац", "location", "address"]):
            location = business.get("location")
            if location:
                return f"Ми знаходимося: {str(location).strip().rstrip('.!?… ')}."

        if any(marker in normalized for marker in ["графік", "години", "працюєте", "working hours", "hours"]):
            working_hours = business.get("working_hours")
            if isinstance(working_hours, dict) and working_hours:
                parts = [f"{key}: {value}" for key, value in working_hours.items()]
                return "Графік роботи: " + "; ".join(parts) + "."
            if isinstance(working_hours, str) and working_hours.strip():
                return f"Графік роботи: {working_hours.strip()}."

        if any(marker in normalized for marker in ["контакт", "телефон", "номер", "подзвонити", "contact", "phone"]):
            contacts = business.get("contacts")
            if isinstance(contacts, dict) and contacts:
                parts = [str(value) for value in contacts.values() if value]
                if parts:
                    return "Контакти: " + ", ".join(parts) + "."

        return None

    def _looks_like_special_date_hours_question(self, normalized: str) -> bool:
        has_hours_context = any(
            marker in normalized
            for marker in ["працює", "працюєте", "відкрит", "графік", "години", "open", "hours"]
        )
        holiday_markers = [
            "різдво",
            "рiздво",
            "новий рік",
            "новий рiк",
            "свято",
            "святков",
            "вихідн",
            "пасха",
            "великдень",
            "holiday",
            "christmas",
            "new year",
        ]
        return has_hours_context and any(marker in normalized for marker in holiday_markers)

    def _get_pricing_reply(self, language: str) -> str:
        if not self._is_legacy_flowly_kb() and self.knowledge_service is not None:
            pricing = self.knowledge_service.get_pricing() or {}
            pricing_note = pricing.get("pricing_note") or pricing.get("price_note")
            if pricing_note:
                return str(pricing_note)
            service = self._get_primary_service()
            if service.get("price_note"):
                return str(service["price_note"])

        if language == "uk":
            return (
                "Зазвичай старт від 200$, далі залежить від каналів, сценарію й інтеграцій. "
                "Можу зорієнтувати точніше, якщо напишете, які канали потрібні й що бот має робити."
            )

        faq_answer = self._get_faq_answer("Скільки це коштує?", language)
        if faq_answer:
            return faq_answer
        return self._fallback_for_intent(IntentType.PRICE, language)

    def _has_recent_pricing_context(self, sender_id: str) -> bool:
        if self.memory_service is None:
            return False
        recent = " ".join(item.lower() for item in self.memory_service.get_history(sender_id)[-6:])
        markers = [
            "скільки коштує",
            "коштує",
            "ціна",
            "прайс",
            "вартість",
            "грн",
            "price",
            "cost",
        ]
        return any(marker in recent for marker in markers)

    def _get_contextual_pricing_followup_reply(
        self,
        message: NormalizedMessage,
        language: str,
        normalized: str,
    ) -> Optional[str]:
        if self.knowledge_service is None or self._is_legacy_flowly_kb():
            return None
        if not self._has_recent_pricing_context(message.sender_id):
            return None

        compact = re.sub(r"[?!.,…]+", " ", normalized)
        compact = " ".join(compact.split())
        compact = re.sub(r"^(а\s+по|а|по|і|и)\s+", "", compact).strip()
        compact = re.sub(r"^(?:для|у|в)\s+", "", compact).strip()
        if not compact or len(compact.split()) > 4:
            return None

        if re.fullmatch(r"доросл\w*(?:\s+(?:скільки|ціна|вартість|коштує))?", compact):
            # A bare "adult" follow-up is a modifier on whatever pediatric
            # topic is currently active, not a service name of its own --
            # resolving it via the generic matcher below would match
            # unrelated services whose description text happens to mention
            # "дорослим" in passing (e.g. braces).
            context = self.memory_service.get_context(message.sender_id) if self.memory_service else {}
            pediatric_service_id = context.get("current_service_id")
            adult_service = (
                self.knowledge_service.get_adult_counterpart_service(str(pediatric_service_id))
                if pediatric_service_id
                else None
            )
            if adult_service is not None:
                return self._reply_with_service_price(message.sender_id, adult_service)

        service = self.knowledge_service.find_service(compact)
        if service and (service.get("price_note") or service.get("price_options")):
            return self._reply_with_service_price(message.sender_id, service)

        faq_answer = self.knowledge_service.find_faq_answer(
            f"скільки коштує {compact}",
            language,
        )
        if faq_answer:
            return faq_answer

        compact_tokens = set(compact.split())
        for service in self.knowledge_service.get_services():
            service_text = " ".join(
                str(service.get(field) or "")
                for field in ["name", "description", "price_note"]
            ).lower()
            if compact_tokens and all(token in service_text for token in compact_tokens):
                price_note = service.get("price_note")
                if price_note:
                    return str(price_note)

        return None

    def _looks_like_price_query(self, normalized: str) -> bool:
        markers = ["скільки коштує", "коштує", "ціна", "ціну", "ціни", "вартість", "прайс", "price", "cost"]
        return any(marker in normalized for marker in markers)

    def _looks_like_anesthesia_question(self, normalized: str) -> bool:
        return any(marker in normalized for marker in ["анестез", "знебол", "обезбол"])

    def _find_anesthesia_topic_service(self) -> dict[str, Any] | None:
        """Finds whichever KB service is itself about anesthesia, by
        re-using _looks_like_anesthesia_question against each service's own
        name/description/aliases -- no hardcoded service id, so it works for
        any KB that happens to model an anesthesia-like topic."""
        if self.knowledge_service is None:
            return None
        for candidate in self.knowledge_service.get_services():
            combined = " ".join(
                str(part) for part in (
                    candidate.get("name"),
                    candidate.get("description"),
                    *candidate.get("aliases", []),
                )
                if part
            ).lower()
            if self._looks_like_anesthesia_question(combined):
                return candidate
        return None

    def _looks_like_directions_question(self, normalized: str) -> bool:
        markers = [
            "як до вас добрати",
            "як до вас доїх",
            "як до вас діст",
            "як добрати",
            "як доїхати",
            "як дістатися",
            "дорогу до клінік",
            "дорога до клінік",
            "маршрут",
            "route",
            "directions",
        ]
        return any(marker in normalized for marker in markers)

    def _extract_directions_origin(self, text: str) -> str | None:
        normalized = " ".join(text.strip().split())
        patterns = [
            r"\bвід\s+(.+?)(?:[?.!…]+)?$",
            r"\bз\s+(.+?)(?:[?.!…]+)?$",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                origin = match.group(1).strip(" ,;:-")
                if origin:
                    return origin
        return None

    def _looks_like_directions_origin(self, normalized: str, context: dict[str, Any]) -> bool:
        if context.get("question_context") != "directions":
            return False
        if self._looks_like_directions_question(normalized):
            return False
        if (
            self._looks_like_price_query(normalized)
            or self._looks_like_booking_message_like_text(normalized)
            or self.knowledge_service.find_confident_service(normalized) is not None
            or self.knowledge_service.find_bounded_consultation_service(normalized) is not None
        ):
            return False
        return bool(normalized) and len(normalized.split()) <= 5

    def _looks_like_booking_message_like_text(self, normalized: str) -> bool:
        return any(
            marker in normalized
            for marker in [
                "хочу запис",
                "запишіть",
                "записатися",
                "записатись",
                "на чистку",
                "на консультац",
                "брон",
            ]
        )

    def _looks_like_service_availability_question(self, normalized: str) -> bool:
        has_question = any(
            marker in normalized
            for marker in ["чи ", "є у вас", "робите", "вставляєте", "ставите", "можна"]
        )
        has_service_like_object = any(
            marker in normalized
            for marker in ["вінір", "венір", "коронк", "брекет", "елайнер", "послуг", "процедур"]
        )
        return has_question and has_service_like_object

    def _reply_with_directions_origin(self, sender_id: str, origin_text: str) -> str:
        return self._reply_with_grounded_directions(sender_id)

    def _reply_with_grounded_directions(self, sender_id: str) -> str:
        business = self.knowledge_service.get_business() if self.knowledge_service else {}
        raw_location = str(business.get("location") or "").strip()
        location = raw_location.rstrip(".!?… ") or "адресу клініки потрібно уточнити"
        if self.memory_service is not None:
            self.memory_service.update_context(sender_id, question_context=None)
        return (
            f"Адреса клініки: {location}. "
            "Точного маршруту в базі немає, тому для найзручнішої дороги краще звірити маршрут у навігаторі."
        )

    def _looks_like_how_much_marker(self, normalized: str) -> bool:
        markers = ["скільки", "сколько", "how much"]
        return any(marker in normalized for marker in markers)

    def _reply_with_contextual_service_followup(
        self,
        sender_id: str,
        *,
        normalized: str,
        language: str,
        remembered_service_id: str | None,
        resolved_service: dict[str, Any] | None,
    ) -> Optional[str]:
        if self.knowledge_service is None or not remembered_service_id:
            return None

        if self._looks_like_cancellation_or_time_only_followup(normalized):
            return None
        match = self.knowledge_service.find_contextual_service_followup(
            normalized,
            remembered_service_id=remembered_service_id,
            resolved_service=resolved_service,
            language=language,
        )
        if match is None:
            return None

        service = match["service"]
        kind = match["kind"]

        if kind == "faq":
            self._remember_front_desk_context(
                sender_id,
                current_service_id=remembered_service_id,
                question_context="services",
            )
            return str(match["answer"])

        if kind == "price_options":
            reply_text = self._format_price_options_reply(service, price_options=match["price_options"])
            if reply_text is not None:
                self._remember_front_desk_context(
                    sender_id,
                    current_service_id=remembered_service_id,
                    question_context="pricing",
                )
                return reply_text

        if kind == "price":
            return self._reply_with_service_price(sender_id, service)

        if kind == "consultation_summary":
            return self._reply_with_service_summary(sender_id, service)

        if kind == "summary":
            return self._reply_with_service_summary(
                sender_id,
                service,
                availability_confirmation=(
                    self._looks_like_contextual_pronoun_availability_question(normalized)
                    or self._looks_like_known_service_availability_question(normalized)
                ),
            )

        return None

    def _looks_like_cancellation_or_time_only_followup(self, normalized: str) -> bool:
        if any(marker in normalized for marker in ["скасу", "не треба", "не хочу", "передум"]):
            return True
        if re.search(r"\b\d{1,2}(?::\d{2})?\b", normalized) and any(
            marker in normalized for marker in ["зручно", "зручноо", "підход", "норм"]
        ):
            return True
        if re.fullmatch(
            r"(?:а|тоді|краще|давайте|давай|можна)?\s*(?:о|на)?\s*\d{1,2}(?::\d{2})?\??",
            normalized,
        ):
            return True
        return False

    def _looks_like_services_question(self, normalized: str) -> bool:
        markers = ["які послуги", "послуги у вас", "послуги", "services"]
        return any(marker in normalized for marker in markers)

    def _looks_like_option_list_question(self, normalized: str) -> bool:
        return any(marker in normalized for marker in ["які", "яка", "варіанти", "види"]) and any(
            marker in normalized for marker in ["є", "бува", "маєте", "пропонуєте"]
        )

    def _looks_like_business_fact_question(self, normalized: str) -> bool:
        return any(
            marker in normalized
            for marker in [
                "де ви",
                "адрес",
                "локац",
                "графік",
                "години",
                "працюєте",
                "контакт",
                "телефон",
                "номер",
            ]
        )

    def _remember_front_desk_context(
        self,
        sender_id: str,
        *,
        current_service_id: str | None = None,
        question_context: str | None = None,
    ) -> None:
        if self.memory_service is None:
            return
        values: dict[str, Any] = {}
        if current_service_id is not None:
            values["current_service_id"] = current_service_id
        if question_context is not None:
            values["question_context"] = question_context
        if values:
            self.memory_service.update_context(sender_id, **values)

    def _format_price_options_reply(
        self,
        service: dict[str, Any],
        *,
        price_options: list[dict[str, Any]] | None = None,
    ) -> Optional[str]:
        price_options = price_options if price_options is not None else service.get("price_options")
        if not isinstance(price_options, list) or not price_options:
            return None
        variants = [
            f"{option['label']} — {option['price_note']}"
            for option in price_options
            if isinstance(option, dict) and option.get("label") and option.get("price_note")
        ]
        if not variants:
            return None
        name = service.get("name")
        prefix = service.get("price_options_reply_prefix")
        if not isinstance(prefix, str):
            prefix = f"{name}: " if name else ""
        return prefix + "; ".join(variants) + "."

    def _reply_with_service_price(self, sender_id: str, service: dict[str, Any]) -> Optional[str]:
        # price_options (multiple priced variants of one topic, e.g. crown
        # materials) takes priority when present; absent/empty/malformed
        # falls straight through to the existing single price_note reply,
        # so a service without price_options behaves exactly as before.
        reply_text = self._format_price_options_reply(service)
        if reply_text is None:
            price_note = service.get("price_note")
            if not price_note:
                return None
            reply_text = str(price_note)

        service_id = service.get("id")
        self._remember_front_desk_context(
            sender_id,
            current_service_id=str(service_id) if service_id else None,
            question_context="pricing",
        )
        return reply_text

    def _reply_with_service_summary(
        self,
        sender_id: str,
        service: dict[str, Any],
        *,
        availability_confirmation: bool = False,
    ) -> Optional[str]:
        summary_reply = service.get("summary_reply")
        if isinstance(summary_reply, str) and summary_reply.strip():
            service_id = service.get("id")
            self._remember_front_desk_context(
                sender_id,
                current_service_id=str(service_id) if service_id else None,
                question_context="services",
            )
            if not availability_confirmation:
                return summary_reply.strip()

        description = service.get("description")
        price_note = service.get("price_note")
        name = service.get("name")
        parts = [str(part) for part in [name, description, price_note] if part]
        if not parts:
            return None
        service_id = service.get("id")
        self._remember_front_desk_context(
            sender_id,
            current_service_id=str(service_id) if service_id else None,
            question_context="services",
        )
        reply_text = " ".join(parts)
        if availability_confirmation:
            availability_reply = service.get("availability_reply")
            if availability_reply:
                return str(availability_reply)
            if description:
                return f"Так, така послуга є в Smile Dental Clinic. {description}"
            if name:
                return f"Так, {str(name).lower()} є в Smile Dental Clinic."
        return reply_text

    def get_unknown_service_detail_reply(
        self,
        sender_id: str,
        normalized: str,
        service: dict[str, Any] | None,
    ) -> Optional[str]:
        if service is None or self.knowledge_service is None:
            return None
        unknown_tokens = self.knowledge_service.unknown_detail_tokens_for_service(
            normalized,
            service,
        )
        if not unknown_tokens:
            return None
        service_id = service.get("id")
        self._remember_front_desk_context(
            sender_id,
            current_service_id=str(service_id) if service_id else None,
            question_context="unknown_detail_pending",
        )
        service_name = service.get("name") or "цієї послуги"
        return (
            f"Щодо «{service_name}»: у базі немає підтвердження саме цієї деталі або варіанту. "
            "Краще уточнити це з адміністратором або лікарем перед записом."
        )

    def _looks_like_unknown_detail_followup_ack(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        normalized = re.sub(r"[.!?…]+$", "", normalized).strip()
        normalized = re.sub(r"[,;]+", " ", normalized)
        words = " ".join(normalized.split()).split()
        if not words or len(words) > 3:
            return False
        ack_words = {
            "так",
            "добре",
            "гаразд",
            "ок",
            "окей",
            "давайте",
            "давай",
            "звісно",
            "уточніть",
            "уточни",
            "уточнити",
        }
        return all(word in ack_words for word in words)

    def get_unknown_detail_followup_reply(self, sender_id: str, text: str) -> Optional[str]:
        """A short acknowledgement ("гаразд, уточніть", "добре") right after
        the bot's own unknown-service-detail containment reply refers to
        that same unresolved subject -- it must not be answered with a
        generic "what would you like to clarify?" as if the prior turn
        never happened, and must not claim a handoff (e.g. "passed to the
        administrator") that this application never actually performs.
        Scoped strictly to the `unknown_detail_pending` marker the
        containment reply sets, so an ordinary "так" elsewhere in the
        conversation is completely unaffected.
        """
        if self.memory_service is None or self.knowledge_service is None:
            return None
        context = self.memory_service.get_context(sender_id)
        if context.get("question_context") != "unknown_detail_pending":
            return None
        if not self._looks_like_unknown_detail_followup_ack(text):
            return None

        service_id = context.get("current_service_id")
        service = self.knowledge_service.get_service_by_id(str(service_id)) if service_id else None
        subject = service.get("name") if service else "цієї деталі"
        self._remember_front_desk_context(sender_id, question_context="services")
        return (
            f"Щодо «{subject}» потрібне уточнення адміністратора або лікаря. "
            "Можу допомогти записатися на консультацію."
        )

    def _get_channel_reply(self, text: str, language: str) -> Optional[str]:
        _ = text
        if self._is_legacy_flowly_kb():
            return self._fallback_for_intent(IntentType.CHANNELS, language)
        faq_answer = self._get_faq_answer("З якими каналами ви працюєте?", language)
        if faq_answer:
            return faq_answer
        return self._fallback_for_intent(IntentType.CHANNELS, language)

    def _get_consultation_reply(self, language: str) -> str:
        return self._fallback_for_intent(IntentType.BOOKING_REQUEST, language)

    def _get_unknown_fallback_reply(self, language: str) -> str:
        if self.front_desk_config_service is not None and not self._is_legacy_flowly_kb():
            safety = self.front_desk_config_service.get_safety()
            fallback = safety.get("unknown_fallback")
            if fallback:
                return str(fallback)
        return (
            "Хочу правильно зрозуміти ваш запит. Ви маєте на увазі, як бот може "
            "працювати саме у вашому бізнесі, чи більше цікавить ціна, канали або запис на дзвінок?"
        )

    def _get_rejection_reply(self, language: str) -> str:
        if self.front_desk_config_service is not None and not self._is_legacy_flowly_kb():
            return "Зрозуміло. Якщо буде потрібно, напишіть нам."
        return (
            "Зрозумів, дякую. Якщо пізніше буде актуально автоматизувати відповіді "
            "в месенджерах — можете просто написати сюди."
        )

    def _get_repeated_rejection_reply(self, language: str) -> str:
        return "Добре, зрозумів."

    def get_rejection_reply(self, language: str, *, repeated: bool = False) -> str:
        if repeated:
            return self._get_repeated_rejection_reply(language)
        return self._get_rejection_reply(language)

    def _get_frustrated_reply(self, language: str) -> str:
        if self.front_desk_config_service is not None and not self._is_legacy_flowly_kb():
            return self._get_unknown_fallback_reply(language)
        return (
            "Розумію, відповідь була не зовсім по суті. Можу коротко пояснити конкретно: "
            "що робить бот, для яких бізнесів підходить або скільки коштує."
        )

    def _get_aggressive_objection_reply(self, language: str) -> str:
        return (
            "Розумію скепсис. Бот має сенс не просто як “чатик”, а коли є повторювані "
            "звернення, заявки губляться або менеджери довго відповідають. Якщо у вас цього "
            "немає — можливо, автоматизація справді не потрібна."
        )

    def _get_industries_reply(self, language: str) -> str:
        if self.front_desk_config_service is not None and not self._is_legacy_flowly_kb():
            return self._get_unknown_fallback_reply(language)
        return (
            "Найкраще бот підходить для сервісних бізнесів, де є багато вхідних повідомлень, "
            "записів або заявок.\n\n"
            "Наприклад:\n"
            "— стоматології та клініки\n"
            "— СТО / автосервіси\n"
            "— салони краси, барбершопи, косметології\n"
            "— освітні курси та школи\n"
            "— фітнес / спорт студії\n"
            "— консультаційні та локальні сервіси\n\n"
            "Якщо коротко: там, де клієнти часто пишуть у месенджери і потрібно швидко "
            "відповідати, кваліфікувати заявку або доводити до запису."
        )

    def get_after_hours_reply(self, language: str) -> str:
        return (
            "Якщо клієнт пише вночі, бот може одразу відповісти, прийняти заявку, "
            "уточнити базові деталі й зібрати контакт. А вже зранку менеджер бачить "
            "готове звернення і може швидко продовжити діалог. Тут уже краще коротко "
            "розібрати ваш кейс зі спеціалістом, щоб не відповідати загально."
        )

    def _get_interest_signal_reply(self, language: str) -> str:
        if self.front_desk_config_service is not None and not self._is_legacy_flowly_kb():
            return self._get_unknown_fallback_reply(language)
        if language == "uk":
            return (
                "Супер, тоді це схоже на нормальний кейс для запуску. "
                "Можемо швидко пройтись по вашому процесу зі спеціалістом і підказати, "
                "що варто автоматизувати першим. Зручно коротко зідзвонитись?"
            )
        return (
            "Супер, тоді це схоже на нормальний кейс для запуску. "
            "Можемо швидко пройтись по вашому процесу зі спеціалістом і підказати, "
            "що варто автоматизувати першим. Зручно коротко зідзвонитись?"
        )

    def _get_use_cases_reply(self, language: str) -> str:
        if self.front_desk_config_service is not None and not self._is_legacy_flowly_kb():
            return self._get_unknown_fallback_reply(language)
        return (
            "Можемо показати типові use cases для різних бізнесів.\n\n"
            "Наприклад:\n"
            "— стоматологічна клініка: бот відповідає на питання про послуги, ціни, "
            "графік і допомагає записати клієнта на консультацію\n"
            "— СТО / автосервіс: бот приймає звернення, уточнює проблему з авто і "
            "передає заявку менеджеру\n"
            "— салон краси: бот відповідає на типові питання і допомагає записати "
            "клієнта до майстра\n"
            "— освітній проєкт: бот кваліфікує заявку перед консультацією або продажем курсу\n\n"
            "Але краще розібрати саме ваш процес на короткому дзвінку — тоді покажемо, "
            "як це може працювати у вашому бізнесі."
        )

    def _get_ai_fallback_reply(
        self,
        text: str,
        language: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        if self.ai_service is None:
            return None

        if self._is_legacy_flowly_kb():
            system_instruction = (
                "Ти менеджер Flowly Agency.\n"
                "Ти допомагаєш клієнтам із сервісних бізнесів у DM.\n"
                "Відповідай коротко, природно, як уважний sales-менеджер, а не як скрипт.\n"
                "Будь гнучким, емоційно інтелектуальним і контекстуально адаптивним.\n"
                "Якщо запит нечіткий, із помилками або може мати кілька значень — коротко "
                "перефразуй, як ти його зрозумів, і постав одне уточнююче питання.\n"
                "Не вигадуй можливості.\n"
                "Не відповідай російською.\n"
                "Не продавай дзвінок на кожне повідомлення.\n"
                "Не запускай запис на дзвінок автоматично лише через слова “запис”, “записує”, "
                "“календар”, “інтеграція”, “CRM”, “майстер”, “прийом”. Спочатку зрозумій: це питання про функцію "
                "чи реальний намір отримати консультацію.\n"
                "Якщо користувач просить без дзвінка або пояснити в чаті — допоможи в чаті й не тисни.\n"
                "Якщо питання складне — коротко поясни, що потрібно уточнити, і м’яко запропонуй "
                "розібрати кейс зі спеціалістом."
            )
        else:
            business = self.knowledge_service.get_business() if self.knowledge_service else {}
            business_name = str(business.get("name") or "the business")
            system_instruction = (
                f"Ти front desk асистент для {business_name}.\n"
                "Відповідай коротко, спокійно і тільки на основі knowledge context.\n"
                "Не вигадуй послуги, ціни, адресу, графік, політики або доступність календаря.\n"
                "Не став медичні діагнози і не призначай лікування в чаті.\n"
                "Якщо питання потребує лікаря або адміністратора, безпечно передай людині або запропонуй запис на візит."
            )

        try:
            ai_result = self.ai_service.try_generate_reply(
                user_message=text,
                history=history or [],
                grounding_context=None,
                system_instruction=system_instruction,
            )
        except TypeError:
            ai_result = self.ai_service.try_generate_reply(
                user_message=text,
                history=history or [],
            )
        except Exception:
            logger.exception("AI fallback failed")
            return None

        ai_reply_text = ai_result.get("reply_text") if isinstance(ai_result, dict) else None
        if ai_reply_text:
            return str(ai_reply_text)
        return None

    def _is_price_query(self, normalized: str) -> bool:
        price_markers = [
            "price",
            "pricing",
            "cost",
            "how much",
            "how much does it cost",
            "what does it cost",
            "що по ціні",
            "ціна",
            "ціні",
            "скільки",
            "вартість",
            "бюджет",
            "скільки коштує",
        ]
        return self._contains_any(normalized, price_markers)

    def _is_service_query(self, normalized: str) -> bool:
        service_markers = [
            "що це",
            "чим займаєтесь",
            "чим ви займаєтесь",
            "що ви робите",
            "що робите",
            "що ви пропонуєте",
            "що пропонуєте",
            "що можете запропонувати",
            "яка у вас пропозиція",
            "як це працює",
            "що входить",
            "що входить у сервіс",
            "що входить в сервіс",
            "що за сервіс",
            "розкажіть про сервіс",
            "розкажіть детальніше",
            "пояснити ваш сервіс",
            "поясніть ваш сервіс",
            "поясніть сервіс",
            "можете коротко пояснити",
            "хочу зрозуміти що за бот",
            "що за бот",
            "для кого це",
            "кому це підходить",
            "як ви працюєте",
            "що саме ви робите",
            "what is this",
            "what do you do",
            "how does it work",
            "what's included",
            "what is included",
            "what does it include",
            "tell me about the service",
            "tell me more about the service",
            "who is it for",
            "how do you work",
            "what exactly do you do",
        ]
        return self._contains_any(normalized, service_markers)

    def _is_greeting(self, normalized: str) -> bool:
        greeting_markers = [
            "прив",
            "привіт",
            "доброго",
            "доброго дня",
            "добрий день",
            "добрий вечір",
            "вітаю",
            "ітаю",
            "хай",
            "hello",
            "hi",
            "hey",
            "good morning",
            "good afternoon",
            "good evening",
        ]
        return self._contains_any(normalized, greeting_markers)

    def _is_mid_level_query(self, normalized: str) -> bool:
        mid_level_markers = [
            "як це працює",
            "як працює",
            "як швидко запуск",
            "як швидко можна запустити",
            "чи підійде",
            "чи підходить",
            "для клініки",
            "для стоматології",
            "для салону",
            "для бізнесу",
            "for clinic",
            "for dental clinic",
            "is it suitable",
            "how does it work",
            "how fast",
            "how quickly can you launch",
            "how long does launch take",
        ]
        return self._contains_any(normalized, mid_level_markers)

    def _is_implementation_time_query(self, normalized: str) -> bool:
        implementation_markers = [
            "скільки часу займає впровадження",
            "скільки часу займає запуск",
            "скільки часу впровадження",
            "скільки триває запуск",
            "скільки триває впровадження",
            "час впровадження",
            "термін впровадження",
            "терміни впровадження",
            "як довго займає впровадження",
            "як довго впровадження",
            "впроваджен",
            "як швидко запуск",
            "як швидко можна запустити",
            "коли запуск",
            "how long does launch take",
            "how quickly can you launch",
            "implementation timeline",
            "launch timeline",
        ]
        return self._contains_any(normalized, implementation_markers)

    def _is_guarantee_query(self, normalized: str) -> bool:
        guarantee_markers = [
            "гарантія",
            "гарантії",
            "гарантуєте",
            "які гарантії",
            "а гарантії є",
            "договір",
            "контракт",
            "legal",
            "contract",
            "guarantee",
            "warranty",
        ]
        return self._contains_any(normalized, guarantee_markers)

    def _get_guarantee_reply(self, language: str) -> str:
        return (
            "Гарантовані цифри без аналізу кейсу не обіцяємо. Можемо чесно оцінити ваш процес, "
            "показати, де бот може зняти ручну роботу і що реально варто автоматизувати першим."
        )

    def _is_client_questions_query(self, normalized: str) -> bool:
        markers = [
            "що бот буде питати",
            "що буде питати",
            "які питання ставить",
            "що питає в клієнта",
            "що бот питає",
        ]
        return self._contains_any(normalized, markers)

    def _get_client_questions_reply(self, language: str) -> str:
        return (
            "Залежить від бізнесу, але зазвичай бот може уточнити ім’я, телефон або email, "
            "послугу чи запит, бажаний день і час, а також важливі деталі. Наприклад для СТО "
            "це може бути марка авто, проблема і зручний час для візиту."
        )

    def _is_repair_price_query(self, normalized: str) -> bool:
        markers = [
            "ціни на ремонт",
            "рахувати ремонт",
            "вартість ремонту",
            "рахувати ціну ремонту",
            "рахувати ціни ремонту",
            "порахувати ремонт",
            "оцінити ремонт",
        ]
        return self._contains_any(normalized, markers)

    def _get_repair_price_reply(self, language: str) -> str:
        return (
            "Точну вартість ремонту бот не має вигадувати. Він може дати орієнтир за вашими "
            "правилами або зібрати деталі проблеми й передати заявку менеджеру для точної оцінки."
        )

    def _is_price_objection(self, normalized: str) -> bool:
        markers = [
            "це дорого",
            "дорого",
            "дорогувато",
            "задорого",
            "expensive",
            "too expensive",
        ]
        return self._contains_any(normalized, markers)

    def _get_price_objection_reply(self, language: str) -> str:
        return (
            "Розумію, не завжди є сенс одразу робити велику систему. Можна почати з "
            "мінімального сценарію: відповіді на часті питання, збір заявки і передача "
            "менеджеру. Якщо коротко опишете, де зараз губляться клієнти, підкажу, чи "
            "є сенс стартувати з базового варіанту."
        )

    def _looks_like_question(self, normalized: str, original_text: str) -> bool:
        if "?" in original_text:
            return True

        question_markers = [
            "що",
            "як",
            "чи",
            "скільки",
            "коли",
            "why",
            "what",
            "how",
            "when",
            "can",
            "could",
            "would",
            "do you",
            "does it",
            "is it",
            "are you",
        ]
        return self._contains_any(normalized, question_markers)

    def classify_question_level(
        self,
        user_text: str,
        intent: IntentType,
        history: List[str],
    ) -> Tuple[str, str]:
        normalized = self._normalize(user_text)

        basic_intents = {
            IntentType.PRICE,
            IntentType.CHANNELS,
            IntentType.SERVICE_DESCRIPTION,
            IntentType.INDUSTRIES,
            IntentType.USE_CASES,
            IntentType.REJECTION,
            IntentType.FRUSTRATED,
        }
        if intent in basic_intents:
            return "basic", intent.value
        if intent == IntentType.INTEREST_SIGNAL:
            return "mid", intent.value
        if self._is_greeting(normalized):
            return "basic", "greeting"
        if intent in {IntentType.BOOKING_REQUEST, IntentType.CONSULTATION_INTEREST}:
            return "mid", intent.value
        if self._is_implementation_time_query(normalized):
            return "mid", "implementation_time_question"
        if self._is_guarantee_query(normalized):
            return "complex", "guarantee_or_legal_question"
        if self._is_price_objection(normalized):
            return "mid", "price_objection"
        if self._is_complex_query(normalized):
            return "complex", "complex_keywords_detected"

        should_escalate, reason = self.evaluate_escalation(user_text, history)
        if should_escalate:
            return "complex", reason

        if self._is_service_query(normalized) or self._is_mid_level_query(normalized):
            return "mid", "known_product_question"

        if not self._looks_like_question(normalized, user_text):
            return "unclear", "not_clearly_a_question"

        return "mid", "general_non_complex"

    def _get_greeting_reply(self, language: str) -> str:
        if not self._is_legacy_flowly_kb() and self.knowledge_service is not None:
            config_business = (
                self.front_desk_config_service.get_business()
                if self.front_desk_config_service is not None
                else {}
            )
            business = self.knowledge_service.get_business() or {}
            name = config_business.get("name") or business.get("name")
            if name:
                if language == "en":
                    return f"Hello! {name}. How can we help?"
                return f"Вітаю! {name}. Чим можемо допомогти?"

        if language == "en":
            return (
                "Hello! We set up AI bots for Instagram, Facebook, Telegram, WhatsApp, and Viber "
                "so businesses can reply to clients 24/7 and guide them toward booking. "
                "If you want, I can briefly explain how this could work in your case."
            )
        return (
            "Привіт! Ми налаштовуємо AI-ботів для Instagram, Facebook, Telegram, WhatsApp і Viber, "
            "щоб бізнес відповідав клієнтам 24/7 і доводив їх до запису. Хочете, коротко "
            "підкажу, як це може працювати у вашому випадку?"
        )

    def _is_consultation_query(self, normalized: str) -> bool:
        consultation_markers = [
            "call",
            "consultation",
            "book",
            "booking",
            "schedule",
            "meeting",
            "дзвінок",
            "консультац",
            "зідзвон",
            "созвон",
            "зустріч",
            "забронювати",
            "запис",
        ]
        return self._contains_any(normalized, consultation_markers)

    def _build_service_grounding_context(self, language: str) -> Dict[str, Any]:
        company = self.knowledge_service.get_company() or {}
        business = self.knowledge_service.get_business() or {}
        service = self._get_primary_service()
        service = service or {}
        pricing = self.knowledge_service.get_pricing() or {}
        consultation = self.knowledge_service.get_consultation() or {}
        constraints = self.knowledge_service.get_constraints() or {}

        return {
            "language": language,
            "company": {
                "name": business.get("name") or company.get("name"),
                "short_description": business.get("description") or company.get("short_description"),
                "location": business.get("location"),
                "working_hours": business.get("working_hours"),
                "contacts": business.get("contacts"),
                "tone": company.get("tone"),
                "languages": company.get("languages", []),
            },
            "service": {
                "id": service.get("id"),
                "name": service.get("name"),
                "short_description": service.get("short_description"),
                "for_whom": service.get("for_whom", []),
                "solves": service.get("solves", []),
                "includes": service.get("includes", []),
                "does_not_include": service.get("does_not_include", []),
                "typical_result": service.get("typical_result", []),
            },
            "pricing": {
                "starting_from_usd": pricing.get("starting_from_usd"),
                "pricing_note": pricing.get("pricing_note"),
                "what_affects_price": pricing.get("what_affects_price", []),
                "how_to_answer_price_questions": pricing.get("how_to_answer_price_questions"),
            },
            "consultation": {
                "duration_minutes": consultation.get("duration_minutes"),
                "goal": consultation.get("goal"),
                "cta_soft": consultation.get("cta_soft"),
            },
            "constraints": constraints,
        }

    def _get_service_system_instruction(self, language: str) -> str:
        if not self._is_legacy_flowly_kb():
            business = self.knowledge_service.get_business() if self.knowledge_service else {}
            business_name = str(business.get("name") or "the business")
            if language == "uk":
                return (
                    f"Ти front desk асистент для {business_name}.\n\n"
                    "Відповідай українською, коротко і природно.\n"
                    "Використовуй лише факти з knowledge context.\n"
                    "Не вигадуй послуги, ціни, адресу, графік, політики або доступність календаря.\n"
                    "Якщо користувач просить діагноз, лікування або точний кошторис складного лікування, не вигадуй і поясни, що потрібен огляд лікаря.\n"
                    "Якщо доречно, запропонуй запис на візит без тиску."
                )
            return (
                f"You are the front desk assistant for {business_name}.\n\n"
                "Reply in English, briefly and naturally.\n"
                "Use only facts from the knowledge context.\n"
                "Do not invent services, prices, address, working hours, policies, or calendar availability.\n"
                "If the user asks for a diagnosis, treatment instructions, or an exact complex treatment estimate, explain that a dentist examination is needed.\n"
                "When appropriate, offer to book a visit without pressure."
            )

        if language == "uk":
            return (
                "Ти AI-асистент компанії Flowly.\n\n"
                "Відповідай ТІЛЬКИ українською мовою, без змішування з іншими мовами.\n\n"
                "Використовуй лише факти з knowledge context, але НЕ копіюй його і НЕ переказуй як список. "
                "Твоя задача — перетворити ці факти у живу, коротку відповідь.\n\n"
                "Роль:\n"
                "ти не просто довідник, а уважний менеджер у DM. Ти слухаєш контекст, "
                "підхоплюєш намір клієнта і допомагаєш зробити наступний крок без тиску.\n\n"
                "Стиль:\n"
                "як реальна переписка в Instagram — просто, природно, без канцеляриту і без "
                "“презентаційного” тону.\n\n"
                "Правила:\n"
                "- максимум 3–4 короткі речення\n"
                "- без списків і довгих переліків\n"
                "- не пояснюй все одразу, відповідай тільки на те, що запитали\n"
                "- не повторюй однакову структуру в кожній відповіді\n\n"
                "Адаптація:\n"
                "- якщо питають “що це” — коротко поясни суть\n"
                "- якщо “як працює” — поясни простими словами процес\n"
                "- якщо “для кого” — скажи кому це реально підходить\n\n"
                "- якщо запит нечіткий або з помилками — перефразуй, як ти зрозумів, і задай одне уточнення\n"
                "- якщо клієнт ще не проявив готовність до дзвінка — спочатку уточни, чи актуальне впровадження і який у нього бізнес\n"
                "- якщо клієнт питає про свій бізнес — адаптуй відповідь під сферу, не давай абстрактний текст\n\n"
                "Діалог:\n"
                "- після корисної відповіді став одне коротке питання, яке допомагає зрозуміти потребу\n"
                "- не кидай у бронювання занадто рано\n"
                "- не запускай запис автоматично лише через слова “запис”, “записує”, “календар”, “інтеграція”, “CRM”, “майстер”, “прийом” — це може бути питання про функцію\n"
                "- якщо користувач просить без дзвінка або пояснити тут, відповідай у чаті й не наполягай\n"
                "- якщо доречно, м’яко запропонуй дзвінок зі спеціалістом, але тільки після корисної відповіді\n\n"
                "Заборонено:\n"
                "- вигадувати\n"
                "- копіювати KB\n"
                "- писати як сайт або презентація\n\n"
                "В кінці (опціонально):\n"
                "додай одну коротку, природну фразу типу:\n"
                "“можемо коротко глянути ваш кейс і підказати, як це буде працювати у вас”"
            )

        return (
            "You are an AI assistant for Flowly.\n\n"
            "Always use one language only, with no language mixing.\n\n"
            "Use only facts from the knowledge context, but do not copy or dump it like a knowledge base. "
            "Turn those facts into a short, natural reply.\n\n"
            "Role:\n"
            "You are not a static FAQ. You listen to context, infer the client's intent carefully, "
            "and guide the conversation one step at a time without pressure.\n\n"
            "Style:\n"
            "conversational Instagram DM tone, simple and human.\n\n"
            "Rules:\n"
            "- maximum 3-4 short sentences\n"
            "- no bullet points or list-style dumping in the actual reply\n"
            "- answer only what the user asked\n"
            "- avoid repeating the same structure every time\n\n"
            "Adaptation:\n"
            "- for what-is questions, explain the core idea briefly\n"
            "- for how-it-works questions, explain the process in simple words\n"
            "- for for-whom questions, explain realistic fit\n\n"
            "- if the request is unclear or typo-heavy, briefly rephrase what you understood and ask one clarifying question\n"
            "- if the client has not shown call readiness yet, ask whether implementation is relevant and what business they have\n"
            "- adapt to the client's business context instead of giving abstract text\n\n"
            "Consultation rules:\n"
            "- do not start booking just because the user mentioned booking, calendar, integration, CRM, specialist, appointment, or reception\n"
            "- first decide whether it is a feature question or real consultation intent\n"
            "- if the user asks to explain here or without a call, answer in chat and do not push\n\n"
            "Optional ending:\n"
            "you may add one short, natural soft CTA."
        )

    def _get_service_fallback_reply(self, language: str) -> str:
        service = self._get_primary_service()
        service = service or {}
        business = self.knowledge_service.get_business() if self.knowledge_service else {}
        short_description = service.get("short_description", "")
        business_description = business.get("description", "")
        includes = service.get("includes", [])
        typical_result = service.get("typical_result", [])
        for_whom = service.get("for_whom", [])

        if not self._is_legacy_flowly_kb():
            parts: List[str] = []
            if business_description:
                parts.append(str(business_description))
            if short_description:
                parts.append(str(short_description))
            elif service.get("description"):
                parts.append(str(service["description"]))
            if includes:
                parts.append("До послуги входить: " + ", ".join(str(item) for item in includes[:4]) + ".")
            if parts:
                return " ".join(parts)
            return self._get_unknown_fallback_reply(language)

        if language == "uk":
            parts: List[str] = []

            if short_description:
                parts.append(short_description)
            else:
                parts.append(
                    "Це AI-асистент для Instagram і Facebook DM, який допомагає автоматизувати "
                    "обробку вхідних звернень і вести клієнта до запису."
                )

            if includes:
                parts.append("Зазвичай у сервіс входить: " + ", ".join(includes[:5]) + ".")

            if for_whom:
                parts.append("Найкраще підходить для: " + ", ".join(for_whom[:3]) + ".")

            if typical_result:
                parts.append("Типовий результат: " + ", ".join(typical_result[:3]) + ".")

            parts.append("Якщо хочете, можемо коротко подивитися ваш кейс на безкоштовній консультації.")

            return " ".join(parts)

        parts = []

        if short_description:
            parts.append(short_description)
        else:
            parts.append(
                "It is an AI assistant for Instagram and Facebook DMs that helps automate "
                "inbound communication and guide clients toward booking."
            )

        if includes:
            parts.append("It usually includes: " + ", ".join(includes[:5]) + ".")

        if for_whom:
            parts.append("It is best suited for: " + ", ".join(for_whom[:3]) + ".")

        if typical_result:
            parts.append("Typical results include: " + ", ".join(typical_result[:3]) + ".")

        parts.append("If you want, we can take a quick look at your case during a free consultation.")

        return " ".join(parts)

    def _is_service_includes_query(self, normalized: str) -> bool:
        includes_markers = [
            "що входить",
            "що входить у сервіс",
            "що входить в сервіс",
            "що включено",
            "what is included",
            "what's included",
            "what does the service include",
        ]
        return self._contains_any(normalized, includes_markers)

    def _is_for_whom_query(self, normalized: str) -> bool:
        for_whom_markers = [
            "для кого це",
            "а для кого",
            "для кого цей",
            "кому це підходить",
            "для кого підходить",
            "for whom",
            "who is this for",
        ]
        return self._contains_any(normalized, for_whom_markers)

    def _is_what_does_bot_do_query(self, normalized: str) -> bool:
        what_does_markers = [
            "що робить ваш бот",
            "що робить бот",
            "що вміє бот",
            "як працює бот",
            "що конкретно робить",
            "як це працює",
            "як ви працюєте",
            "які функції",
            "розкажіть про",
            "розкажіть детальніше",
            "що саме ви робите",
            "how does it work",
            "what does it do",
            "what are the features",
        ]
        return self._contains_any(normalized, what_does_markers)

    def _get_service_description_fallback_reply(
        self,
        language: str,
        user_text: Optional[str] = None,
    ) -> str:
        normalized = self._normalize(user_text or "")
        if not self._is_legacy_flowly_kb():
            if normalized:
                fact_reply = self._get_business_fact_reply(normalized, language)
                if fact_reply:
                    return fact_reply
            if normalized and self._is_service_includes_query(normalized):
                faq_answer = self._get_faq_answer("Що входить у сервіс?", language)
                if faq_answer:
                    return faq_answer
            return self._get_service_fallback_reply(language)

        standard_description = (
            "Ми налаштовуємо AI-бота для Instagram/Facebook/Telegram/WhatsApp/Viber. "
            "Він відповідає на типові повідомлення, збирає заявки, кваліфікує клієнтів "
            "і допомагає доводити їх до запису або дзвінка. "
            "Можемо швидко пройтись по вашому процесу зі спеціалістом і підказати, "
            "що варто автоматизувати першим. Зручно коротко зідзвонитись?"
        )
        if normalized and self._is_how_it_works_query(normalized):
            faq_answer = self._get_faq_answer("Як це працює?", language)
            if faq_answer:
                return faq_answer
            return (
                "Спочатку розбираємо ваші типові звернення і сценарії в месенджерах. "
                "Потім налаштовуємо бота, запис і передачу заявок, а після запуску дивимось "
                "на реальні діалоги та допрацьовуємо відповіді."
            )
        if normalized and self._is_what_does_bot_do_query(normalized) and language == "uk":
            return standard_description
        if normalized and self._is_service_includes_query(normalized):
            faq_answer = self._get_faq_answer("Що входить у сервіс?", language)
            if faq_answer:
                return faq_answer
        if normalized and self._is_for_whom_query(normalized):
            faq_answer = self._get_faq_answer("Для кого це?", language)
            if faq_answer:
                return faq_answer
        if normalized and self._is_what_does_bot_do_query(normalized):
            faq_answer = self._get_faq_answer("Як це працює?", language)
            if faq_answer:
                return faq_answer

        service = self._get_primary_service()
        service = service or {}
        short_description = service.get("short_description", "")
        includes = service.get("includes", [])

        if language == "uk":
            if normalized and self._is_for_whom_query(normalized) and not short_description:
                return (
                    "Найчастіше Flowly підходить сервісним бізнесам: салонам краси, "
                    "клінікам, стоматологіям, спортзалам, автосервісам та іншим компаніям, "
                    "які регулярно отримують звернення в месенджерах."
                )
            if normalized and self._is_service_includes_query(normalized) and not includes:
                return (
                    "У сервіс входять аудит воронки заявок, налаштування автоматичних "
                    "відповідей у месенджерах, AI-бот для обробки звернень, онлайн-запис, "
                    "нагадування, єдине місце для всіх заявок і подальша оптимізація після запуску."
                )

            parts: List[str] = []
            if short_description:
                parts.append(standard_description)
            else:
                parts.append(standard_description)
            if includes and normalized and self._is_service_includes_query(normalized):
                parts.append("У сервіс зазвичай входить " + ", ".join(includes[:4]) + ".")
            return " ".join(parts)

        parts = []
        if short_description:
            parts.append(short_description)
        else:
            parts.append(
                "We set up an AI bot that handles common inbound questions, helps qualify leads, and guides clients toward booking."
            )
        if includes and normalized and self._is_service_includes_query(normalized):
            parts.append("The service usually includes " + ", ".join(includes[:4]) + ".")
        return " ".join(parts)

    def _is_how_it_works_query(self, normalized: str) -> bool:
        how_it_works_markers = [
            "як це працює",
            "як працює сервіс",
            "як ви працюєте",
            "як відбувається робота",
            "як проходить впровадження",
            "how does it work",
            "how do you work",
        ]
        return self._contains_any(normalized, how_it_works_markers)

    def _get_implementation_time_reply(self, language: str) -> str:
        faq_answer = self._get_faq_answer("Скільки триває запуск?", language)
        if faq_answer:
            return faq_answer
        return "Типовий запуск займає 7-10 днів, залежно від складності кейсу."

    def get_buying_signal_reply(self, language: str) -> str:
        return (
            "Так, можемо допомогти з ботом для месенджерів. Він відповідатиме на типові "
            "питання, збиратиме заявки і допомагатиме доводити клієнта до запису або дзвінка. "
            "Можемо коротко обговорити ваш процес і підказати, що варто автоматизувати першим."
        )

    def _get_niche_fit_reply(self, normalized: str, language: str) -> Optional[str]:
        niche_markers = {
            "dentistry": [
                "стоматолог",
                "стоматологія",
                "стоматологии",
                "dental",
                "dentistry",
            ],
            "clinic": [
                "клінік",
                "клиник",
                "clinic",
            ],
            "auto_service": [
                "автосерв",
                "сто",
                "car service",
                "auto service",
                "repair shop",
            ],
            "beauty_salon": [
                "салон краси",
                "б'юті",
                "бюті",
                "beauty salon",
                "beauty studio",
            ],
        }

        matched_dentistry = self._contains_any(normalized, niche_markers["dentistry"])
        matched_clinic = self._contains_any(normalized, niche_markers["clinic"])
        matched_auto_service = (
            self._contains_any(normalized, ["автосерв", "car service", "auto service", "repair shop"])
            or bool(re.search(r"(?<![A-Za-zА-Яа-яІіЇїЄєҐґ])сто(?![A-Za-zА-Яа-яІіЇїЄєҐґ])", normalized))
        )
        matched_beauty_salon = self._contains_any(normalized, niche_markers["beauty_salon"])
        if not matched_dentistry and not matched_clinic and not matched_auto_service and not matched_beauty_salon:
            return None

        if language == "en":
            if matched_dentistry:
                return (
                    "Yes, it is a good fit for dental practices. The bot can help with booking, "
                    "answer common patient questions, and send visit reminders."
                )
            if matched_clinic:
                return (
                    "Yes, it is a good fit for clinics. The bot can help with booking, answer common "
                    "questions, and remind clients about upcoming visits."
                )
            if matched_auto_service:
                return (
                    "Yes, it can be a good fit for a car service. The bot can answer common questions, "
                    "help with booking, and pass requests to your specialist."
                )
            return (
                "Yes, it can work well for a beauty salon. The bot can help with booking, answer "
                "common questions, and remind clients about upcoming visits."
            )

        if matched_auto_service:
            return (
                "Так, для автосервісу це може добре підійти — бот може відповідати на типові "
                "питання, уточнювати деталі по авто і вести клієнта до запису. Щоб не вигадувати "
                "сценарій у чаті, краще коротко розібрати ваш процес зі спеціалістом на дзвінку."
            )

        if matched_dentistry:
            return (
                "Так, для стоматологій це добре працює: бот відповідає на типові питання, "
                "допомагає з записом і нагадує про візити."
            )
        if matched_auto_service:
            return (
                "Так, для автосервісу це може добре підійти — бот може відповідати на типові "
                "питання, уточнювати деталі по авто і вести клієнта до запису. Щоб не вигадувати "
                "сценарій у чаті, краще коротко розібрати ваш процес зі спеціалістом на дзвінку."
            )
        if matched_beauty_salon:
            return (
                "Так, для салону краси це добре підходить: бот відповідає на типові питання, "
                "допомагає з записом і нагадує про візити. Щоб не вигадувати сценарій у чаті, "
                "краще коротко розібрати ваш процес зі спеціалістом на дзвінку."
            )
        if matched_clinic:
            return (
            "Так, для клінік це добре працює: бот допомагає з записом, відповідає на типові "
            "питання і нагадує про візити."
            )
        return None

    def get_niche_fit_reply(self, text: str, language: Optional[str] = None) -> Optional[str]:
        resolved_language = language or self._detect_language(text)
        return self._get_niche_fit_reply(self._normalize(text), resolved_language)

    def _generate_service_ai_reply(
        self,
        user_message: str,
        history: List[Dict[str, Any]],
        language: str,
    ) -> str:
        grounding_context = self._build_service_grounding_context(language=language)
        system_instruction = self._get_service_system_instruction(language=language)

        try:
            ai_result = self.ai_service.try_generate_reply(
                user_message=user_message,
                history=history,
                grounding_context=grounding_context,
                system_instruction=system_instruction,
            )
        except TypeError:
            # Fallback for the current AIService signature if it still only accepts
            # user_message and history.
            ai_result = self.ai_service.try_generate_reply(
                user_message=user_message,
                history=history,
            )

        if isinstance(ai_result, dict):
            logger.debug(
                "ReplyService service-query ai_result: used_ai=%s reason=%s has_reply_text=%s",
                ai_result.get("used_ai"),
                ai_result.get("reason"),
                bool(ai_result.get("reply_text")),
            )

        ai_reply_text = ai_result.get("reply_text") if isinstance(ai_result, dict) else None
        if ai_reply_text:
            logger.debug("ReplyService service-query path: OpenAI reply_text returned")
            return str(ai_reply_text)

        if isinstance(ai_result, dict):
            logger.debug(
                "ReplyService service-query fallback used: used_ai=%s reason=%s",
                ai_result.get("used_ai"),
                ai_result.get("reason"),
            )
        return self._get_service_fallback_reply(language)

    def generate_reply(self, message: NormalizedMessage, intent: Optional[IntentType] = None) -> str:
        text = message.user_message.strip()
        language = self._detect_language(text)
        resolved_intent = intent or IntentType.GENERAL_QUESTION
        normalized = self._normalize(text)

        if not self._is_legacy_flowly_kb():
            if resolved_intent != IntentType.BOOKING_REQUEST:
                contextual_reply = self.get_contextual_front_desk_reply(message, resolved_intent)
                if contextual_reply:
                    return contextual_reply
            fact_reply = self._get_business_fact_reply(normalized, language)
            if fact_reply:
                return fact_reply
            pricing_followup_reply = self._get_contextual_pricing_followup_reply(
                message,
                language,
                normalized,
            )
            if pricing_followup_reply:
                return pricing_followup_reply

            if resolved_intent == IntentType.PRICE:
                return self._get_pricing_reply(language)

            if self._is_greeting(normalized):
                return self._get_greeting_reply(language)

            if resolved_intent == IntentType.REJECTION:
                return self._get_rejection_reply(language)

            if resolved_intent == IntentType.FRUSTRATED:
                return self._get_frustrated_reply(language)

            if resolved_intent == IntentType.AGGRESSIVE_OBJECTION:
                return self._get_unknown_fallback_reply(language)

            if self._is_service_query(normalized) or self._is_mid_level_query(normalized):
                history = self.memory_service.get_history(message.sender_id)
                return self._generate_service_ai_reply(
                    user_message=text,
                    history=history,
                    language=language,
                )

            ai_reply = self._get_ai_fallback_reply(
                text,
                language,
                history=self.memory_service.get_history(message.sender_id),
            )
            if ai_reply:
                return ai_reply

            return self._get_unknown_fallback_reply(language)

        if resolved_intent == IntentType.PRICE:
            return self._get_pricing_reply(language)

        if resolved_intent == IntentType.CHANNELS:
            channel_reply = self._get_channel_reply(text, language)
            if channel_reply:
                return channel_reply
            return self._fallback_for_intent(IntentType.CHANNELS, language)

        if resolved_intent == IntentType.INDUSTRIES:
            return self._get_industries_reply(language)

        if resolved_intent == IntentType.USE_CASES:
            return self._get_use_cases_reply(language)

        if resolved_intent == IntentType.SERVICE_DESCRIPTION:
            return self._get_service_description_fallback_reply(language, text)

        if resolved_intent == IntentType.INTEREST_SIGNAL:
            return self._get_interest_signal_reply(language)

        if resolved_intent == IntentType.REJECTION:
            return self._get_rejection_reply(language)

        if resolved_intent == IntentType.FRUSTRATED:
            return self._get_frustrated_reply(language)

        if resolved_intent == IntentType.AGGRESSIVE_OBJECTION:
            return self._get_aggressive_objection_reply(language)

        if resolved_intent == IntentType.HESITATION:
            return (
                "Нормально, тут не треба вирішувати одразу. Бот має сенс, якщо є багато "
                "повторюваних питань, заявки губляться або менеджери довго відповідають. "
                "Де зараз найбільше ручної переписки?"
            )

        if resolved_intent == IntentType.BUYING_SIGNAL:
            return (
                "Супер, можемо допомогти. Почати можна з простого сценарію: напишіть, будь "
                "ласка, який у вас бізнес і де зараз найбільше звернень? Тут уже можна "
                "підібрати мінімальний сценарій під ваші канали й процес."
            )

        if resolved_intent == IntentType.START_REQUIREMENTS:
            return (
                "Для старту потрібно зрозуміти 4 речі: де клієнти пишуть, які питання "
                "повторюються, що треба збирати в заявці і куди передавати заявки. Після "
                "цього можна скласти перший сценарій бота. Тут уже краще коротко розібрати "
                "ваш кейс зі спеціалістом, щоб не відповідати загально."
            )

        if resolved_intent in {IntentType.CONSULTATION_INTEREST, IntentType.BOOKING_REQUEST}:
            return self._get_consultation_reply(language)

        if self._is_greeting(normalized):
            return self._get_greeting_reply(language)

        niche_fit_reply = self._get_niche_fit_reply(normalized, language)
        if niche_fit_reply:
            return niche_fit_reply

        if self._is_implementation_time_query(normalized):
            return self._get_implementation_time_reply(language)

        if self._is_guarantee_query(normalized):
            return self._get_guarantee_reply(language)

        if self._is_client_questions_query(normalized):
            return self._get_client_questions_reply(language)

        if self._is_repair_price_query(normalized):
            return self._get_repair_price_reply(language)

        if self._is_price_objection(normalized):
            return self._get_price_objection_reply(language)

        if self._is_service_query(normalized) or self._is_mid_level_query(normalized):
            history = self.memory_service.get_history(message.sender_id)
            return self._generate_service_ai_reply(
                user_message=text,
                history=history,
                language=language,
            )

        ai_reply = self._get_ai_fallback_reply(
            text,
            language,
            history=self.memory_service.get_history(message.sender_id),
        )
        if ai_reply:
            return ai_reply

        # Unknown intent fallback only.
        if not self._is_legacy_flowly_kb():
            return self._get_unknown_fallback_reply(language)
        return self._fallback_for_intent(IntentType.SERVICE_DESCRIPTION, language)
        
