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

        access_token = self._access_token_for_platform(platform)
        if not access_token:
            token_name = (
                "META_FACEBOOK_PAGE_ACCESS_TOKEN"
                if platform == "facebook"
                else "META_PAGE_ACCESS_TOKEN"
            )
            return {
                "sent": False,
                "stub": True,
                "reason": f"Missing {token_name}",
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

        if platform == "instagram":
            url = "https://graph.instagram.com/v25.0/me/messages"
        else:
            url = f"https://graph.facebook.com/{self.settings.meta_graph_api_version}/me/messages"

        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": text},
            "messaging_type": "RESPONSE",
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, headers=headers, json=payload)
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

    def _access_token_for_platform(self, platform: str) -> str:
        if platform == "facebook":
            return self.settings.meta_facebook_page_access_token.strip()

        return self.settings.meta_page_access_token.strip()
