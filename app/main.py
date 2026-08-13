import os

import uvicorn
from fastapi import FastAPI

from app.api.routes.debug_booking import router as debug_booking_router
from app.api.routes.debug_reply import router as debug_reply_router
from app.api.routes.health import router as health_router
from app.api.routes.meta_webhook import router as meta_webhook_router
from app.application.services.ai_service import AIService
from app.application.services.booking_service import BookingService
from app.application.services.calendar_service import CalendarService
from app.application.services.dedup_service import DedupService
from app.application.services.front_desk_config_service import FrontDeskConfigService
from app.application.services.intent_service import IntentService
from app.application.services.knowledge_service import KnowledgeService
from app.application.services.language_service import LanguageService
from app.application.services.memory_service import MemoryService
from app.application.services.message_processor import MessageProcessor
from app.application.services.outbound_service import OutboundService
from app.application.services.redis_booking_state_service import RedisBookingStateService
from app.application.services.redis_dedup_service import RedisDedupService
from app.application.services.redis_memory_service import RedisMemoryService
from app.application.services.reply_service import ReplyService
from app.application.services.speech_service import SpeechService
from app.core.config import get_settings
from app.infrastructure.google.calendar_client import GoogleCalendarClient
from app.infrastructure.meta.client import MetaClient
from app.infrastructure.openai.client import OpenAIClient
from app.infrastructure.persistence.redis_client import RedisClientProvider

settings = get_settings()
settings.validate_production_settings()

app = FastAPI(title=settings.app_name)

redis_provider = RedisClientProvider()
redis_client = redis_provider.get_client()
front_desk_config_service = FrontDeskConfigService(
    file_path=settings.front_desk_config_path,
)

if redis_client is not None:
    memory_service = RedisMemoryService(redis_client=redis_client)
    dedup_service = RedisDedupService(redis_client=redis_client)
    booking_state_service = RedisBookingStateService(
        redis_client=redis_client,
        ttl_seconds=settings.redis_booking_confirmation_ttl_seconds,
        completed_ttl_seconds=settings.redis_completed_booking_ttl_seconds,
    )
else:
    if settings.redis_enabled:
        raise RuntimeError("Redis is enabled but no Redis client is available")
    memory_service = MemoryService()
    dedup_service = DedupService()
    booking_state_service = None

intent_service = IntentService()
language_service = LanguageService()
knowledge_service = KnowledgeService()

google_calendar_client = GoogleCalendarClient()
calendar_service = CalendarService(google_calendar_client=google_calendar_client)

booking_service = BookingService(
    calendar_service=calendar_service,
    language_service=language_service,
    booking_state_service=booking_state_service,
    front_desk_config_service=front_desk_config_service,
)

openai_client = OpenAIClient(
    front_desk_config_service=front_desk_config_service,
    knowledge_service=knowledge_service,
)
speech_service = SpeechService(openai_client=openai_client)
ai_service = AIService(openai_client=openai_client)

reply_service = ReplyService(
    ai_service=ai_service,
    memory_service=memory_service,
    knowledge_service=knowledge_service,
    front_desk_config_service=front_desk_config_service,
)

meta_client = MetaClient()
outbound_service = OutboundService(meta_client=meta_client)

message_processor = MessageProcessor(
    memory_service=memory_service,
    reply_service=reply_service,
    outbound_service=outbound_service,
    dedup_service=dedup_service,
    intent_service=intent_service,
    booking_service=booking_service,
    speech_service=speech_service,
)

app.state.redis_client = redis_client
app.state.front_desk_config_service = front_desk_config_service
app.state.memory_service = memory_service
app.state.dedup_service = dedup_service
app.state.booking_state_service = booking_state_service
app.state.intent_service = intent_service
app.state.language_service = language_service
app.state.knowledge_service = knowledge_service
app.state.google_calendar_client = google_calendar_client
app.state.calendar_service = calendar_service
app.state.booking_service = booking_service
app.state.openai_client = openai_client
app.state.speech_service = speech_service
app.state.ai_service = ai_service
app.state.reply_service = reply_service
app.state.meta_client = meta_client
app.state.outbound_service = outbound_service
app.state.message_processor = message_processor


@app.get("/")
async def root() -> dict:
    try:
        calendar_health = google_calendar_client.healthcheck()
    except Exception as exc:
        calendar_health = {
            "enabled": settings.google_calendar_enabled,
            "configured": False,
            "connected": False,
            "reason": str(exc),
        }

    return {
        "status": "ok",
        "service": "flowly-meta-bot",
        "environment": settings.environment,
        "redis_enabled": settings.redis_enabled,
        "redis_connected": redis_client is not None,
        "redis_booking_state_enabled": booking_state_service is not None,
        "google_calendar_enabled": calendar_health.get("enabled", False),
        "google_calendar_configured": calendar_health.get("configured", False),
        "google_calendar_connected": calendar_health.get("connected", False),
        "google_calendar_id": calendar_health.get("calendar_id", ""),
        "google_calendar_timezone": calendar_health.get("calendar_timezone", ""),
        "google_calendar_reason": calendar_health.get("reason", ""),
    }


app.include_router(health_router)
app.include_router(meta_webhook_router, prefix="/webhooks")
if settings.environment.strip().lower() != "production":
    app.include_router(debug_booking_router)
    app.include_router(debug_reply_router)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
