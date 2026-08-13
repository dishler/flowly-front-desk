from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(default="Flowly Meta Bot")
    environment: str = Field(default="dev")
    debug: bool = Field(default=True)

    meta_verify_token: str = Field(default="change-me")
    meta_app_secret: str = Field(default="")
    meta_page_access_token: str = Field(default="")
    meta_graph_api_version: str = Field(default="v21.0")
    meta_send_enabled: bool = Field(default=False)

    openai_api_key: str = Field(default="")
    openai_model: str = Field(default="gpt-5-mini")
    openai_enabled: bool = Field(default=False)

    redis_enabled: bool = Field(default=False)
    redis_url: str = Field(default="redis://localhost:6379/0")
    redis_message_ttl_seconds: int = Field(default=60 * 60 * 24)
    redis_memory_ttl_seconds: int = Field(default=60 * 60 * 24 * 7)
    redis_booking_confirmation_ttl_seconds: int = Field(default=60 * 60)
    redis_completed_booking_ttl_seconds: int = Field(default=60 * 60 * 24 * 30)

    google_calendar_enabled: bool = Field(default=False)
    google_calendar_id: str = Field(default="")
    google_service_account_file: str = Field(default="")
    google_service_account_json: str = Field(default="")
    google_calendar_timezone: str = Field(default="Europe/Kyiv")

    default_timezone: str = Field(default="Europe/Kyiv")
    front_desk_config_path: str = Field(default="app/data/front_desk_config.json")
    knowledge_base_path: str = Field(default="app/data/knowledge_base.json")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def validate_production_settings(self) -> None:
        if self.environment.strip().lower() != "production":
            return

        errors: list[str] = []

        if not self.meta_verify_token.strip() or self.meta_verify_token == "change-me":
            errors.append("META_VERIFY_TOKEN must be configured")

        if not self.meta_app_secret.strip():
            errors.append("META_APP_SECRET must be configured")

        if not self.meta_send_enabled:
            errors.append("META_SEND_ENABLED must be true")

        if not self.meta_page_access_token.strip():
            errors.append("META_PAGE_ACCESS_TOKEN must be configured")

        if not self.openai_enabled:
            errors.append("OPENAI_ENABLED must be true")

        if not self.openai_api_key.strip():
            errors.append("OPENAI_API_KEY must be configured")

        if not self.redis_enabled:
            errors.append("REDIS_ENABLED must be true")

        if not self.redis_url.strip():
            errors.append("REDIS_URL must be configured")

        if not self.google_calendar_enabled:
            errors.append("GOOGLE_CALENDAR_ENABLED must be true")

        if not self.google_calendar_id.strip():
            errors.append("GOOGLE_CALENDAR_ID must be configured")

        if not (
            self.google_service_account_file.strip()
            or self.google_service_account_json.strip()
        ):
            errors.append(
                "GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON must be configured"
            )

        if (
            not self.front_desk_config_path.strip()
            or self.front_desk_config_path == "app/data/front_desk_config.json"
        ):
            errors.append("FRONT_DESK_CONFIG_PATH must point to an explicit client config")

        if (
            not self.knowledge_base_path.strip()
            or self.knowledge_base_path == "app/data/knowledge_base.json"
        ):
            errors.append("KNOWLEDGE_BASE_PATH must point to an explicit client knowledge base")

        if errors:
            raise RuntimeError(
                "Invalid production configuration: " + "; ".join(errors)
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
