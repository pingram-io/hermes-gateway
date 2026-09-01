"""Pingram SMS platform adapter."""

import asyncio
import datetime
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
)

from pingram_gateway.core.config import load_shared_config, load_sms_allowlist, sms_inbound_ready
from pingram_gateway.core.constants import DOWNLOAD_TIMEOUT, PLATFORM_SMS
from pingram_gateway.core.helpers import (
    cfg_value,
    ensure_runtime_deps,
    ext_for_content_type,
    is_image,
    is_plausible_sms_number,
    norm_phone,
    normalize_phone_e164,
    normalize_sms_chat_id,
    redact_user,
)
from pingram_gateway.core.directory import seed_platform_directory
from pingram_gateway.core.poll import PingramPollCoordinator
from pingram_gateway.core.send import pingram_send

logger = logging.getLogger(__name__)


class PingramSmsAdapter(BasePlatformAdapter):
    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM_SMS))
        self.shared = load_shared_config(config)
        self.from_sms = normalize_phone_e164(cfg_value(config, "PINGRAM_FROM_SMS", "from_sms", ""))
        self._allowed = load_sms_allowlist(config)
        self._reply_ctx: Dict[str, Dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "Pingram SMS"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.shared.api_key:
            self._set_fatal_error("config_missing", "PINGRAM_API_KEY must be set", retryable=False)
            return False
        if not sms_inbound_ready(self.config):
            self._set_fatal_error(
                "config_missing",
                "Set PINGRAM_SMS_ALLOWED_USERS or PINGRAM_ALLOW_ALL_USERS=true",
                retryable=False,
            )
            return False
        if not await ensure_runtime_deps():
            self._set_fatal_error("dependency_missing", "Pingram SDK or aiohttp not installed", retryable=False)
            return False
        await PingramPollCoordinator.instance().attach_sms(self, self.shared, is_reconnect=is_reconnect)
        self._mark_connected()
        seed_platform_directory(PLATFORM_SMS, self.config)
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        await PingramPollCoordinator.instance().detach_sms()

    def is_sender_allowed(self, sender: Any) -> bool:
        if self.shared.allow_all:
            return True
        return norm_phone(normalize_phone_e164(sender)) in self._allowed

    async def process_inbound(self, payload: dict) -> None:
        try:
            await self._dispatch_sms(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pingram SMS: error processing inbound message")

    async def _dispatch_sms(self, payload: dict) -> None:
        if not self._message_handler:
            return
        sender = normalize_phone_e164(payload.get("from", ""))
        if not is_plausible_sms_number(sender):
            logger.warning("Pingram SMS: ignoring invalid sender %s", redact_user(payload.get("from")))
            return
        chat_id = sender
        text = payload.get("text") or ""
        media_urls, media_types = await self._collect_sms_media(payload)
        self._reply_ctx[chat_id] = {"to_number": sender}
        source = self.build_source(
            chat_id=chat_id,
            chat_name=redact_user(sender),
            chat_type="dm",
            user_id=sender,
            user_name=redact_user(sender),
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.PHOTO if media_urls else MessageType.TEXT,
            source=source,
            message_id=str(payload.get("trackingId") or int(time.time() * 1000)),
            timestamp=datetime.datetime.now(),
            media_urls=media_urls,
            media_types=media_types,
        )
        await self.handle_message(event)

    async def _collect_sms_media(self, payload: dict) -> Tuple[List[str], List[str]]:
        media_urls: List[str] = []
        media_types: List[str] = []
        for item in payload.get("media") or []:
            url = item.get("url") if isinstance(item, dict) else None
            if not url:
                continue
            content_type = (item.get("contentType") if isinstance(item, dict) else "") or ""
            data, fetched_ct = await self._download(url)
            if not data:
                continue
            content_type = content_type or fetched_ct
            try:
                if is_image(content_type):
                    path = cache_image_from_bytes(data, ext_for_content_type(content_type))
                else:
                    fname = url.split("/")[-1].split("?")[0] or f"attachment{ext_for_content_type(content_type)}"
                    path = cache_document_from_bytes(data, fname)
                media_urls.append(path)
                media_types.append(content_type or "application/octet-stream")
            except Exception:
                logger.debug("Pingram SMS: failed to cache media", exc_info=True)
        return media_urls, media_types

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        number = normalize_sms_chat_id(chat_id) or normalize_phone_e164(chat_id)
        if not is_plausible_sms_number(number):
            return SendResult(success=False, error=f"Invalid SMS recipient: {redact_user(number)}")
        return await self._send_sms(number, content)

    async def _send_sms(self, number: str, content: str, *, attachment_note: str = "") -> SendResult:
        number = normalize_phone_e164(number)
        message = (content or "").strip()
        if attachment_note:
            message = f"{message}\n\n{attachment_note}".strip()
        if not message:
            return SendResult(success=False, error="Empty SMS message")
        sms_block: Dict[str, Any] = {"message": message}
        if self.from_sms:
            sms_block["from"] = self.from_sms
        body = {
            "type": self.shared.notification_type,
            "to": {"id": number, "number": number},
            "sms": sms_block,
        }
        return await pingram_send(self.shared.api_key, self.shared.region, body)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm", "channel": "sms"}

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult:
        return await self._send_media(chat_id, image_url, caption, is_url=True)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, image_path, caption, is_url=False)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, file_path, caption, is_url=False, file_name=file_name)

    async def _send_media(self, chat_id, source, caption, *, is_url: bool, file_name: Optional[str] = None) -> SendResult:
        number = normalize_sms_chat_id(chat_id) or normalize_phone_e164(chat_id)
        if is_url and source.lower().startswith(("http://", "https://")):
            note = f"Attachment: {source}"
        else:
            note = "(An attachment was generated but can't be sent over SMS.)"
        return await self._send_sms(number, caption or "", attachment_note=note)

    async def _download(self, url: str) -> Tuple[Optional[bytes], str]:
        try:
            import aiohttp
        except ImportError:
            return None, ""
        try:
            timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None, ""
                    return await resp.read(), resp.headers.get("Content-Type", "")
        except Exception:
            logger.debug("Pingram SMS: download failed for %s", url, exc_info=True)
            return None, ""


async def standalone_send_sms(pconfig, chat_id: str, message: str, *, thread_id=None, media_files=None, force_document=False):
    from pingram_gateway.core.helpers import ensure_importable
    from pingram_gateway.core.constants import AIOHTTP_IMPORT, AIOHTTP_PACKAGE, PINGRAM_IMPORT, PINGRAM_PACKAGE

    if not (os.getenv("PINGRAM_API_KEY") or (getattr(pconfig, "extra", {}) or {}).get("api_key")):
        return {"error": "Pingram SMS: PINGRAM_API_KEY not configured"}
    if not await asyncio.to_thread(ensure_importable, AIOHTTP_IMPORT, AIOHTTP_PACKAGE):
        return {"error": "Pingram SMS: aiohttp not installed"}
    if not await asyncio.to_thread(ensure_importable, PINGRAM_IMPORT, PINGRAM_PACKAGE):
        return {"error": "Pingram SMS: pingram-python not installed"}

    resolved = normalize_sms_chat_id(chat_id)
    if not resolved:
        home = os.getenv("PINGRAM_SMS_HOME_CHANNEL", "").strip()
        resolved = normalize_sms_chat_id(home)
    if not resolved:
        return {"error": "Pingram SMS: no recipient. Use target 'pingram-sms' or set PINGRAM_SMS_HOME_CHANNEL."}

    adapter = PingramSmsAdapter(pconfig)
    if media_files and not (message or "").strip():
        media_path, _ = media_files[0]
        result = await adapter.send_image_file(resolved, media_path)
    else:
        result = await adapter.send(resolved, message or "")
    if result.success:
        return {"success": True, "platform": PLATFORM_SMS, "chat_id": resolved, "message_id": result.message_id}
    return {"error": result.error or "Pingram SMS send failed"}
