"""Pingram Voice Agent adapter — outbound conversational calls via POST /voice/call."""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from pingram_gateway.core.config import load_shared_config, load_voice_allowlist, voice_agent_id, voice_configured
from pingram_gateway.core.constants import PLATFORM_VOICE
from pingram_gateway.core.directory import seed_platform_directory
from pingram_gateway.core.helpers import (
    ensure_runtime_deps,
    html_to_text,
    is_plausible_sms_number,
    norm_phone,
    normalize_phone_e164,
    normalize_voice_chat_id,
    redact_user,
)
from pingram_gateway.core.send import pingram_place_voice_call

logger = logging.getLogger(__name__)


class PingramVoiceAdapter(BasePlatformAdapter):
    """Places outbound Pingram Voice Agent calls (two-way AI on the phone).

    Hermes starts the call with a briefing; Pingram hosts the live conversation.
    This is not the one-way CALL notification channel (`send({ call })`).
    """

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM_VOICE))
        self.shared = load_shared_config(config)
        self._allowed = load_voice_allowlist(config)
        self._agent_id = voice_agent_id(config)

    @property
    def name(self) -> str:
        return "Pingram Voice"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        if not self.shared.api_key:
            self._set_fatal_error("config_missing", "PINGRAM_API_KEY must be set", retryable=False)
            return False
        if not voice_configured(self.config):
            self._set_fatal_error(
                "config_missing",
                "Set PINGRAM_VOICE_HOME_CHANNEL or PINGRAM_VOICE_ALLOWED_USERS",
                retryable=False,
            )
            return False
        if not await ensure_runtime_deps():
            self._set_fatal_error("dependency_missing", "Pingram SDK or aiohttp not installed", retryable=False)
            return False
        self._mark_connected()
        seed_platform_directory(PLATFORM_VOICE, self.config)
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    def is_sender_allowed(self, sender: Any) -> bool:
        if self.shared.allow_all:
            return True
        return norm_phone(normalize_phone_e164(sender)) in self._allowed

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        number = normalize_voice_chat_id(chat_id) or normalize_phone_e164(chat_id)
        if not is_plausible_sms_number(number):
            return SendResult(success=False, error=f"Invalid voice recipient: {redact_user(number)}")
        briefing = _briefing_text(content)
        if not briefing:
            return SendResult(success=False, error="Empty voice briefing")
        return await pingram_place_voice_call(
            self.shared.api_key,
            self.shared.region,
            number,
            briefing,
            agent_id=self._agent_id or None,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm", "channel": "voice"}

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult:
        return await self.send(
            chat_id,
            caption or "Mention that you have an image to share, but this is a voice call so describe it instead.",
        )

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self.send(
            chat_id,
            caption or "Mention that you have an image to share, but this is a voice call so describe it instead.",
        )

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self.send(
            chat_id,
            caption or "Mention that you have a file to share, but this is a voice call so summarize it instead.",
        )


def _briefing_text(content: str) -> str:
    raw = (content or "").strip()
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        raw = html_to_text(raw)
    return raw.strip()


async def standalone_send_voice(pconfig, chat_id: str, message: str, *, thread_id=None, media_files=None, force_document=False):
    from pingram_gateway.core.helpers import ensure_importable
    from pingram_gateway.core.constants import AIOHTTP_IMPORT, AIOHTTP_PACKAGE, PINGRAM_IMPORT, PINGRAM_PACKAGE

    if not (os.getenv("PINGRAM_API_KEY") or (getattr(pconfig, "extra", {}) or {}).get("api_key")):
        return {"error": "Pingram Voice: PINGRAM_API_KEY not configured"}
    if not await asyncio.to_thread(ensure_importable, AIOHTTP_IMPORT, AIOHTTP_PACKAGE):
        return {"error": "Pingram Voice: aiohttp not installed"}
    if not await asyncio.to_thread(ensure_importable, PINGRAM_IMPORT, PINGRAM_PACKAGE):
        return {"error": "Pingram Voice: pingram-python not installed"}

    resolved = normalize_voice_chat_id(chat_id)
    if not resolved:
        home = os.getenv("PINGRAM_VOICE_HOME_CHANNEL", "").strip()
        resolved = normalize_voice_chat_id(home)
    if not resolved:
        return {"error": "Pingram Voice: no recipient. Use target 'pingram-voice' or set PINGRAM_VOICE_HOME_CHANNEL."}

    adapter = PingramVoiceAdapter(pconfig)
    briefing = message or ""
    if media_files and not briefing.strip():
        briefing = "Mention that a file was attached in Hermes, but this is a voice call so summarize it instead."
    result = await adapter.send(resolved, briefing)
    if result.success:
        return {"success": True, "platform": PLATFORM_VOICE, "chat_id": resolved, "message_id": result.message_id}
    return {"error": result.error or "Pingram Voice send failed"}
