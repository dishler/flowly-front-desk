from __future__ import annotations

from typing import Any, Optional

from app.application.dto.normalized_message import NormalizedMessage


class TelegramPayloadParser:
    PLATFORM = "telegram"

    @staticmethod
    def parse(payload: dict[str, Any]) -> Optional[NormalizedMessage]:
        if not isinstance(payload, dict):
            return None

        update_id = payload.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return None

        message = payload.get("message")
        if not isinstance(message, dict):
            return None

        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None

        chat_type = chat.get("type")
        if chat_type != "private":
            return None

        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0:
            return None

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return None

        telegram_recipient_id = f"telegram:{chat_id}"

        return NormalizedMessage(
            platform=TelegramPayloadParser.PLATFORM,
            sender_id=telegram_recipient_id,
            recipient_id=telegram_recipient_id,
            message_mid=f"telegram:update:{update_id}",
            user_message=text,
        )
