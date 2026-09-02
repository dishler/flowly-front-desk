from __future__ import annotations

import httpx

from app.core.config import Settings
from app.infrastructure.meta.client import MetaClient


class RecordingHttpClient:
    calls: list[dict] = []

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> "RecordingHttpClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def post(self, url: str, headers: dict, json: dict) -> httpx.Response:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": self.timeout,
            }
        )
        return httpx.Response(
            200,
            json={"recipient_id": "recipient", "message_id": "mid"},
            request=httpx.Request("POST", url),
        )


def _client(
    *,
    instagram_token: str = "instagram-token",
    facebook_token: str = "facebook-token",
) -> MetaClient:
    client = MetaClient()
    client.settings = Settings(
        _env_file=None,
        meta_send_enabled=True,
        meta_page_access_token=instagram_token,
        meta_facebook_page_access_token=facebook_token,
    )
    return client


def test_instagram_uses_existing_meta_page_access_token(monkeypatch):
    RecordingHttpClient.calls = []
    monkeypatch.setattr(httpx, "Client", RecordingHttpClient)

    result = _client().send_text_message(
        platform="instagram",
        recipient_id="ig-user",
        text="hello instagram",
    )

    assert result["sent"] is True
    assert len(RecordingHttpClient.calls) == 1
    call = RecordingHttpClient.calls[0]
    assert call["url"] == "https://graph.instagram.com/v25.0/me/messages"
    assert call["headers"]["Authorization"] == "Bearer instagram-token"
    assert call["json"] == {
        "recipient": {"id": "ig-user"},
        "message": {"text": "hello instagram"},
        "messaging_type": "RESPONSE",
    }


def test_facebook_uses_facebook_page_access_token(monkeypatch):
    RecordingHttpClient.calls = []
    monkeypatch.setattr(httpx, "Client", RecordingHttpClient)

    result = _client().send_text_message(
        platform="facebook",
        recipient_id="fb-user",
        text="hello facebook",
    )

    assert result["sent"] is True
    assert len(RecordingHttpClient.calls) == 1
    call = RecordingHttpClient.calls[0]
    assert call["url"] == "https://graph.facebook.com/v21.0/me/messages"
    assert call["headers"]["Authorization"] == "Bearer facebook-token"
    assert call["json"] == {
        "recipient": {"id": "fb-user"},
        "message": {"text": "hello facebook"},
        "messaging_type": "RESPONSE",
    }


def test_instagram_remains_backward_compatible_without_facebook_token(monkeypatch):
    RecordingHttpClient.calls = []
    monkeypatch.setattr(httpx, "Client", RecordingHttpClient)

    result = _client(facebook_token="").send_text_message(
        platform="instagram",
        recipient_id="ig-user",
        text="still works",
    )

    assert result["sent"] is True
    assert len(RecordingHttpClient.calls) == 1
    assert RecordingHttpClient.calls[0]["headers"]["Authorization"] == (
        "Bearer instagram-token"
    )


def test_missing_facebook_token_fails_safely_without_affecting_instagram(monkeypatch):
    RecordingHttpClient.calls = []
    monkeypatch.setattr(httpx, "Client", RecordingHttpClient)

    client = _client(facebook_token="")
    facebook_result = client.send_text_message(
        platform="facebook",
        recipient_id="fb-user",
        text="hello facebook",
    )
    instagram_result = client.send_text_message(
        platform="instagram",
        recipient_id="ig-user",
        text="hello instagram",
    )

    assert facebook_result == {
        "sent": False,
        "stub": True,
        "reason": "Missing META_FACEBOOK_PAGE_ACCESS_TOKEN",
        "platform": "facebook",
        "recipient_id": "fb-user",
        "text": "hello facebook",
    }
    assert instagram_result["sent"] is True
    assert len(RecordingHttpClient.calls) == 1
    assert RecordingHttpClient.calls[0]["url"] == (
        "https://graph.instagram.com/v25.0/me/messages"
    )
