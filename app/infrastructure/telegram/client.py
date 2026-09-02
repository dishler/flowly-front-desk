from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from app.core.config import get_settings


class TelegramClient:
    PLATFORM = "telegram"
    RECIPIENT_PREFIX = "telegram:"

    def __init__(self, settings: Optional[Any] = None) -> None:
        self.settings = settings or get_settings()

    def send_text_message(
        self,
        recipient_id: str,
        text: str,
    ) -> Dict[str, Any]:
        chat_id = self.extract_chat_id(recipient_id)

        if not chat_id:
            return {
                "sent": False,
                "stub": True,
                "reason": "Malformed Telegram recipient_id",
                "platform": self.PLATFORM,
                "recipient_id": recipient_id,
                "text": text,
            }

        if not self.settings.telegram_send_enabled:
            return {
                "sent": False,
                "stub": True,
                "reason": "TELEGRAM_SEND_ENABLED=false",
                "platform": self.PLATFORM,
                "recipient_id": recipient_id,
                "text": text,
            }

        if not self.settings.telegram_bot_token:
            return {
                "sent": False,
                "stub": True,
                "reason": "Missing TELEGRAM_BOT_TOKEN",
                "platform": self.PLATFORM,
                "recipient_id": recipient_id,
                "text": text,
            }

        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()

            return {
                "sent": True,
                "stub": False,
                "platform": self.PLATFORM,
                "recipient_id": recipient_id,
                "text": text,
                "telegram_response": data,
            }

        except httpx.HTTPStatusError as exc:
            return {
                "sent": False,
                "stub": False,
                "platform": self.PLATFORM,
                "recipient_id": recipient_id,
                "text": text,
                "status_code": exc.response.status_code if exc.response else None,
                "error_type": type(exc).__name__,
                "telegram_error_body": self._safe_response_text(exc.response),
            }

        except httpx.HTTPError as exc:
            return {
                "sent": False,
                "stub": False,
                "platform": self.PLATFORM,
                "recipient_id": recipient_id,
                "text": text,
                "error_type": type(exc).__name__,
            }

    @classmethod
    def extract_chat_id(cls, recipient_id: str) -> str:
        if not isinstance(recipient_id, str):
            return ""

        value = recipient_id.strip()
        if not value.startswith(cls.RECIPIENT_PREFIX):
            return ""

        chat_id = value[len(cls.RECIPIENT_PREFIX):].strip()
        if not chat_id.isdecimal():
            return ""

        return chat_id

    def _safe_response_text(self, response: httpx.Response | None) -> str:
        if response is None:
            return ""
        try:
            text = response.text
        except Exception:
            return ""
        return self._redact_token(text)

    def _redact_token(self, value: str) -> str:
        token = str(getattr(self.settings, "telegram_bot_token", "") or "")
        if not token:
            return value
        return value.replace(token, "[REDACTED_TELEGRAM_BOT_TOKEN]")
