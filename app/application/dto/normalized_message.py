from typing import Optional

from pydantic import BaseModel


class NormalizedMessage(BaseModel):
    platform: str
    sender_id: str
    recipient_id: str
    message_mid: str
    user_message: str = ""
    audio_url: Optional[str] = None
    timestamp: Optional[int] = None