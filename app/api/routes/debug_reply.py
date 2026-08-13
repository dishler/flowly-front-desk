from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.application.dto.normalized_message import NormalizedMessage

router = APIRouter(tags=["debug-reply"])


class DebugReplyRequest(BaseModel):
    sender_id: str
    message_text: str


@router.post("/debug/reply")
async def debug_reply(payload: DebugReplyRequest, request: Request):
    normalized_message = NormalizedMessage(
        platform="debug",
        sender_id=payload.sender_id,
        recipient_id="debug-local",
        message_mid="",
        user_message=payload.message_text,
    )
    message_processor = getattr(request.app.state, "message_processor", None)
    if message_processor is None:
        return {"status": "error", "reason": "message_processor_not_configured"}

    result = await message_processor.process(normalized_message)
    return {
        "status": "ok",
        "normalized_message": normalized_message.model_dump(),
        "result": result,
        "reply_text": result.get("reply_text", ""),
    }
