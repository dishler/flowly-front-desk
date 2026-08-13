import hashlib
import hmac

from app.api.routes import meta_webhook


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return f"sha256={digest}"


def test_valid_meta_signature(monkeypatch):
    body = b'{"object":"page"}'
    secret = "test-meta-secret"

    monkeypatch.setattr(
        meta_webhook.settings,
        "meta_app_secret",
        secret,
    )
    monkeypatch.setattr(
        meta_webhook.settings,
        "environment",
        "production",
    )

    signature = _signature(secret, body)

    assert meta_webhook._verify_meta_signature(
        raw_body=body,
        signature_header=signature,
    )


def test_wrong_meta_signature_is_rejected(monkeypatch):
    body = b'{"object":"page"}'

    monkeypatch.setattr(
        meta_webhook.settings,
        "meta_app_secret",
        "correct-secret",
    )
    monkeypatch.setattr(
        meta_webhook.settings,
        "environment",
        "production",
    )

    assert not meta_webhook._verify_meta_signature(
        raw_body=body,
        signature_header="sha256=wrong",
    )


def test_missing_signature_is_rejected_in_production(monkeypatch):
    monkeypatch.setattr(
        meta_webhook.settings,
        "meta_app_secret",
        "test-secret",
    )
    monkeypatch.setattr(
        meta_webhook.settings,
        "environment",
        "production",
    )

    assert not meta_webhook._verify_meta_signature(
        raw_body=b"{}",
        signature_header=None,
    )


def test_missing_app_secret_fails_closed_in_production(monkeypatch):
    monkeypatch.setattr(
        meta_webhook.settings,
        "meta_app_secret",
        "",
    )
    monkeypatch.setattr(
        meta_webhook.settings,
        "environment",
        "production",
    )

    assert not meta_webhook._verify_meta_signature(
        raw_body=b"{}",
        signature_header=None,
    )


def test_modified_payload_invalidates_signature(monkeypatch):
    original_body = b'{"message":"hello"}'
    modified_body = b'{"message":"changed"}'
    secret = "test-meta-secret"

    monkeypatch.setattr(
        meta_webhook.settings,
        "meta_app_secret",
        secret,
    )
    monkeypatch.setattr(
        meta_webhook.settings,
        "environment",
        "production",
    )

    signature = _signature(secret, original_body)

    assert not meta_webhook._verify_meta_signature(
        raw_body=modified_body,
        signature_header=signature,
    )


def test_dev_without_app_secret_allows_local_request(monkeypatch):
    monkeypatch.setattr(
        meta_webhook.settings,
        "meta_app_secret",
        "",
    )
    monkeypatch.setattr(
        meta_webhook.settings,
        "environment",
        "dev",
    )

    assert meta_webhook._verify_meta_signature(
        raw_body=b"{}",
        signature_header=None,
    )
