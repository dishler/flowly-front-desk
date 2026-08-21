import json
from typing import Any, Dict, List, Optional

import redis

from app.core.config import get_settings


class RedisMemoryService:
    def __init__(self, redis_client: Optional[redis.Redis]) -> None:
        self.redis_client = redis_client
        self.settings = get_settings()

    def add_user_message(self, sender_id: str, text: str) -> None:
        self._append(sender_id, f"user: {text}")

    def add_assistant_message(self, sender_id: str, text: str) -> None:
        self._append(sender_id, f"assistant: {text}")

    def get_history(self, sender_id: str) -> List[str]:
        if self.redis_client is None:
            return []

        key = self._build_key(sender_id)
        raw = self.redis_client.get(key)
        if not raw:
            return []

        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(item) for item in data]
            return []
        except json.JSONDecodeError:
            return []

    def get_context(self, sender_id: str) -> Dict[str, Any]:
        if self.redis_client is None:
            return {}

        raw = self.redis_client.get(self._build_context_key(sender_id))
        if not raw:
            return {}

        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return dict(data)
            return {}
        except json.JSONDecodeError:
            return {}

    def update_context(self, sender_id: str, **values: Any) -> None:
        if self.redis_client is None:
            return

        context = self.get_context(sender_id)
        for key, value in values.items():
            if value is None:
                context.pop(key, None)
            else:
                context[key] = value

        self.redis_client.set(
            self._build_context_key(sender_id),
            json.dumps(context, ensure_ascii=False),
            ex=self.settings.redis_memory_ttl_seconds,
        )

    def _append(self, sender_id: str, value: str, max_items: int = 10) -> None:
        if self.redis_client is None:
            return

        history = self.get_history(sender_id)
        history.append(value)
        history = history[-max_items:]

        key = self._build_key(sender_id)
        self.redis_client.set(
            key,
            json.dumps(history, ensure_ascii=False),
            ex=self.settings.redis_memory_ttl_seconds,
        )

    @staticmethod
    def _build_key(sender_id: str) -> str:
        return f"meta_bot:memory:{sender_id}"

    @staticmethod
    def _build_context_key(sender_id: str) -> str:
        return f"meta_bot:memory_context:{sender_id}"
        
