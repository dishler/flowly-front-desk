import pytest

from app.core.config import Settings


def valid_production_settings(**overrides) -> Settings:
    values = {
        "environment": "production",
        "meta_verify_token": "verify-token",
        "meta_app_secret": "app-secret",
        "meta_page_access_token": "page-token",
        "meta_send_enabled": True,
        "openai_enabled": True,
        "openai_api_key": "openai-key",
        "redis_enabled": True,
        "redis_url": "redis://redis:6379/0",
        "google_calendar_enabled": True,
        "google_calendar_id": "calendar@example.com",
        "google_service_account_json": "{}",
        "front_desk_config_path": "client/front_desk_config.json",
        "knowledge_base_path": "client/knowledge_base.json",
    }

    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_dev_allows_missing_production_integrations():
    Settings(
        _env_file=None,
        environment="dev",
    ).validate_production_settings()


def test_valid_production_configuration_passes():
    valid_production_settings().validate_production_settings()


def test_production_requires_meta_configuration():
    settings = valid_production_settings(meta_app_secret="")

    with pytest.raises(RuntimeError, match="META_APP_SECRET"):
        settings.validate_production_settings()


def test_production_requires_openai_configuration():
    settings = valid_production_settings(openai_api_key="")

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        settings.validate_production_settings()


def test_production_requires_redis_url():
    settings = valid_production_settings(redis_url="")

    with pytest.raises(RuntimeError, match="REDIS_URL"):
        settings.validate_production_settings()


def test_production_requires_calendar_configuration():
    settings = valid_production_settings(google_calendar_id="")

    with pytest.raises(RuntimeError, match="GOOGLE_CALENDAR_ID"):
        settings.validate_production_settings()


def test_production_requires_explicit_front_desk_config_path():
    settings = valid_production_settings(front_desk_config_path="app/data/front_desk_config.json")

    with pytest.raises(RuntimeError, match="FRONT_DESK_CONFIG_PATH"):
        settings.validate_production_settings()


def test_production_requires_explicit_knowledge_base_path():
    settings = valid_production_settings(knowledge_base_path="app/data/knowledge_base.json")

    with pytest.raises(RuntimeError, match="KNOWLEDGE_BASE_PATH"):
        settings.validate_production_settings()
