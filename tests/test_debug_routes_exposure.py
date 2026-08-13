import importlib
import sys

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.infrastructure.persistence import redis_client as redis_client_module


DEBUG_ROUTES = {
    "/debug/booking/request",
    "/debug/booking/confirm",
    "/debug/reply",
}


class FakeRedisClient:
    def ping(self) -> bool:
        return True

    def get(self, key: str):
        return None

    def set(self, key: str, value: str, ex=None) -> None:
        return None

    def setex(self, key: str, ttl: int, value: str) -> None:
        return None

    def exists(self, key: str) -> int:
        return 0

    def delete(self, key: str) -> None:
        return None


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
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "calendar@example.com")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setenv("FRONT_DESK_CONFIG_PATH", "tests/fixtures/front_desk_config.json")
    monkeypatch.setenv("KNOWLEDGE_BASE_PATH", "tests/fixtures/knowledge_base.json")


def _import_main_app():
    _clear_main_import_state()
    return importlib.import_module("app.main").app


def _route_paths(app) -> set[str]:
    return {route.path for route in app.routes}


def test_production_route_list_excludes_debug_routes(monkeypatch):
    _set_valid_production_env(monkeypatch)
    monkeypatch.setattr(
        redis_client_module.redis.Redis,
        "from_url",
        lambda *args, **kwargs: FakeRedisClient(),
    )

    try:
        app = _import_main_app()

        assert DEBUG_ROUTES.isdisjoint(_route_paths(app))
    finally:
        _clear_main_import_state()


def test_dev_route_list_includes_debug_routes(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("REDIS_ENABLED", "false")

    try:
        app = _import_main_app()

        assert DEBUG_ROUTES.issubset(_route_paths(app))
    finally:
        _clear_main_import_state()


def test_production_debug_route_requests_return_404(monkeypatch):
    _set_valid_production_env(monkeypatch)
    monkeypatch.setattr(
        redis_client_module.redis.Redis,
        "from_url",
        lambda *args, **kwargs: FakeRedisClient(),
    )

    try:
        client = TestClient(_import_main_app())

        for path in DEBUG_ROUTES:
            response = client.post(path, json={"sender_id": "user-1", "message_text": "test"})
            assert response.status_code == 404
    finally:
        _clear_main_import_state()


def test_dev_debug_routes_remain_reachable(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("META_SEND_ENABLED", "false")
    monkeypatch.setenv("OPENAI_ENABLED", "false")
    monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", "false")

    try:
        client = TestClient(_import_main_app())

        booking_request = client.post(
            "/debug/booking/request",
            json={"sender_id": "user-1", "message_text": "давай кол"},
        )
        booking_confirm = client.post(
            "/debug/booking/confirm",
            json={"sender_id": "user-1", "message_text": "завтра о 12"},
        )
        reply = client.post(
            "/debug/reply",
            json={"sender_id": "debug-user", "message_text": "Привіт"},
        )

        assert booking_request.status_code == 200
        assert booking_confirm.status_code == 200
        assert reply.status_code == 200
        assert reply.json()["status"] == "ok"
    finally:
        _clear_main_import_state()
