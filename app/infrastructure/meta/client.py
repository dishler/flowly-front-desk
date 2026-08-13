from typing import Any, Dict

import httpx

from app.core.config import get_settings


class MetaClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def send_text_message(
        self,
        platform: str,
        recipient_id: str,
        text: str,
    ) -> Dict[str, Any]:
        if not self.settings.meta_send_enabled:
            return {
                "sent": False,
                "stub": True,
                "reason": "META_SEND_ENABLED=false",
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
            }

        if not self.settings.meta_page_access_token:
            return {
                "sent": False,
                "stub": True,
                "reason": "Missing META_PAGE_ACCESS_TOKEN",
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
            }

        if platform not in {"facebook", "instagram"}:
            return {
                "sent": False,
                "stub": True,
                "reason": f"Unsupported platform={platform}",
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
            }

        url = f"https://graph.facebook.com/{self.settings.meta_graph_api_version}/me/messages"

        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": text},
            "messaging_type": "RESPONSE",
        }

        params = {
            "access_token": self.settings.meta_page_access_token,
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, params=params, json=payload)
                response.raise_for_status()
                data = response.json()

            return {
                "sent": True,
                "stub": False,
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
                "meta_response": data,
            }

        except httpx.HTTPStatusError as exc:
            error_body = ""
            try:
                error_body = exc.response.text
            except Exception:
                error_body = ""

            return {
                "sent": False,
                "stub": False,
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
                "status_code": exc.response.status_code if exc.response else None,
                "error": str(exc),
                "meta_error_body": error_body,
            }

        except httpx.HTTPError as exc:
            return {
                "sent": False,
                "stub": False,
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
                "error": str(exc),
            }