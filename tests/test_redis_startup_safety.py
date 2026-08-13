import importlib
import sys
from types import SimpleNamespace

import pytest
import redis

from app.core.config import get_settings
from app.infrastructure.persistence import redis_client as redis_client_module
from app.infrastructure.persistence.redis_client import RedisClientProvider


def _clear_main_import_state() -> None:
    sys.modules.pop("app.main", None)
    get_settings.cache_clear()


def _set_valid_production_env(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("META_VERIFY_TOKEN", "verify-token")
    monkeypatch.setenv("META_APP_SECRET", "app-secret")
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "page-token")
    monkeypatch.setenv("META_SEND_ENABLED", "true")
    monkeypatch.setenv("OPENAI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://:secret-password@redis:6379/0")
    monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "calendar@example.com")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setenv("FRONT_DESK_CONFIG_PATH", "tests/fixtures/front_desk_config.json")
    monkeypatch.setenv("KNOWLEDGE_BASE_PATH", "tests/fixtures/knowledge_base.json")


def test_app_startup_enforces_production_configuration(monkeypatch):
    _clear_main_import_state()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("REDIS_ENABLED", "false")

    try:
        with pytest.raises(RuntimeError, match="Invalid production configuration"):
            importlib.import_module("app.main")
    finally:
        _clear_main_import_state()


def test_redis_connection_failure_fails_closed_when_redis_enabled(monkeypatch):
    def fail_from_url(*args, **kwargs):
        raise redis.RedisError("connection refused")

    monkeypatch.setattr(redis_client_module.redis.Redis, "from_url", fail_from_url)

    provider = RedisClientProvider()
    provider.settings = SimpleNamespace(
        redis_enabled=True,
        redis_url="redis://:secret-password@redis:6379/0",
    )

    with pytest.raises(RuntimeError) as exc_info:
        provider.get_client()

    assert "Redis connection failed while Redis is enabled" in str(exc_info.value)
    assert "secret-password" not in str(exc_info.value)


def test_dev_with_redis_disabled_allows_in_memory_fallback(monkeypatch):
    called = False

    def fake_from_url(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(redis_client_module.redis.Redis, "from_url", fake_from_url)

    provider = RedisClientProvider()
    provider.settings = SimpleNamespace(redis_enabled=False)

    assert provider.get_client() is None
    assert called is False


def test_production_redis_connection_failure_does_not_fallback_to_memory(monkeypatch):
    _clear_main_import_state()
    _set_valid_production_env(monkeypatch)

    def fail_from_url(*args, **kwargs):
        raise redis.RedisError("connection refused")

    monkeypatch.setattr(redis_client_module.redis.Redis, "from_url", fail_from_url)

    try:
        with pytest.raises(RuntimeError, match="Redis connection failed while Redis is enabled"):
            importlib.import_module("app.main")
    finally:
        _clear_main_import_state()
