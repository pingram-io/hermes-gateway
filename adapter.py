"""
Pingram Platform Adapter for Hermes Agent.

A single Hermes *platform plugin* that lets users chat with their Hermes agent
over Pingram-managed **SMS** and **Email**.  One combined ``pingram`` platform
serves both channels (Pingram uses one API key for both); the channel is encoded
in the Hermes ``chat_id`` prefix (``sms:`` / ``email:``).

Inbound messages are received by **polling** Pingram's logs API on a timer — no
public endpoint or webhook registration is required.

Flow::

    Human --SMS/Email--> Pingram
                            |  (poll logs.getLogs every N seconds)
                            v
                  PingramAdapter -> MessageEvent -> Hermes agent
                            |
    Human <--SMS/Email-- Pingram <-- Pingram SDK <-- PingramAdapter.send()

Configuration (env vars override config.yaml ``extra``):
    PINGRAM_API_KEY            (required) pingram_sk_...
    PINGRAM_REGION             us | eu | ca (default: us)
    PINGRAM_CHANNELS           csv of channels to enable (sms,email); this turns
                               channels on. Default: inferred from any sender.
    PINGRAM_POLL_INTERVAL      seconds between polls (default 15)
    PINGRAM_POLL_LIMIT         max messages fetched per poll page (default 50)
    PINGRAM_FROM_SMS           optional SMS sender number (E.164); blank -> Pingram
                               account default number
    PINGRAM_FROM_EMAIL         optional email sender address; blank -> Pingram default
    PINGRAM_FROM_NAME          optional email sender display name; blank -> default
    PINGRAM_ALLOWED_USERS      csv of phones/emails allowed to talk to the agent
    PINGRAM_ALLOW_ALL_USERS    true to allow everyone (dev only; default false)
    PINGRAM_NOTIFICATION_TYPE  Pingram notification `type` for replies
                               (default: hermes_agent_reply)

Note: inbound email *attachments* are not downloaded in polling mode (Pingram's
logs API returns attachment metadata only). Inbound SMS/MMS images work fully.
"""

import asyncio
import base64
import datetime
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

DEFAULT_NOTIFICATION_TYPE = "hermes_agent_reply"
_DEDUP_TTL_SECONDS = 3600
_DOWNLOAD_TIMEOUT = 30

# Inbound polling.
DEFAULT_POLL_INTERVAL = 15
MIN_POLL_INTERVAL = 3
DEFAULT_POLL_LIMIT = 50
# Bound on how many log pages a single poll cycle will walk back through.
_MAX_POLL_PAGES = 10
# logs.getLogs event_type values for inbound messages.
_LOG_EVENT_SMS_INBOUND = "sms_inbound"
_LOG_EVENT_EMAIL_INBOUND = "inbound"


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

        # Sender identity is OPTIONAL on every channel — when left blank, Pingram
        # fills in the account default (a Pingram-managed number / sender). We
        # only send these fields when the user explicitly set them.
        self.from_sms: str = str(cfg("PINGRAM_FROM_SMS", "from_sms", "")).strip()
        self.from_email: str = _norm_email(cfg("PINGRAM_FROM_EMAIL", "from_email", ""))
        self.from_name: str = str(cfg("PINGRAM_FROM_NAME", "from_name", "")).strip()

        # Channels are an explicit choice (PINGRAM_CHANNELS), independent of
        # whether a sender is configured. Fall back to inferring from configured
        # senders for legacy setups that predate the channel selector.
        requested = set(_parse_csv(cfg("PINGRAM_CHANNELS", "channels", ""))) & {"sms", "email"}
        if requested:
            self.channels: set = requested
        else:
            inferred: set = set()
            if self.from_sms:
                inferred.add("sms")
            if self.from_email:
                inferred.add("email")
            self.channels = inferred

        # Inbound polling cadence.
        try:
            self.poll_interval: int = int(cfg("PINGRAM_POLL_INTERVAL", "poll_interval", DEFAULT_POLL_INTERVAL))
        except (TypeError, ValueError):
            self.poll_interval = DEFAULT_POLL_INTERVAL
        self.poll_interval = max(MIN_POLL_INTERVAL, self.poll_interval)
        try:
            self.poll_limit: int = int(cfg("PINGRAM_POLL_LIMIT", "poll_limit", DEFAULT_POLL_LIMIT))
        except (TypeError, ValueError):
            self.poll_limit = DEFAULT_POLL_LIMIT
        self.poll_limit = max(1, self.poll_limit)

        self.notification_type: str = str(
            cfg("PINGRAM_NOTIFICATION_TYPE", "notification_type", DEFAULT_NOTIFICATION_TYPE)
        ).strip() or DEFAULT_NOTIFICATION_TYPE

        self.allow_all: bool = _truthy(cfg("PINGRAM_ALLOW_ALL_USERS", "allow_all_users", False))
        allowed = _parse_csv(cfg("PINGRAM_ALLOWED_USERS", "allowed_users", ""))
        self._allowed_phones: set = {_norm_phone(a) for a in allowed if "@" not in a}
        self._allowed_emails: set = {_norm_email(a) for a in allowed if "@" in a}

        # Runtime state
        self._reply_ctx: Dict[str, Dict[str, Any]] = {}
        self._seen: Dict[str, float] = {}
        self._tasks: set = set()
        self._poll_task = None
        # Only messages newer than this watermark are processed; set at startup
        # so we don't replay history on the first poll.
        self._poll_watermark_ms = 0

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
            logger.error("Pingram: enable at least one channel via PINGRAM_CHANNELS (sms and/or email)")
            self._set_fatal_error(
                "config_missing",
                "PINGRAM_CHANNELS must enable sms and/or email",
                retryable=False,
            )
            return False

        # aiohttp is used to download inbound MMS media.
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            logger.error("Pingram: aiohttp is required (pip install aiohttp)")
            self._set_fatal_error("dependency_missing", "aiohttp not installed", retryable=False)
            return False

        # Fail fast if the Pingram SDK is missing — it's used to poll for inbound
        # messages and to send replies.
        try:
            import pingram  # noqa: F401
        except ImportError:
            logger.error("Pingram: the Pingram SDK is required (pip install pingram-python)")
            self._set_fatal_error("dependency_missing", "pingram SDK not installed", retryable=False)
            return False

        # Start the watermark at "now" so we don't replay the account's history
        # on the first poll — only messages that arrive after startup are
        # delivered.
        self._poll_watermark_ms = int(time.time() * 1000)
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._mark_connected()
        logger.info(
            "Pingram: polling logs.getLogs every %ss (channels: %s); no public endpoint required",
            self.poll_interval,
            ",".join(sorted(self.channels)),
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    # ── Inbound helpers ───────────────────────────────────────────────────

    def _is_allowed(self, channel: str, sender: Any) -> bool:
        if self.allow_all:
            return True
        if channel == "sms":
            return _norm_phone(sender) in self._allowed_phones
        return _norm_email(sender) in self._allowed_emails

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

    # ── Poll-mode inbound ─────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Periodically pull new inbound messages via logs.getLogs.

        Each cycle fetches the newest messages, keeps those newer than a
        watermark, and dispatches the inbound ones. trackingId dedup is the
        authoritative guard against reprocessing; the watermark only bounds how
        far back we page.
        """
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pingram: poll cycle failed")
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self) -> None:
        from pingram import Pingram

        new_messages: List[Any] = []
        highest_ms = self._poll_watermark_ms
        cursor: Optional[str] = None
        pages = 0

        async with Pingram(api_key=self.api_key, region=self.region) as client:
            while pages < _MAX_POLL_PAGES:
                resp = await client.logs.logs_get_logs(limit=self.poll_limit, cursor=cursor)
                messages = getattr(resp, "messages", None) or []
                if not messages:
                    break
                # Messages are newest-first; once we cross the watermark every
                # remaining (and subsequent page) entry is older, so we stop.
                reached_old = False
                for msg in messages:
                    epoch = int(getattr(msg, "epoch_ms", 0) or 0)
                    if epoch <= self._poll_watermark_ms:
                        reached_old = True
                        break
                    new_messages.append(msg)
                    if epoch > highest_ms:
                        highest_ms = epoch
                if reached_old:
                    break
                cursor = getattr(resp, "next_cursor", None)
                if not cursor:
                    break
                pages += 1

        # Dispatch oldest-first so a burst arrives in natural order.
        for msg in reversed(new_messages):
            self._handle_log_message(msg)

        # Advance the watermark so the next cycle only sees newer messages.
        self._poll_watermark_ms = max(self._poll_watermark_ms, highest_ms)

    def _handle_log_message(self, msg: Any) -> None:
        event_type = str(getattr(msg, "event_type", "") or "").lower()
        if event_type == _LOG_EVENT_SMS_INBOUND:
            channel = "sms"
        elif event_type == _LOG_EVENT_EMAIL_INBOUND:
            channel = "email"
        else:
            return  # not an inbound message (sent/delivered/opened/etc.)

        if channel not in self.channels:
            return

        sender = getattr(msg, "var_from", None) or ""
        if not self._is_allowed(channel, sender):
            logger.info("Pingram: ignoring polled %s from unauthorized user %s", channel, _redact_user(sender))
            return

        tracking = str(getattr(msg, "tracking_id", "") or "")
        epoch = int(getattr(msg, "epoch_ms", 0) or 0)
        dedup_key = f"track:{tracking}" if tracking else f"poll:{channel}:{_norm_phone(sender) or sender}:{epoch}"
        if self._is_duplicate(dedup_key):
            return

        payload = self._log_msg_to_payload(channel, msg)

        # Run dispatch in the background so a long agent turn doesn't stall the
        # poll loop.
        task = asyncio.create_task(self._process_inbound(channel, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _log_msg_to_payload(channel: str, msg: Any) -> dict:
        """Adapt a logs.getLogs message summary to the dict the ``_dispatch_*``
        methods consume."""
        tracking = getattr(msg, "tracking_id", None)
        if channel == "sms":
            media = []
            for m in getattr(msg, "media", None) or []:
                url = getattr(m, "url", None)
                if url:
                    media.append({"url": url, "contentType": getattr(m, "content_type", None)})
            return {
                "from": getattr(msg, "var_from", None) or "",
                "text": getattr(msg, "body_text", None) or "",
                "media": media,
                "trackingId": tracking,
            }
        # Email. Inbound attachment content is not available via logs.getLogs
        # (only metadata), so attachments are intentionally omitted here.
        return {
            "from": getattr(msg, "var_from", None) or "",
            "subject": getattr(msg, "subject", None) or "",
            "bodyText": getattr(msg, "body_text", None) or "",
            "bodyHtml": getattr(msg, "body_html", None) or "",
            "references": getattr(msg, "references", None),
            "inReplyTo": getattr(msg, "in_reply_to", None),
            "messageId": getattr(msg, "message_id", None),
            "fromName": getattr(msg, "from_name", None),
            "trackingId": tracking,
        }

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
            message_type=MessageType.PHOTO if media_urls else MessageType.TEXT,
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
        if "sms" not in self.channels:
            return SendResult(success=False, error="SMS channel not configured")
        message = (content or "").strip()
        if attachment_note:
            message = f"{message}\n\n{attachment_note}".strip()
        if not message:
            return SendResult(success=False, error="Empty SMS message")

        # ``from`` is optional — omit it so Pingram uses the account's default
        # number unless the user pinned a specific verified sender.
        sms_block: Dict[str, Any] = {"message": message}
        if self.from_sms:
            sms_block["from"] = self.from_sms
        body = {
            "type": self.notification_type,
            "to": {"id": number, "number": number},
            "sms": sms_block,
        }
        return await self._pingram_send(body)

    async def _send_email(self, chat_id: str, content: str, *, attachments: Optional[List[dict]] = None) -> SendResult:
        if "email" not in self.channels:
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
        # Sender name/address are optional overrides — omit them and Pingram
        # uses the account default (e.g. noreply@pingram.io).
        if self.from_name:
            email_block["senderName"] = self.from_name
        if self.from_email:
            email_block["senderEmail"] = self.from_email
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
    # A channel must be enabled. Senders are optional (Pingram defaults them),
    # so fall back to legacy sender-presence inference when PINGRAM_CHANNELS is
    # not set.
    has_channel = (
        os.getenv("PINGRAM_CHANNELS") or extra.get("channels")
        or os.getenv("PINGRAM_FROM_SMS") or extra.get("from_sms")
        or os.getenv("PINGRAM_FROM_EMAIL") or extra.get("from_email")
    )
    return bool(api_key and has_channel)


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars during gateway config load."""
    api_key = os.getenv("PINGRAM_API_KEY", "").strip()
    channels = os.getenv("PINGRAM_CHANNELS", "").strip()
    from_sms = os.getenv("PINGRAM_FROM_SMS", "").strip()
    from_email = os.getenv("PINGRAM_FROM_EMAIL", "").strip()
    from_name = os.getenv("PINGRAM_FROM_NAME", "").strip()
    # Enable when a channel is selected; senders are optional (Pingram defaults
    # them). Legacy setups enable implicitly via a configured sender.
    if not (api_key and (channels or from_sms or from_email)):
        return None
    seed: dict = {"api_key": api_key}
    if os.getenv("PINGRAM_REGION"):
        seed["region"] = os.getenv("PINGRAM_REGION").strip().lower()
    if channels:
        seed["channels"] = channels
    if from_sms:
        seed["from_sms"] = from_sms
    if from_email:
        seed["from_email"] = from_email
    if from_name:
        seed["from_name"] = from_name
    if os.getenv("PINGRAM_POLL_INTERVAL"):
        seed["poll_interval"] = os.getenv("PINGRAM_POLL_INTERVAL").strip()
    if os.getenv("PINGRAM_POLL_LIMIT"):
        seed["poll_limit"] = os.getenv("PINGRAM_POLL_LIMIT").strip()
    if os.getenv("PINGRAM_NOTIFICATION_TYPE"):
        seed["notification_type"] = os.getenv("PINGRAM_NOTIFICATION_TYPE").strip()
    return seed


def interactive_setup() -> None:
    """Guided ``hermes setup gateway`` flow for Pingram.

    Reached by selecting Pingram in the messaging-platforms menu. Lazy-imports
    the CLI prompt helpers so the plugin stays importable in non-CLI contexts
    (gateway runtime, tests). Only the region, API key, and a channel choice are
    required; sender identity is optional (Pingram supplies defaults).
    """
    from hermes_cli.setup import (
        prompt,
        prompt_choice,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("Pingram")
    if get_env_value("PINGRAM_API_KEY") and not prompt_yes_no("Pingram is already configured. Reconfigure?", False):
        return

    print_info("Chat with your Hermes agent over SMS and/or Email, routed through Pingram.")
    print_info("You'll need a Pingram API key. Sender details are optional — Pingram")
    print_info("uses sensible defaults when you leave them blank.")
    print()

    # Region first — it selects the API endpoint, so it must match the account's
    # region before anything else (e.g. the key) is used.
    print_info("Pick the region your Pingram account lives in (selects the API endpoint).")
    regions = ["us", "eu", "ca"]
    current_region = (get_env_value("PINGRAM_REGION") or "us").strip().lower()
    default_idx = regions.index(current_region) if current_region in regions else 0
    region = regions[prompt_choice("Region", ["US", "EU", "CA"], default_idx)]
    save_env_value("PINGRAM_REGION", region)

    api_key = prompt("Pingram API key (pingram_sk_...)", password=True)
    if not api_key:
        print_warning("API key is required — skipping Pingram setup.")
        return
    save_env_value("PINGRAM_API_KEY", api_key.strip())

    # Channel selection drives everything else. Senders are optional per channel.
    print()
    print_info("Choose which channels your agent should use.")
    existing_channels = {c for c in _parse_csv(get_env_value("PINGRAM_CHANNELS") or "") if c in {"sms", "email"}}
    channel_options = ["SMS only", "Email only", "Both SMS and Email"]
    if existing_channels == {"sms"}:
        channel_default = 0
    elif existing_channels == {"email"}:
        channel_default = 1
    elif existing_channels == {"sms", "email"}:
        channel_default = 2
    else:
        channel_default = 2
    channel_idx = prompt_choice("Which channels do you want to enable?", channel_options, channel_default)
    channels = {0: {"sms"}, 1: {"email"}, 2: {"sms", "email"}}[channel_idx]
    save_env_value("PINGRAM_CHANNELS", ",".join(sorted(channels)))

    # SMS sender — default Pingram number, or a specific verified number.
    if "sms" in channels:
        print()
        print_info("SMS sender number (the 'from' your texts come from).")
        existing_sms = (get_env_value("PINGRAM_FROM_SMS") or "").strip()
        sms_sender_idx = prompt_choice(
            "Which number should send your SMS replies?",
            [
                "Use my Pingram account's default number (recommended)",
                "Use a specific number from my Pingram account",
            ],
            1 if existing_sms else 0,
        )
        if sms_sender_idx == 1:
            sms_from = prompt(
                "Pingram sender number (E.164, e.g. +15551234567)",
                default=existing_sms,
            )
            save_env_value("PINGRAM_FROM_SMS", sms_from.strip())
            if not sms_from.strip():
                print_info("Left blank — Pingram will use your account's default number.")
        else:
            save_env_value("PINGRAM_FROM_SMS", "")
    else:
        save_env_value("PINGRAM_FROM_SMS", "")

    # Email sender identity — both fields optional; blank uses Pingram defaults.
    if "email" in channels:
        print()
        print_info("Email sender identity. Both fields are optional — leave them blank to")
        print_info("use Pingram's defaults.")
        from_name = prompt(
            'Sender display name (e.g. "My Bot") — blank for default (Pingram)',
            default=get_env_value("PINGRAM_FROM_NAME") or "",
        )
        save_env_value("PINGRAM_FROM_NAME", from_name.strip())
        from_email = prompt(
            "Sender email address (e.g. noreply@yourdomain.com) — blank for default (noreply@pingram.io)",
            default=get_env_value("PINGRAM_FROM_EMAIL") or "",
        )
        save_env_value("PINGRAM_FROM_EMAIL", from_email.strip())
    else:
        save_env_value("PINGRAM_FROM_NAME", "")
        save_env_value("PINGRAM_FROM_EMAIL", "")

    # Access control.
    print()
    print_info("Access control: restrict who can talk to the agent.")
    if prompt_yes_no("Allow ALL users to message the agent? (dev only)", False):
        save_env_value("PINGRAM_ALLOW_ALL_USERS", "true")
        save_env_value("PINGRAM_ALLOWED_USERS", "")
        print_warning("Open access — anyone who texts/emails your address can use the agent.")
    else:
        save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")
        # One allowlist env var, but ask per enabled channel so the prompts only
        # cover what's relevant (phones for SMS, emails for Email).
        existing = _parse_csv(get_env_value("PINGRAM_ALLOWED_USERS") or "")
        existing_phones = ",".join(u for u in existing if "@" not in u)
        existing_emails = ",".join(u for u in existing if "@" in u)
        allowed: List[str] = []
        if "sms" in channels:
            phones = prompt(
                "Allowed phone numbers for SMS (comma-separated, E.164)",
                default=existing_phones,
            )
            allowed += _parse_csv(phones)
        if "email" in channels:
            emails = prompt(
                "Allowed email addresses for Email (comma-separated)",
                default=existing_emails,
            )
            allowed += _parse_csv(emails)
        save_env_value("PINGRAM_ALLOWED_USERS", ",".join(allowed))
        if not allowed:
            print_warning("No users allowlisted — inbound messages will be ignored until you add some.")

    # Inbound polling cadence. Hermes pulls new messages from Pingram on a timer;
    # there's no public endpoint or webhook to configure.
    print()
    print_info("Hermes receives incoming messages by polling Pingram (no public URL needed).")
    interval = prompt(
        "Seconds between polls",
        default=get_env_value("PINGRAM_POLL_INTERVAL") or str(DEFAULT_POLL_INTERVAL),
    )
    save_env_value("PINGRAM_POLL_INTERVAL", interval.strip() or str(DEFAULT_POLL_INTERVAL))
    if "email" in channels:
        print_warning("Note: inbound email attachments are not downloaded when polling "
                      "(SMS/MMS images still work).")

    print()
    print_success("Pingram configured!")
    print_info("Next steps:")
    print_info("  1. Start the gateway: hermes gateway")
    print_info(f"  2. That's it — Hermes polls Pingram every {interval.strip() or DEFAULT_POLL_INTERVAL}s "
               "for new messages.")
    print_info("     No public URL or webhook subscription required.")


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="pingram",
        label="Pingram",
        adapter_factory=lambda cfg: PingramAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["PINGRAM_API_KEY", "PINGRAM_REGION"],
        install_hint="pip install hermes-pingram-gateway  (or: pip install pingram-python aiohttp)",
        env_enablement_fn=_env_enablement,
        setup_fn=interactive_setup,
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
            "Inbound MMS images are provided to you as media (inbound email "
            "attachments are not available)."
        ),
    )
