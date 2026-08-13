from __future__ import annotations

import json
from datetime import datetime
from typing import Any


class RedisBookingStateService:
    def __init__(
        self,
        redis_client,
        ttl_seconds: int = 3600,
        completed_ttl_seconds: int = 60 * 60 * 24 * 30,
    ) -> None:
        self.redis_client = redis_client
        self.ttl_seconds = ttl_seconds
        self.completed_ttl_seconds = completed_ttl_seconds

    def _key(self, sender_id: str) -> str:
        return f"booking:pending:{sender_id}"

    def _completed_key(self, sender_id: str) -> str:
        return f"booking:completed:{sender_id}"

    def has_pending_confirmation(self, sender_id: str) -> bool:
        return self.redis_client.exists(self._key(sender_id)) == 1

    def save_pending_confirmation(self, sender_id: str, data: dict[str, Any]) -> None:
        payload = dict(data)

        start_dt = payload.get("start_dt")
        if isinstance(start_dt, datetime):
            payload["start_dt"] = start_dt.isoformat()

        self.redis_client.setex(
            self._key(sender_id),
            self.ttl_seconds,
            json.dumps(payload),
        )

    def get_pending_confirmation(self, sender_id: str) -> dict[str, Any] | None:
        raw = self.redis_client.get(self._key(sender_id))
        if not raw:
            return None

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        payload = json.loads(raw)

        start_dt = payload.get("start_dt")
        if isinstance(start_dt, str):
            payload["start_dt"] = datetime.fromisoformat(start_dt)

        return payload

    def clear_pending_confirmation(self, sender_id: str) -> None:
        self.redis_client.delete(self._key(sender_id))

    def has_completed_booking(self, sender_id: str) -> bool:
        return self.redis_client.exists(self._completed_key(sender_id)) == 1

    def save_completed_booking(self, sender_id: str, data: dict[str, Any]) -> None:
        payload = {
            "start_dt": data.get("start_dt"),
            "customer_name": data.get("customer_name"),
            "email": data.get("email"),
            "phone": data.get("phone"),
            "calendar_event_id": data.get("calendar_event_id"),
        }

        start_dt = payload.get("start_dt")
        if isinstance(start_dt, datetime):
            payload["start_dt"] = start_dt.isoformat()

        self.redis_client.setex(
            self._completed_key(sender_id),
            self.completed_ttl_seconds,
            json.dumps(payload),
        )

    def get_completed_booking(self, sender_id: str) -> dict[str, Any] | None:
        raw = self.redis_client.get(self._completed_key(sender_id))
        if not raw:
            return None

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None

        return {
            "start_dt": payload.get("start_dt"),
            "customer_name": payload.get("customer_name"),
            "email": payload.get("email"),
            "phone": payload.get("phone"),
            "calendar_event_id": payload.get("calendar_event_id"),
        }

    def clear_completed_booking(self, sender_id: str) -> None:
        self.redis_client.delete(self._completed_key(sender_id))
