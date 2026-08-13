from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class OpenAIClient:
    def __init__(
        self,
        front_desk_config_service: Any | None = None,
        knowledge_service: Any | None = None,
    ) -> None:
        self.settings = get_settings()
        self.client = OpenAI(api_key=self.settings.openai_api_key) if self.settings.openai_api_key else None
        self.front_desk_config_service = front_desk_config_service
        self.knowledge_service = knowledge_service

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

    def _build_default_system_prompt(self) -> str:
        business_name = "the business"
        supported_languages = ["uk", "en"]
        tone = "calm, concise, helpful front desk receptionist"
        safety_rules: list[str] = []
        handoff_rules: list[str] = []

        if self.front_desk_config_service is not None:
            business = self.front_desk_config_service.get_business()
            assistant = self.front_desk_config_service.get_assistant()
            safety = self.front_desk_config_service.get_safety()
            handoff = self.front_desk_config_service.get_handoff()

            business_name = str(business.get("name") or business_name)
            supported_languages = assistant.get("supported_languages") or supported_languages
            tone = str(assistant.get("tone") or tone)
            safety_rules = [str(item) for item in safety.get("do_not_claim", [])]
            handoff_rules = [str(item) for item in handoff.get("rules", [])]

        lines = [
            f"You are the front desk assistant for {business_name}.",
            f"Tone: {tone}.",
            "Supported languages: " + ", ".join(str(lang) for lang in supported_languages) + ".",
            "",
            "Strict rules:",
            "- Use the knowledge base as the only factual grounding source.",
            "- If a detail is missing from the knowledge base, say you need to check or ask one concise clarifying question.",
            "- Do not invent services, prices, policies, availability, guarantees, locations, or contact details.",
            "- Do not say an appointment is confirmed unless the booking flow actually confirms it.",
            "- Keep replies short, natural, and practical.",
            "- Reply in the user's language when it is supported.",
            "- If the request requires a human, hand it off instead of guessing.",
        ]

        if safety_rules:
            lines.append("")
            lines.append("Safety rules:")
            lines.extend(f"- {rule}" for rule in safety_rules)

        if handoff_rules:
            lines.append("")
            lines.append("Handoff triggers:")
            lines.extend(f"- {rule}" for rule in handoff_rules)

        return "\n".join(lines)

    def _build_messages(
        self,
        user_message: str,
        history: List[Dict[str, str]],
        grounding_context: Optional[Dict[str, Any]] = None,
        system_instruction: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []

        base_system_prompt = self._build_default_system_prompt()
        if system_instruction:
            system_prompt = f"{base_system_prompt}\nAdditional instructions:\n{system_instruction.strip()}"
        else:
            system_prompt = base_system_prompt

        messages.append(
            {
                "role": "system",
                "content": system_prompt,
            }
        )

        if grounding_context:
            grounding_json = json.dumps(grounding_context, ensure_ascii=False, indent=2)
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Use the following business knowledge context as the only factual grounding source. "
                        "If some detail is not present here, do not invent it.\n\n"
                        f"{grounding_json}"
                    ),
                }
            )

        trimmed_history = history[-10:] if history else []
        for item in trimmed_history:
            role = item.get("role", "user")
            content = item.get("content", "").strip()
            if content:
                messages.append(
                    {
                        "role": role,
                        "content": content,
                    }
                )

        messages.append(
            {
                "role": "user",
                "content": user_message.strip(),
            }
        )

        return messages

    def _messages_to_responses_input(self, messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        response_input: List[Dict[str, Any]] = []

        for message in messages:
            role = message["role"]
            content = message["content"]
            response_input.append(
                {
                    "role": role,
                    "content": [
                        {
                            "type": "input_text",
                            "text": content,
                        }
                    ],
                }
            )

        return response_input

    def _extract_reply_text(self, response: Any) -> Optional[str]:
        direct_output_text = getattr(response, "output_text", None)
        if isinstance(direct_output_text, str) and direct_output_text.strip():
            return direct_output_text.strip()

        output_items = getattr(response, "output", None)
        if not output_items:
            return None

        for item in output_items:
            content_items = getattr(item, "content", None) or []
            for content_item in content_items:
                text_value = getattr(content_item, "text", None)
                if isinstance(text_value, str) and text_value.strip():
                    return text_value.strip()

        return None

    def generate_reply(
        self,
        user_message: str,
        history: Optional[List[Any]] = None,
        grounding_context: Optional[Dict[str, Any]] = None,
        system_instruction: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.settings.openai_enabled:
            logger.debug("OpenAI generate_reply fallback: OPENAI_ENABLED is false")
            return {
                "used_ai": False,
                "stub": True,
                "reason": "OPENAI_ENABLED=false",
                "reply_text": None,
            }

        if not self.settings.openai_api_key or self.client is None:
            logger.debug("OpenAI generate_reply fallback: missing OPENAI_API_KEY/client")
            return {
                "used_ai": False,
                "stub": True,
                "reason": "Missing OPENAI_API_KEY",
                "reply_text": None,
            }

        cleaned_user_message = user_message.strip()
        if not cleaned_user_message:
            logger.debug("OpenAI generate_reply fallback: empty user message")
            return {
                "used_ai": False,
                "stub": False,
                "reason": "Empty user message",
                "reply_text": None,
            }

        normalized_history = self._normalize_history(history)

        messages = self._build_messages(
            user_message=cleaned_user_message,
            history=normalized_history,
            grounding_context=grounding_context,
            system_instruction=system_instruction,
        )

        try:
            logger.debug(
                "OpenAI generate_reply request: model=%s history_count=%s has_grounding=%s has_system_instruction=%s",
                self.settings.openai_model,
                len(normalized_history),
                bool(grounding_context),
                bool(system_instruction),
            )

            response = self.client.responses.create(
                model=self.settings.openai_model,
                input=self._messages_to_responses_input(messages),
            )

            reply_text = self._extract_reply_text(response)

            if not reply_text:
                logger.debug("OpenAI generate_reply fallback: empty response text")
                return {
                    "used_ai": False,
                    "stub": False,
                    "reason": "Empty OpenAI response",
                    "reply_text": None,
                }

            cleaned_reply = reply_text.strip()
            if not cleaned_reply:
                logger.debug("OpenAI generate_reply fallback: blank response text")
                return {
                    "used_ai": False,
                    "stub": False,
                    "reason": "Blank OpenAI response",
                    "reply_text": None,
                }

            logger.debug("OpenAI generate_reply success: reply_text extracted")
            return {
                "used_ai": True,
                "stub": False,
                "reason": None,
                "reply_text": cleaned_reply,
            }

        except Exception as exc:
            logger.exception("OpenAI generate_reply exception: %s", exc)
            return {
                "used_ai": False,
                "stub": False,
                "reason": f"OpenAI error: {exc}",
                "reply_text": None,
            }

    def transcribe_audio(self, file_path: str) -> Dict[str, Any]:
        if not self.settings.openai_enabled:
            logger.debug("OpenAI transcribe_audio fallback: OPENAI_ENABLED is false")
            return {
                "used_ai": False,
                "stub": True,
                "reason": "OPENAI_ENABLED=false",
                "text": None,
            }

        if not self.settings.openai_api_key or self.client is None:
            logger.debug("OpenAI transcribe_audio fallback: missing OPENAI_API_KEY/client")
            return {
                "used_ai": False,
                "stub": True,
                "reason": "Missing OPENAI_API_KEY",
                "text": None,
            }

        path = Path(file_path)
        if not path.exists() or not path.is_file():
            logger.debug("OpenAI transcribe_audio fallback: file does not exist")
            return {
                "used_ai": False,
                "stub": False,
                "reason": f"Audio file not found: {file_path}",
                "text": None,
            }

        try:
            logger.debug("OpenAI transcribe_audio request: file_path=%s", file_path)

            with path.open("rb") as audio_file:
                response = self.client.audio.transcriptions.create(
                    model="gpt-4o-mini-transcribe",
                    file=audio_file,
                )

            transcript_text = getattr(response, "text", None)
            cleaned_text = transcript_text.strip() if isinstance(transcript_text, str) else ""

            if not cleaned_text:
                logger.debug("OpenAI transcribe_audio fallback: empty transcript")
                return {
                    "used_ai": False,
                    "stub": False,
                    "reason": "Empty transcript",
                    "text": None,
                }

            logger.debug("OpenAI transcribe_audio success")
            return {
                "used_ai": True,
                "stub": False,
                "reason": None,
                "text": cleaned_text,
            }

        except Exception as exc:
            logger.exception("OpenAI transcribe_audio exception: %s", exc)
            return {
                "used_ai": False,
                "stub": False,
                "reason": f"OpenAI transcription error: {exc}",
                "text": None,
            }
