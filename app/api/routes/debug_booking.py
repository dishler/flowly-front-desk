from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["debug-booking"])


class DebugBookingRequest(BaseModel):
    sender_id: str
    message_text: str


@router.post("/debug/booking/request")
async def debug_booking_request(payload: DebugBookingRequest, request: Request):
    result = request.app.state.booking_service.handle_booking_request(
        sender_id=payload.sender_id,
        message_text=payload.message_text,
    )
    return result


@router.post("/debug/booking/confirm")
async def debug_booking_confirm(payload: DebugBookingRequest, request: Request):
    result = request.app.state.booking_service.handle_booking_confirmation(
        sender_id=payload.sender_id,
        message_text=payload.message_text,
    )
    return result