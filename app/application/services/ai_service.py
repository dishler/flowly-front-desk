import logging
from typing import Any, Dict, List, Optional

from app.infrastructure.openai.client import OpenAIClient

logger = logging.getLogger(__name__)


class AIService:
    def __init__(self, openai_client: OpenAIClient) -> None:
        self.openai_client = openai_client

    def _normalize_history(self, history: Optional[List[Any]]) -> List[Dict[str, str]]:
        if not history:
            return []

        normalized_history: List[Dict[str, str]] = []

        for item in history:
            if isinstance(item, dict):
                role = str(item.get("role", "user")).strip() or "user"
                content = str(item.get("content", "")).strip()
                if content:
                    normalized_history.append(
                        {
                            "role": role,
                            "content": content,
                        }
                    )
                continue

            if isinstance(item, str):
                content = item.strip()
                if content:
                    normalized_history.append(
                        {
                            "role": "user",
                            "content": content,
                        }
                    )

        return normalized_history

    def _sanitize_grounding_context(
        self,
        grounding_context: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not grounding_context or not isinstance(grounding_context, dict):
            return None

        return grounding_context

    def _sanitize_system_instruction(
        self,
        system_instruction: Optional[str],
    ) -> Optional[str]:
        if not system_instruction:
            return None

        cleaned = system_instruction.strip()
        return cleaned or None

    def try_generate_reply(
        self,
        user_message: str,
        history: Optional[List[Any]] = None,
        grounding_context: Optional[Dict[str, Any]] = None,
        system_instruction: Optional[str] = None,
    ) -> Dict[str, Any]:
        cleaned_user_message = user_message.strip()
        if not cleaned_user_message:
            return {"reply_text": None}

        normalized_history = self._normalize_history(history)
        safe_grounding_context = self._sanitize_grounding_context(grounding_context)
        safe_system_instruction = self._sanitize_system_instruction(system_instruction)

        try:
            result = self.openai_client.generate_reply(
                user_message=cleaned_user_message,
                history=normalized_history,
                grounding_context=safe_grounding_context,
                system_instruction=safe_system_instruction,
            )
            logger.debug(
                "AIService try_generate_reply result: used_ai=%s reason=%s has_reply_text=%s",
                result.get("used_ai"),
                result.get("reason"),
                bool(result.get("reply_text")),
            )
            return result
        except TypeError:
            # Backward compatibility with an older OpenAI client signature
            # that only accepts user_message and history.
            try:
                result = self.openai_client.generate_reply(
                    user_message=cleaned_user_message,
                    history=normalized_history,
                )
                logger.debug(
                    "AIService fallback signature result: used_ai=%s reason=%s has_reply_text=%s",
                    result.get("used_ai"),
                    result.get("reason"),
                    bool(result.get("reply_text")),
                )
                return result
            except Exception:
                logger.exception("AIService fallback signature call failed")
                return {"reply_text": None}
        except Exception:
            logger.exception("AIService try_generate_reply failed")
            return {"reply_text": None}
            