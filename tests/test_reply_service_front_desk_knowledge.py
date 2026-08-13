import json

from app.application.dto.normalized_message import NormalizedMessage
from app.application.services.front_desk_config_service import FrontDeskConfigService
from app.application.services.knowledge_service import KnowledgeService
from app.application.services.memory_service import MemoryService
from app.application.services.reply_service import ReplyService
from app.domain.enums import IntentType


class DummyAIService:
    def try_generate_reply(
        self,
        user_message: str,
        history=None,
        grounding_context=None,
        system_instruction=None,
    ) -> dict:
        return {"reply_text": None}


def _reply_service() -> ReplyService:
    return ReplyService(
        ai_service=DummyAIService(),
        memory_service=MemoryService(),
        knowledge_service=KnowledgeService("tests/fixtures/dental_knowledge_base.json"),
        front_desk_config_service=FrontDeskConfigService("tests/fixtures/front_desk_config.json"),
    )


def _message(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        platform="instagram",
        sender_id="user-1",
        recipient_id="bot-1",
        message_mid="",
        user_message=text,
    )


def _assert_no_flowly_leakage(text: str) -> None:
    serialized = text.lower()
    assert "flowly" not in serialized
    assert "200" not in serialized
    assert "usd" not in serialized
    assert "ai-бот" not in serialized
    assert "automation" not in serialized


def test_dental_price_reply_comes_from_knowledge_base():
    reply = _reply_service().generate_reply(_message("скільки коштує консультація?"), IntentType.PRICE)

    assert "800 грн" in reply
    assert "огляду" in reply
    _assert_no_flowly_leakage(reply)


def test_dental_service_reply_comes_from_knowledge_base():
    reply = _reply_service().generate_reply(_message("що ви робите?"), IntentType.SERVICE_DESCRIPTION)

    assert "Стоматологічна клініка" in reply
    assert "лікування карієсу" in reply
    _assert_no_flowly_leakage(reply)


def test_dental_location_hours_contact_and_faq_come_from_knowledge_base():
    service = _reply_service()

    location = service.generate_reply(_message("де ви знаходитесь?"), IntentType.GENERAL_QUESTION)
    hours = service.generate_reply(_message("який графік роботи?"), IntentType.GENERAL_QUESTION)
    contact = service.generate_reply(_message("який телефон?"), IntentType.GENERAL_QUESTION)
    faq = service.generate_reply(_message("Чи робите чистку?"), IntentType.GENERAL_QUESTION)

    assert "Київ" in location
    assert "09:00-19:00" in hours
    assert "+380991112233" in contact
    assert "професійну гігієну" in faq
    for reply in [location, hours, contact, faq]:
        _assert_no_flowly_leakage(reply)
