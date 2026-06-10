"""Pingram Email platform adapter."""

import asyncio
import base64
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

from pingram_gateway.core.config import load_email_allowlist, load_shared_config
from pingram_gateway.core.constants import DEFAULT_FROM_NAME, DOWNLOAD_TIMEOUT, PLATFORM_EMAIL
from pingram_gateway.core.helpers import (
    cfg_value,
    email_chat_id,
    ensure_runtime_deps,
    ext_for_content_type,
    html_to_text,
    is_deliverable_email,
    is_image,
    is_routing_or_message_id_address,
    norm_email,
    normalize_email_chat_id,
    parse_sender_email,
    recipient_from_email_chat_id,
    redact_user,
    text_to_html,
)
from pingram_gateway.core.directory import seed_platform_directory
from pingram_gateway.core.poll import PingramPollCoordinator
from pingram_gateway.core.send import pingram_send
from pingram_gateway.email.subject import resolve_outbound_subject

logger = logging.getLogger(__name__)


class PingramEmailAdapter(BasePlatformAdapter):
    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM_EMAIL))
        self.shared = load_shared_config(config)
        self.from_email = norm_email(cfg_value(config, "PINGRAM_FROM_EMAIL", "from_email", ""))
        self.from_name = str(cfg_value(config, "PINGRAM_FROM_NAME", "from_name", DEFAULT_FROM_NAME)).strip() or DEFAULT_FROM_NAME
        self._allowed = load_email_allowlist(config)
        self._reply_ctx: Dict[str, Dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "Pingram Email"

    async def connect(self) -> bool:
        if not self.shared.api_key:
            self._set_fatal_error("config_missing", "PINGRAM_API_KEY must be set", retryable=False)
            return False
        if not self._allowed:
            self._set_fatal_error(
                "config_missing",
                "PINGRAM_EMAIL_ALLOWED_USERS or PINGRAM_EMAIL_HOME_CHANNEL must be set",
                retryable=False,
            )
            return False
        if not await ensure_runtime_deps():
            self._set_fatal_error("dependency_missing", "Pingram SDK or aiohttp not installed", retryable=False)
            return False
        await PingramPollCoordinator.instance().attach_email(self, self.shared)
        self._mark_connected()
        seed_platform_directory(PLATFORM_EMAIL, self.config)
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        await PingramPollCoordinator.instance().detach_email()

    def is_sender_allowed(self, sender: Any) -> bool:
        if self.shared.allow_all:
            return True
        parsed = parse_sender_email(sender)
        if parsed and parsed in self._allowed:
            return True
        if norm_email(sender) in self._allowed:
            return True
        if is_routing_or_message_id_address(str(sender)) and self._allowed:
            return True
        return False

    def _resolve_inbound_sender(self, raw_from: Any) -> str:
        parsed = parse_sender_email(raw_from)
        if parsed and (self.shared.allow_all or parsed in self._allowed):
            return parsed
        if is_routing_or_message_id_address(str(raw_from)) and self._allowed:
            if len(self._allowed) == 1:
                return next(iter(self._allowed))
        if len(self._allowed) == 1:
            return next(iter(self._allowed))
        return ""

    async def process_inbound(self, payload: dict) -> None:
        try:
            await self._dispatch_email(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pingram Email: error processing inbound message")

    async def _dispatch_email(self, payload: dict) -> None:
        if not self._message_handler:
            return
        sender = self._resolve_inbound_sender(payload.get("from", ""))
        if not sender:
            logger.warning("Pingram Email: ignoring undeliverable sender %s", redact_user(payload.get("from")))
            return
        subject = payload.get("subject") or ""
        thread_key = self._email_thread_key(payload)
        chat_id = email_chat_id(sender, thread_key)
        text = payload.get("bodyText") or html_to_text(payload.get("bodyHtml") or "")
        self._reply_ctx[chat_id] = {
            "to_email": sender,
            "subject": subject,
            "message_id": payload.get("messageId"),
            "references": payload.get("references"),
        }
        source = self.build_source(
            chat_id=chat_id,
            chat_name=subject or redact_user(sender),
            chat_type="dm",
            user_id=sender,
            user_name=payload.get("fromName") or redact_user(sender),
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(payload.get("messageId") or payload.get("trackingId") or int(time.time() * 1000)),
            timestamp=datetime.datetime.now(),
        )
        await self.handle_message(event)

    @staticmethod
    def _email_thread_key(payload: dict) -> str:
        references = payload.get("references")
        if references:
            if isinstance(references, (list, tuple)):
                refs = [str(r).strip() for r in references if str(r).strip()]
                if refs:
                    return refs[0]
            else:
                refs = str(references).split()
                if refs:
                    return refs[0].strip()
        for key in ("inReplyTo", "messageId"):
            value = payload.get(key)
            if value:
                return str(value).strip()
        return ""

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        normalized = normalize_email_chat_id(chat_id) or chat_id
        explicit_subject = (metadata or {}).get("subject")
        return await self._send_email(normalized, content, explicit_subject=explicit_subject)

    async def _send_email(
        self,
        chat_id: str,
        content: str,
        *,
        attachments: Optional[List[dict]] = None,
        explicit_subject: Optional[str] = None,
    ) -> SendResult:
        ctx = self._reply_ctx.get(chat_id)
        to_email = (ctx or {}).get("to_email") or recipient_from_email_chat_id(chat_id)
        if not to_email:
            home = os.getenv("PINGRAM_EMAIL_HOME_CHANNEL", "").strip()
            if home and is_deliverable_email(home.split("#", 1)[0]):
                to_email = recipient_from_email_chat_id(home) or home.split("#", 1)[0]
        if not to_email:
            return SendResult(success=False, error="No recipient email for thread")
        subject, body = resolve_outbound_subject(content, ctx, explicit_subject=explicit_subject)
        email_block: Dict[str, Any] = {"subject": subject, "html": text_to_html(body), "senderName": self.from_name}
        if self.from_email:
            email_block["senderEmail"] = self.from_email
        body: Dict[str, Any] = {
            "type": self.shared.notification_type,
            "to": {"id": to_email, "email": to_email},
            "email": email_block,
        }
        if attachments:
            body["options"] = {"email": {"attachments": attachments}}
        return await pingram_send(self.shared.api_key, self.shared.region, body)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm", "channel": "email"}

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult:
        return await self._send_media(chat_id, image_url, caption, is_url=True)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, image_path, caption, is_url=False)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, file_path, caption, is_url=False, file_name=file_name)

    async def _send_media(self, chat_id, source, caption, *, is_url: bool, file_name: Optional[str] = None) -> SendResult:
        normalized = normalize_email_chat_id(chat_id) or chat_id
        attachment = await self._build_email_attachment(source, is_url=is_url, file_name=file_name)
        if attachment is None:
            return await self._send_email(normalized, caption or "(attachment unavailable)")
        return await self._send_email(normalized, caption or "", attachments=[attachment])

    async def _build_email_attachment(self, source: str, *, is_url: bool, file_name: Optional[str]) -> Optional[dict]:
        try:
            if is_url:
                data, content_type = await self._download(source)
                if not data:
                    return None
                filename = file_name or source.split("/")[-1].split("?")[0] or f"file{ext_for_content_type(content_type)}"
            else:
                with open(source, "rb") as fh:
                    data = fh.read()
                content_type = ""
                filename = file_name or os.path.basename(source) or "file"
            return {
                "filename": filename,
                "content": base64.b64encode(data).decode("ascii"),
                "contentType": content_type or "application/octet-stream",
            }
        except Exception:
            logger.debug("Pingram Email: failed to build attachment", exc_info=True)
            return None

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
            return None, ""


async def standalone_send_email(pconfig, chat_id: str, message: str, *, thread_id=None, media_files=None, force_document=False):
    from pingram_gateway.core.helpers import ensure_importable
    from pingram_gateway.core.constants import AIOHTTP_IMPORT, AIOHTTP_PACKAGE, PINGRAM_IMPORT, PINGRAM_PACKAGE

    if not (os.getenv("PINGRAM_API_KEY") or (getattr(pconfig, "extra", {}) or {}).get("api_key")):
        return {"error": "Pingram Email: PINGRAM_API_KEY not configured"}
    if not await asyncio.to_thread(ensure_importable, AIOHTTP_IMPORT, AIOHTTP_PACKAGE):
        return {"error": "Pingram Email: aiohttp not installed"}
    if not await asyncio.to_thread(ensure_importable, PINGRAM_IMPORT, PINGRAM_PACKAGE):
        return {"error": "Pingram Email: pingram-python not installed"}

    resolved = normalize_email_chat_id(chat_id)
    if not resolved:
        home = os.getenv("PINGRAM_EMAIL_HOME_CHANNEL", "").strip()
        resolved = normalize_email_chat_id(home) or home
    if not resolved or "@" not in resolved.split("#", 1)[0]:
        return {"error": "Pingram Email: no recipient. Use target 'pingram-email' or set PINGRAM_EMAIL_HOME_CHANNEL."}

    adapter = PingramEmailAdapter(pconfig)
    if media_files and not (message or "").strip():
        media_path, _ = media_files[0]
        result = await adapter.send_document(resolved, media_path)
    else:
        result = await adapter.send(resolved, message or "")
    if result.success:
        return {"success": True, "platform": PLATFORM_EMAIL, "chat_id": resolved, "message_id": result.message_id}
    return {"error": result.error or "Pingram Email send failed"}
