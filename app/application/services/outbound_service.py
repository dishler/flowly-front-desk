from __future__ import annotations

from typing import Any, Dict

from app.infrastructure.meta.client import MetaClient
from app.infrastructure.telegram.client import TelegramClient


class OutboundService:
    def __init__(
        self,
        meta_client: MetaClient,
        telegram_client: TelegramClient | None = None,
    ) -> None:
        self.meta_client = meta_client
        self.telegram_client = telegram_client

    def send_reply(
        self,
        platform: str,
        recipient_id: str,
        text: str,
    ) -> Dict[str, Any]:
        if platform in {"facebook", "instagram"}:
            return self.meta_client.send_text_message(
                platform=platform,
                recipient_id=recipient_id,
                text=text,
            )

        if platform == "telegram":
            if self.telegram_client is None:
                return {
                    "sent": False,
                    "stub": True,
                    "reason": "Telegram client is not configured",
                    "platform": platform,
                    "recipient_id": recipient_id,
                    "text": text,
                }
            return self.telegram_client.send_text_message(
                recipient_id=recipient_id,
                text=text,
            )

        return {
            "sent": False,
            "stub": True,
            "reason": f"Unsupported platform={platform}",
            "platform": platform,
            "recipient_id": recipient_id,
            "text": text,
        }
