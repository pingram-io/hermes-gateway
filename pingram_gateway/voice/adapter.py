"""Pingram Voice platform stub (alpha)."""

import logging
from typing import Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from pingram_gateway.core.constants import PLATFORM_VOICE, VOICE_ALPHA_MESSAGE

logger = logging.getLogger(__name__)


class PingramVoiceStubAdapter(BasePlatformAdapter):
    """Placeholder adapter — voice is not available yet."""

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM_VOICE))

    @property
    def name(self) -> str:
        return "Pingram Voice"

    async def connect(self) -> bool:
        logger.info("Pingram Voice: %s", VOICE_ALPHA_MESSAGE)
        self._set_fatal_error("not_available", VOICE_ALPHA_MESSAGE, retryable=False)
        return False

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=False, error=VOICE_ALPHA_MESSAGE)


async def standalone_send_voice(*args, **kwargs) -> Dict[str, Any]:
    return {"error": VOICE_ALPHA_MESSAGE}
