from typing import Optional

import redis

from app.core.config import get_settings


class RedisDedupService:
    PROCESSING_VALUE = "processing"
    COMPLETED_VALUE = "completed"
    PROCESSING_TTL_SECONDS = 120

    def __init__(self, redis_client: Optional[redis.Redis]) -> None:
        self.redis_client = redis_client
        self.settings = get_settings()

    def is_duplicate(self, message_mid: str) -> bool:
        if self.redis_client is None:
            return False

        key = self._build_key(message_mid)
        return bool(self.redis_client.exists(key))

    def claim_processing(self, message_mid: str) -> bool:
        if self.redis_client is None:
            return True

        key = self._build_key(message_mid)
        return bool(
            self.redis_client.set(
                key,
                self.PROCESSING_VALUE,
                nx=True,
                ex=self.PROCESSING_TTL_SECONDS,
            )
        )

    def mark_processed(self, message_mid: str) -> None:
        if self.redis_client is None:
            return

        key = self._build_key(message_mid)
        self.redis_client.set(
            key,
            self.COMPLETED_VALUE,
            ex=self.settings.redis_message_ttl_seconds,
        )

    def release_processing(self, message_mid: str) -> None:
        if self.redis_client is None:
            return

        key = self._build_key(message_mid)
        self.redis_client.eval(
            """
            if redis.call("GET", KEYS[1]) == ARGV[1] then
                return redis.call("DEL", KEYS[1])
            end
            return 0
            """,
            1,
            key,
            self.PROCESSING_VALUE,
        )

    @staticmethod
    def _build_key(message_mid: str) -> str:
        return f"processed_message:{message_mid}"
        
