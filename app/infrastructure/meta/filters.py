from app.application.dto.normalized_message import NormalizedMessage


def is_meaningful_message(message: NormalizedMessage) -> bool:
    text = message.user_message.strip()
    return bool(text)
    