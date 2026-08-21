from __future__ import annotations

from app.application.dto.normalized_message import NormalizedMessage
from app.application.services.front_desk_config_service import FrontDeskConfigService
from app.application.services.knowledge_service import KnowledgeService
from app.application.services.redis_memory_service import RedisMemoryService
from app.application.services.reply_service import ReplyService


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True


class DummyAIService:
    def try_generate_reply(self, *args, **kwargs) -> dict:
        return {"reply_text": None}


def _message(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        platform="instagram",
        sender_id="patient-1",
        recipient_id="clinic",
        message_mid="",
        user_message=text,
    )


def _reply_service(redis_memory: RedisMemoryService) -> ReplyService:
    return ReplyService(
        ai_service=DummyAIService(),
        memory_service=redis_memory,
        knowledge_service=KnowledgeService("app/data/knowledge_base.json"),
        front_desk_config_service=FrontDeskConfigService("app/data/front_desk_config.json"),
    )


def test_redis_memory_service_exposes_context_methods():
    service = RedisMemoryService(FakeRedis())

    assert callable(service.get_context)
    assert callable(service.update_context)


def test_redis_memory_context_update_and_get_round_trip():
    service = RedisMemoryService(FakeRedis())

    service.update_context(
        "patient-1",
        current_service_id="dental_cleaning",
        question_context="pricing",
    )

    assert service.get_context("patient-1") == {
        "current_service_id": "dental_cleaning",
        "question_context": "pricing",
    }


def test_redis_memory_missing_context_returns_empty_dict():
    service = RedisMemoryService(FakeRedis())

    assert service.get_context("missing") == {}


def test_redis_memory_existing_history_behavior_still_works():
    service = RedisMemoryService(FakeRedis())

    service.add_user_message("patient-1", "Привіт")
    service.add_assistant_message("patient-1", "Вітаю!")

    assert service.get_history("patient-1") == [
        "user: Привіт",
        "assistant: Вітаю!",
    ]


def test_contextual_pricing_persists_across_redis_backed_service_instances():
    fake_redis = FakeRedis()

    first_memory = RedisMemoryService(fake_redis)
    first_reply_service = _reply_service(first_memory)
    first = first_reply_service.generate_reply(_message("Скільки коштує консультація?"))

    second_memory = RedisMemoryService(fake_redis)
    second_reply_service = _reply_service(second_memory)
    followup = second_reply_service.generate_reply(_message("А чистка?"))

    third_memory = RedisMemoryService(fake_redis)
    third_reply_service = _reply_service(third_memory)
    repeated = third_reply_service.generate_reply(_message("ціна"))

    assert "700 грн" in first
    assert "1800 грн" in followup
    assert "1800 грн" in repeated
    assert "2200 грн" not in repeated
    assert third_memory.get_context("patient-1") == {
        "current_service_id": "dental_cleaning",
        "question_context": "pricing",
    }
