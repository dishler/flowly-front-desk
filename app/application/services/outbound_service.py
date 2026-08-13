from typing import Any, Dict

from app.infrastructure.meta.client import MetaClient


class OutboundService:
    def __init__(self, meta_client: MetaClient) -> None:
        self.meta_client = meta_client

    def send_reply(
        self,
        platform: str,
        recipient_id: str,
        text: str,
    ) -> Dict[str, Any]:
        return self.meta_client.send_text_message(
            platform=platform,
            recipient_id=recipient_id,
            text=text,
        )