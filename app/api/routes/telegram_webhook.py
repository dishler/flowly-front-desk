from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder

from app.core.config import get_settings
from app.infrastructure.telegram.parser import TelegramPayloadParser

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


def _verify_telegram_webhook_secret(
    secret_header: Optional[str],
) -> bool:
    webhook_secret = settings.telegram_webhook_secret.strip()
    environment = settings.environment.strip().lower()

    if not webhook_secret:
        return environment != "production"

    if not secret_header:
        return False

    return hmac.compare_digest(
        webhook_secret,
        secret_header.strip(),
    )


@router.post("/telegram")
async def receive_telegram_webhook(request: Request) -> dict[str, Any]:
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not _verify_telegram_webhook_secret(secret_header=secret_header):
        logger.warning("Rejected Telegram webhook with invalid secret")
        raise HTTPException(
            status_code=403,
            detail="Invalid webhook secret",
        )

    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except Exception as exc:
        logger.exception("Invalid Telegram webhook JSON payload")
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON payload",
        ) from exc

    logger.info("telegram_webhook_received")

    message = TelegramPayloadParser.parse(payload)
    if message is None:
        return {
            "status": "ignored",
            "reason": "unsupported_update",
        }

    message_processor = getattr(request.app.state, "message_processor", None)
    if message_processor is None:
        raise HTTPException(status_code=500, detail="message_processor is not configured")

    logger.info("Entering message_processor for telegram sender_id=%s", message.sender_id)
    try:
        result = await message_processor.process(message)
    except Exception as exc:
        logger.exception(
            "message_processor.process failed for telegram sender_id=%s",
            message.sender_id,
        )
        raise HTTPException(
            status_code=500,
            detail="message_processing_failed",
        ) from exc
    logger.info("message_processor.process completed for telegram sender_id=%s", message.sender_id)

    return {
        "status": "ok",
        "normalized_message": message.model_dump(),
        "result": jsonable_encoder(result),
    }
