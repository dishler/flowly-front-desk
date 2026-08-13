from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class SpeechService:
    def __init__(self, openai_client) -> None:
        self.openai_client = openai_client

    async def transcribe_audio(self, file_url: str) -> str:
        file_path = await self._download_audio(file_url)

        try:
            result = await asyncio.to_thread(
                self.openai_client.transcribe_audio,
                file_path,
            )
            text = (result or {}).get("text") or ""
            return text.strip()
        finally:
            self._safe_delete(file_path)

    async def _download_audio(self, file_url: str) -> str:
        suffix = self._guess_suffix(file_url)

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(file_url)
            response.raise_for_status()

            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(response.content)
                return tmp.name

    def _guess_suffix(self, file_url: str) -> str:
        path = urlparse(file_url).path.lower()

        if path.endswith(".ogg"):
            return ".ogg"
        if path.endswith(".oga"):
            return ".ogg"
        if path.endswith(".mp3"):
            return ".mp3"
        if path.endswith(".wav"):
            return ".wav"
        if path.endswith(".m4a"):
            return ".m4a"
        if path.endswith(".mp4"):
            return ".mp4"
        if path.endswith(".mpeg"):
            return ".mpeg"
        if path.endswith(".webm"):
            return ".webm"

        return ".ogg"

    def _safe_delete(self, file_path: str) -> None:
        try:
            Path(file_path).unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to delete temporary audio file %s: %s", file_path, exc)