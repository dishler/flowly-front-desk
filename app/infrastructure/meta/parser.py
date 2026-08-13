from typing import Any, Optional

from app.application.dto.normalized_message import NormalizedMessage


class MetaPayloadParser:
    @staticmethod
    def parse(payload: dict[str, Any]) -> Optional[NormalizedMessage]:
        obj = payload.get("object")

        if obj == "page":
            return MetaPayloadParser._parse_facebook(payload)

        if obj == "instagram":
            return MetaPayloadParser._parse_instagram(payload)

        return None

    @staticmethod
    def _parse_facebook(payload: dict[str, Any]) -> Optional[NormalizedMessage]:
        entries = payload.get("entry", [])
        if not entries:
            return None

        entry = entries[0]
        messaging = entry.get("messaging", [])
        if not messaging:
            return None

        event = messaging[0]
        message = event.get("message", {})
        text = message.get("text")
        mid = message.get("mid")

        sender_id = event.get("sender", {}).get("id")
        recipient_id = event.get("recipient", {}).get("id")
        timestamp = event.get("timestamp")

        if not text or not sender_id or not recipient_id or not mid:
            return None

        return NormalizedMessage(
            platform="facebook",
            sender_id=str(sender_id),
            recipient_id=str(recipient_id),
            message_mid=str(mid),
            user_message=str(text),
            timestamp=timestamp,
        )

    @staticmethod
    def _parse_instagram(payload: dict[str, Any]) -> Optional[NormalizedMessage]:
        entries = payload.get("entry", [])
        if not entries:
            return None

        entry = entries[0]
        changes = entry.get("changes", [])
        if not changes:
            return None

        change = changes[0]
        value = change.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return None

        message = messages[0]
        text = message.get("text", {}).get("body")
        mid = message.get("id")
        sender_id = message.get("from")
        recipient_id = value.get("metadata", {}).get(
            "phone_number_id", "instagram-recipient"
        )
        timestamp_raw = message.get("timestamp")

        if not text or not sender_id or not mid:
            return None

        timestamp = None
        if timestamp_raw is not None:
            try:
                timestamp = int(timestamp_raw)
            except (TypeError, ValueError):
                timestamp = None

        return NormalizedMessage(
            platform="instagram",
            sender_id=str(sender_id),
            recipient_id=str(recipient_id),
            message_mid=str(mid),
            user_message=str(text),
            timestamp=timestamp,
        )
        