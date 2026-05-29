"""
Pingram Platform Adapter for Hermes Agent.

A single Hermes *platform plugin* that lets users chat with their Hermes agent
over Pingram-managed **SMS** and **Email**.  One combined ``pingram`` platform
serves both channels (Pingram uses one API key and one webhook URL for both);
the channel is encoded in the Hermes ``chat_id`` prefix (``sms:`` / ``email:``).

Flow::

    Human --SMS/Email--> Pingram --POST webhook--> aiohttp server (this adapter)
                                                       |
                                              MessageEvent -> Hermes agent
                                                       |
    Human <--SMS/Email-- Pingram <-- Pingram SDK <-- PingramAdapter.send()

Configuration (env vars override config.yaml ``extra``):
    PINGRAM_API_KEY            (required) pingram_sk_...
    PINGRAM_REGION             us | eu | ca (default: us)
    PINGRAM_FROM_SMS           sender phone (E.164); enables the SMS channel
    PINGRAM_FROM_EMAIL         sender email; enables the Email channel
    PINGRAM_CHANNELS           csv filter (sms,email); default: inferred
    PINGRAM_WEBHOOK_HOST       default 0.0.0.0
    PINGRAM_WEBHOOK_PORT       default 8650
    PINGRAM_WEBHOOK_PATH       default /webhooks/pingram
    PINGRAM_WEBHOOK_SECRET     pingram_whsecret_... (optional -> secured mode)
    PINGRAM_WEBHOOK_TOLERANCE  signature timestamp tolerance, seconds (default 300)
    PINGRAM_ALLOWED_USERS      csv of phones/emails allowed to talk to the agent
    PINGRAM_ALLOW_ALL_USERS    true to allow everyone (dev only; default false)
    PINGRAM_NOTIFICATION_TYPE  Pingram notification `type` for replies
                               (default: hermes_agent_reply)
"""

import asyncio
import base64
import datetime
import hashlib
import html as html_lib
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
    cache_image_from_bytes,
    cache_document_from_bytes,
)
from gateway.config import Platform

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8650
DEFAULT_WEBHOOK_PATH = "/webhooks/pingram"
DEFAULT_NOTIFICATION_TYPE = "hermes_agent_reply"
DEFAULT_TOLERANCE = 300
_DEDUP_TTL_SECONDS = 3600
_DOWNLOAD_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _norm_phone(value: Any) -> str:
    """Reduce a phone number to comparable digits."""
    return re.sub(r"\D", "", str(value or ""))


def _norm_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _redact_user(value: Any) -> str:
    """Redact a phone/email for safe logging."""
    s = str(value or "")
    if "@" in s:
        local, _, domain = s.partition("@")
        head = local[:2]
        return f"{head}***@{domain}"
    digits = _norm_phone(s)
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return "***"


_CT_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
}


def _ext_for_content_type(content_type: str) -> str:
    return _CT_EXT.get((content_type or "").split(";")[0].strip().lower(), ".bin")


def _is_image(content_type: str) -> bool:
    return (content_type or "").split("/", 1)[0].strip().lower() == "image"


def _text_to_html(text: str) -> str:
    """Render a plain-text agent reply as minimal, safe email HTML."""
    escaped = html_lib.escape(text or "")
    body = escaped.replace("\n", "<br>")
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,'
        f'Arial,sans-serif;white-space:normal;">{body}</div>'
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class PingramAdapter(BasePlatformAdapter):
    """Async Pingram adapter implementing the BasePlatformAdapter interface."""

    def __init__(self, config, **kwargs):
        platform = Platform("pingram")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        def cfg(env_key: str, extra_key: str, default: Any = "") -> Any:
            env_val = os.getenv(env_key)
            if env_val is not None and env_val != "":
                return env_val
            return extra.get(extra_key, default)

        self.api_key: str = str(cfg("PINGRAM_API_KEY", "api_key", "")).strip()
        self.region: str = str(cfg("PINGRAM_REGION", "region", "us")).strip().lower() or "us"

        self.from_sms: str = str(cfg("PINGRAM_FROM_SMS", "from_sms", "")).strip()
        self.from_email: str = _norm_email(cfg("PINGRAM_FROM_EMAIL", "from_email", ""))
        self._from_sms_norm = _norm_phone(self.from_sms)

        # Which channels are active = (requested filter) ∩ (have a sender for it)
        available: set = set()
        if self.from_sms:
            available.add("sms")
        if self.from_email:
            available.add("email")
        requested = set(_parse_csv(cfg("PINGRAM_CHANNELS", "channels", "")))
        self.channels: set = (requested & available) if requested else available

        self.webhook_host: str = str(cfg("PINGRAM_WEBHOOK_HOST", "webhook_host", "0.0.0.0")).strip()
        try:
            self.webhook_port: int = int(cfg("PINGRAM_WEBHOOK_PORT", "webhook_port", DEFAULT_PORT))
        except (TypeError, ValueError):
            self.webhook_port = DEFAULT_PORT

        path = str(cfg("PINGRAM_WEBHOOK_PATH", "webhook_path", DEFAULT_WEBHOOK_PATH)).strip() or DEFAULT_WEBHOOK_PATH
        if not path.startswith("/"):
            path = "/" + path
        self.webhook_path: str = path.rstrip("/") or DEFAULT_WEBHOOK_PATH

        self.webhook_secret: str = str(cfg("PINGRAM_WEBHOOK_SECRET", "webhook_secret", "")).strip()
        try:
            self.webhook_tolerance: int = int(cfg("PINGRAM_WEBHOOK_TOLERANCE", "webhook_tolerance", DEFAULT_TOLERANCE))
        except (TypeError, ValueError):
            self.webhook_tolerance = DEFAULT_TOLERANCE

        self.notification_type: str = str(
            cfg("PINGRAM_NOTIFICATION_TYPE", "notification_type", DEFAULT_NOTIFICATION_TYPE)
        ).strip() or DEFAULT_NOTIFICATION_TYPE

        self.allow_all: bool = _truthy(cfg("PINGRAM_ALLOW_ALL_USERS", "allow_all_users", False))
        allowed = _parse_csv(cfg("PINGRAM_ALLOWED_USERS", "allowed_users", ""))
        self._allowed_phones: set = {_norm_phone(a) for a in allowed if "@" not in a}
        self._allowed_emails: set = {_norm_email(a) for a in allowed if "@" in a}

        # Runtime state
        self._runner = None
        self._site = None
        self._reply_ctx: Dict[str, Dict[str, Any]] = {}
        self._seen: Dict[str, float] = {}
        self._tasks: set = set()
        self._warned_unsecured = False

    @property
    def name(self) -> str:
        return "Pingram"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        if not self.api_key:
            logger.error("Pingram: PINGRAM_API_KEY is required")
            self._set_fatal_error("config_missing", "PINGRAM_API_KEY must be set", retryable=False)
            return False

        if not self.channels:
            logger.error("Pingram: configure PINGRAM_FROM_SMS and/or PINGRAM_FROM_EMAIL")
            self._set_fatal_error(
                "config_missing",
                "At least one of PINGRAM_FROM_SMS / PINGRAM_FROM_EMAIL must be set",
                retryable=False,
            )
            return False

        try:
            from aiohttp import web
        except ImportError:
            logger.error("Pingram: aiohttp is required (pip install aiohttp)")
            self._set_fatal_error("dependency_missing", "aiohttp not installed", retryable=False)
            return False

        # Fail fast if the Pingram SDK is missing — sending replies needs it.
        try:
            import pingram  # noqa: F401
        except ImportError:
            logger.error("Pingram: the 'pingram' package is required (pip install pingram)")
            self._set_fatal_error("dependency_missing", "pingram SDK not installed", retryable=False)
            return False

        app = web.Application()
        app.router.add_post(self.webhook_path, self._handle_webhook)
        app.router.add_get("/health", self._handle_health)

        try:
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
            await self._site.start()
        except Exception as e:
            logger.error("Pingram: failed to start webhook server on %s:%s — %s",
                         self.webhook_host, self.webhook_port, e)
            self._set_fatal_error("webhook_bind_failed", str(e), retryable=True)
            return False

        if not self.webhook_secret and not self._warned_unsecured:
            self._warned_unsecured = True
            logger.warning(
                "Pingram: running in UNSECURED mode — webhook signatures are not "
                "verified. Set PINGRAM_WEBHOOK_SECRET (pingram_whsecret_...) to enable "
                "HMAC verification. Recipient validation and the user allowlist still apply."
            )

        self._mark_connected()
        logger.info(
            "Pingram: webhook listening on http://%s:%s%s (channels: %s, mode: %s)",
            self.webhook_host, self.webhook_port, self.webhook_path,
            ",".join(sorted(self.channels)),
            "secured" if self.webhook_secret else "unsecured",
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

    # ── Webhook handling ──────────────────────────────────────────────────

    async def _handle_health(self, request):
        from aiohttp import web
        return web.json_response({
            "status": "ok",
            "platform": "pingram",
            "channels": sorted(self.channels),
            "mode": "secured" if self.webhook_secret else "unsecured",
        })

    async def _handle_webhook(self, request):
        from aiohttp import web
        import json

        raw = await request.read()

        if self.webhook_secret and not self._verify_signature(raw, request.headers):
            return web.Response(status=401, text="invalid signature")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return web.Response(status=400, text="invalid JSON")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="invalid payload")

        event_type = payload.get("eventType") or ""
        if event_type == "SMS_INBOUND":
            channel = "sms"
        elif event_type == "EMAIL_INBOUND":
            channel = "email"
        else:
            return web.json_response({"ok": True, "ignored": "event"})

        if channel not in self.channels:
            return web.json_response({"ok": True, "ignored": "channel"})

        # Defense-in-depth: the inbound recipient must be *our* sender address.
        to_value = payload.get("to", "")
        if channel == "sms":
            if self._from_sms_norm and _norm_phone(to_value) != self._from_sms_norm:
                logger.debug("Pingram: dropping SMS to unexpected recipient %s", _redact_user(to_value))
                return web.json_response({"ok": True, "ignored": "recipient"})
        else:
            if self.from_email and _norm_email(to_value) != self.from_email:
                logger.debug("Pingram: dropping email to unexpected recipient %s", _redact_user(to_value))
                return web.json_response({"ok": True, "ignored": "recipient"})

        sender = payload.get("from", "")
        if not self._is_allowed(channel, sender):
            logger.info("Pingram: ignoring %s from unauthorized user %s", channel, _redact_user(sender))
            return web.json_response({"ok": True, "ignored": "unauthorized"})

        dedup_key = self._dedup_key(channel, payload, request.headers)
        if self._is_duplicate(dedup_key):
            return web.json_response({"ok": True, "ignored": "duplicate"})

        # Build and dispatch in the background so we ACK Pingram immediately.
        task = asyncio.create_task(self._process_inbound(channel, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return web.json_response({"ok": True})

    def _verify_signature(self, raw: bytes, headers) -> bool:
        try:
            from pingram.webhooks import (
                Webhooks,
                WebhookSignatureError,
                WebhookTimestampError,
            )
        except Exception:
            logger.error("Pingram: PINGRAM_WEBHOOK_SECRET is set but the SDK verifier is unavailable")
            return False

        message_id = headers.get("X-Pingram-Id")
        signature = headers.get("X-Pingram-Signature")
        timestamp = headers.get("X-Pingram-Timestamp")
        if not (message_id and signature and timestamp):
            logger.warning("Pingram: webhook missing signature headers")
            return False

        try:
            Webhooks.construct_event(
                payload=raw,
                message_id=message_id,
                signature=signature,
                timestamp=timestamp,
                secret=self.webhook_secret,
                tolerance=self.webhook_tolerance,
            )
            return True
        except (WebhookSignatureError, WebhookTimestampError) as e:
            logger.warning("Pingram: webhook signature rejected: %s", type(e).__name__)
            return False
        except Exception as e:
            logger.warning("Pingram: webhook verification error: %s", e)
            return False

    def _is_allowed(self, channel: str, sender: Any) -> bool:
        if self.allow_all:
            return True
        if channel == "sms":
            return _norm_phone(sender) in self._allowed_phones
        return _norm_email(sender) in self._allowed_emails

    def _dedup_key(self, channel: str, payload: dict, headers) -> str:
        tracking = headers.get("X-Pingram-Id")
        if tracking:
            return f"id:{tracking}"
        if channel == "email" and payload.get("messageId"):
            return f"mid:{payload['messageId']}"
        digest = hashlib.sha256(
            "|".join([
                str(payload.get("from", "")),
                str(payload.get("to", "")),
                str(payload.get("text") or payload.get("bodyText") or payload.get("subject") or ""),
                str(payload.get("receivedAt", "")),
            ]).encode("utf-8")
        ).hexdigest()
        return f"h:{digest}"

    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        # Prune expired entries opportunistically.
        if self._seen:
            expired = [k for k, ts in self._seen.items() if now - ts > _DEDUP_TTL_SECONDS]
            for k in expired:
                self._seen.pop(k, None)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    async def _process_inbound(self, channel: str, payload: dict) -> None:
        try:
            if channel == "sms":
                await self._dispatch_sms(payload)
            else:
                await self._dispatch_email(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pingram: error processing inbound %s", channel)

    # ── SMS inbound ───────────────────────────────────────────────────────

    async def _dispatch_sms(self, payload: dict) -> None:
        if not self._message_handler:
            return
        sender = str(payload.get("from", ""))
        chat_id = f"sms:{sender}"
        text = payload.get("text") or ""

        media_urls, media_types = await self._collect_sms_media(payload)

        self._reply_ctx[chat_id] = {
            "channel": "sms",
            "to_number": sender,
            "user_id": payload.get("userId"),
        }

        source = self.build_source(
            chat_id=chat_id,
            chat_name=_redact_user(sender),
            chat_type="dm",
            user_id=sender,
            user_name=_redact_user(sender),
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.IMAGE if media_urls else MessageType.TEXT,
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
                if _is_image(content_type):
                    path = cache_image_from_bytes(data, _ext_for_content_type(content_type))
                else:
                    fname = url.split("/")[-1].split("?")[0] or f"attachment{_ext_for_content_type(content_type)}"
                    path = cache_document_from_bytes(data, fname)
                media_urls.append(path)
                media_types.append(content_type or "application/octet-stream")
            except Exception:
                logger.debug("Pingram: failed to cache SMS media", exc_info=True)
        return media_urls, media_types

    # ── Email inbound ─────────────────────────────────────────────────────

    async def _dispatch_email(self, payload: dict) -> None:
        if not self._message_handler:
            return
        sender = _norm_email(payload.get("from", ""))
        subject = payload.get("subject") or ""
        thread_key = self._email_thread_key(payload) or sender
        chat_id = f"email:{thread_key}"
        text = payload.get("bodyText") or self._html_to_text(payload.get("bodyHtml") or "")

        media_urls, media_types = self._collect_email_attachments(payload)

        self._reply_ctx[chat_id] = {
            "channel": "email",
            "to_email": sender,
            "subject": subject,
            "message_id": payload.get("messageId"),
            "references": payload.get("references"),
            "user_id": payload.get("userId"),
        }

        source = self.build_source(
            chat_id=chat_id,
            chat_name=subject or _redact_user(sender),
            chat_type="dm",
            user_id=sender,
            user_name=payload.get("fromName") or _redact_user(sender),
            thread_id=thread_key,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.IMAGE if media_urls else MessageType.TEXT,
            source=source,
            message_id=str(payload.get("messageId") or payload.get("trackingId") or int(time.time() * 1000)),
            timestamp=datetime.datetime.now(),
            media_urls=media_urls,
            media_types=media_types,
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

    def _collect_email_attachments(self, payload: dict) -> Tuple[List[str], List[str]]:
        media_urls: List[str] = []
        media_types: List[str] = []
        for att in payload.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            content = att.get("content")
            if not content:
                continue
            try:
                data = base64.b64decode(content)
            except Exception:
                logger.debug("Pingram: failed to decode email attachment", exc_info=True)
                continue
            content_type = att.get("contentType") or "application/octet-stream"
            filename = att.get("filename") or f"attachment{_ext_for_content_type(content_type)}"
            try:
                if _is_image(content_type):
                    path = cache_image_from_bytes(data, _ext_for_content_type(content_type))
                else:
                    path = cache_document_from_bytes(data, filename)
                media_urls.append(path)
                media_types.append(content_type)
            except Exception:
                logger.debug("Pingram: failed to cache email attachment", exc_info=True)
        return media_urls, media_types

    @staticmethod
    def _html_to_text(html: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html or "")
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p>", "\n\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        return html_lib.unescape(text).strip()

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if chat_id.startswith("sms:"):
            number = chat_id[len("sms:"):]
            return await self._send_sms(number, content)
        if chat_id.startswith("email:"):
            return await self._send_email(chat_id, content)
        return SendResult(success=False, error=f"Unknown chat_id prefix: {chat_id}")

    async def _send_sms(self, number: str, content: str, *, attachment_note: str = "") -> SendResult:
        if "sms" not in self.channels or not self.from_sms:
            return SendResult(success=False, error="SMS channel not configured")
        message = (content or "").strip()
        if attachment_note:
            message = f"{message}\n\n{attachment_note}".strip()
        if not message:
            return SendResult(success=False, error="Empty SMS message")

        body = {
            "type": self.notification_type,
            "to": {"id": number, "number": number},
            "sms": {"message": message, "from": self.from_sms},
        }
        return await self._pingram_send(body)

    async def _send_email(self, chat_id: str, content: str, *, attachments: Optional[List[dict]] = None) -> SendResult:
        if "email" not in self.channels or not self.from_email:
            return SendResult(success=False, error="Email channel not configured")
        ctx = self._reply_ctx.get(chat_id)
        to_email = (ctx or {}).get("to_email")
        if not to_email:
            # Fallback: treat the thread key itself as an address if it looks like one.
            candidate = chat_id[len("email:"):]
            to_email = candidate if "@" in candidate else None
        if not to_email:
            return SendResult(success=False, error="No recipient email for thread")

        subject = (ctx or {}).get("subject") or "Message from your Hermes agent"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        email_block: Dict[str, Any] = {"subject": subject, "html": _text_to_html(content)}
        options: Optional[Dict[str, Any]] = None
        if attachments:
            options = {"email": {"attachments": attachments}}

        body: Dict[str, Any] = {
            "type": self.notification_type,
            "to": {"id": to_email, "email": to_email},
            "email": email_block,
        }
        if options:
            body["options"] = options
        return await self._pingram_send(body)

    async def _pingram_send(self, body: Dict[str, Any]) -> SendResult:
        try:
            from pingram import Pingram
            from pingram.models.sender_post_body import SenderPostBody
        except ImportError:
            return SendResult(success=False, error="pingram SDK not installed")

        try:
            sender_body = SenderPostBody.from_dict(body)
        except Exception as e:
            logger.debug("Pingram: failed to build send body", exc_info=True)
            return SendResult(success=False, error=f"invalid send body: {e}")

        try:
            async with Pingram(api_key=self.api_key, region=self.region) as client:
                response = await client.send(sender_body)
            return SendResult(success=True, message_id=getattr(response, "tracking_id", None))
        except Exception as e:
            logger.warning("Pingram: send failed: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # SMS/Email have no typing indicator.
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        channel = "sms" if chat_id.startswith("sms:") else "email" if chat_id.startswith("email:") else "unknown"
        return {"name": chat_id, "type": "dm", "channel": channel}

    # ── Outbound media ────────────────────────────────────────────────────

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, image_url, caption, is_url=True)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media(chat_id, image_path, caption, is_url=False)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media(chat_id, file_path, caption, is_url=False, file_name=file_name)

    async def _send_media(
        self,
        chat_id: str,
        source: str,
        caption: Optional[str],
        *,
        is_url: bool,
        file_name: Optional[str] = None,
    ) -> SendResult:
        caption = caption or ""
        if chat_id.startswith("email:"):
            attachment = await self._build_email_attachment(source, is_url=is_url, file_name=file_name)
            if attachment is None:
                return await self._send_email(chat_id, caption or "(attachment unavailable)")
            return await self._send_email(chat_id, caption, attachments=[attachment])

        if chat_id.startswith("sms:"):
            # Outbound SMS MMS is not supported by the Pingram send SDK (no
            # sms.mediaUrls field). Forward a link when we already have a
            # public URL; otherwise note the limitation in the SMS body.
            number = chat_id[len("sms:"):]
            if is_url and source.lower().startswith(("http://", "https://")):
                note = f"Attachment: {source}"
            else:
                note = "(An attachment was generated but can't be sent over SMS.)"
            return await self._send_sms(number, caption, attachment_note=note)

        return SendResult(success=False, error=f"Unknown chat_id prefix: {chat_id}")

    async def _build_email_attachment(
        self,
        source: str,
        *,
        is_url: bool,
        file_name: Optional[str],
    ) -> Optional[dict]:
        try:
            if is_url:
                data, content_type = await self._download(source)
                if not data:
                    return None
                filename = file_name or source.split("/")[-1].split("?")[0] or f"file{_ext_for_content_type(content_type)}"
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
            logger.debug("Pingram: failed to build email attachment", exc_info=True)
            return None

    # ── Networking ────────────────────────────────────────────────────────

    async def _download(self, url: str) -> Tuple[Optional[bytes], str]:
        try:
            import aiohttp
        except ImportError:
            return None, ""
        try:
            timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.debug("Pingram: download %s returned %s", url, resp.status)
                        return None, ""
                    data = await resp.read()
                    return data, resp.headers.get("Content-Type", "")
        except Exception:
            logger.debug("Pingram: download failed for %s", url, exc_info=True)
            return None, ""


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """True when the Pingram SDK + aiohttp are importable and a key is set."""
    if not os.getenv("PINGRAM_API_KEY"):
        return False
    try:
        import pingram  # noqa: F401
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    api_key = os.getenv("PINGRAM_API_KEY") or extra.get("api_key")
    has_sender = (
        os.getenv("PINGRAM_FROM_SMS") or extra.get("from_sms")
        or os.getenv("PINGRAM_FROM_EMAIL") or extra.get("from_email")
    )
    return bool(api_key and has_sender)


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars during gateway config load."""
    api_key = os.getenv("PINGRAM_API_KEY", "").strip()
    from_sms = os.getenv("PINGRAM_FROM_SMS", "").strip()
    from_email = os.getenv("PINGRAM_FROM_EMAIL", "").strip()
    if not (api_key and (from_sms or from_email)):
        return None
    seed: dict = {"api_key": api_key}
    if os.getenv("PINGRAM_REGION"):
        seed["region"] = os.getenv("PINGRAM_REGION").strip().lower()
    if from_sms:
        seed["from_sms"] = from_sms
    if from_email:
        seed["from_email"] = from_email
    if os.getenv("PINGRAM_CHANNELS"):
        seed["channels"] = os.getenv("PINGRAM_CHANNELS").strip()
    if os.getenv("PINGRAM_WEBHOOK_HOST"):
        seed["webhook_host"] = os.getenv("PINGRAM_WEBHOOK_HOST").strip()
    if os.getenv("PINGRAM_WEBHOOK_PORT"):
        seed["webhook_port"] = os.getenv("PINGRAM_WEBHOOK_PORT").strip()
    if os.getenv("PINGRAM_WEBHOOK_PATH"):
        seed["webhook_path"] = os.getenv("PINGRAM_WEBHOOK_PATH").strip()
    if os.getenv("PINGRAM_NOTIFICATION_TYPE"):
        seed["notification_type"] = os.getenv("PINGRAM_NOTIFICATION_TYPE").strip()
    return seed


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="pingram",
        label="Pingram",
        adapter_factory=lambda cfg: PingramAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["PINGRAM_API_KEY"],
        install_hint="pip install pingram aiohttp",
        env_enablement_fn=_env_enablement,
        allowed_users_env="PINGRAM_ALLOWED_USERS",
        allow_all_env="PINGRAM_ALLOW_ALL_USERS",
        emoji="📨",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are chatting over SMS and/or Email via Pingram. The channel is "
            "encoded in the chat_id prefix: 'sms:' or 'email:'. For SMS, reply in "
            "plain text only (no markdown), keep it short (messages are split into "
            "~160-character segments), and avoid links where possible. For Email, a "
            "subject and light HTML are fine; replies are threaded as 'Re:'. "
            "Inbound MMS images and email attachments are provided to you as media."
        ),
    )
