from typing import Optional
import logging

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class RedisClientProvider:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client: Optional[redis.Redis] = None

    def get_client(self) -> Optional[redis.Redis]:
        if not self.settings.redis_enabled:
            return None

        if self._client is not None:
            return self._client

        try:
            client = redis.Redis.from_url(
                self.settings.redis_url,
                decode_responses=True,
            )
            client.ping()
            self._client = client
            return self._client
        except redis.RedisError as exc:
            logger.exception("Redis connection failed while Redis is enabled")
            raise RuntimeError("Redis connection failed while Redis is enabled") from exc
            
