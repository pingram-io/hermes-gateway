"""Shared inbound polling for Pingram SMS and Email adapters."""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from pingram_gateway.core.constants import (
    DEDUP_TTL_SECONDS,
    LOG_EVENT_EMAIL_INBOUND,
    LOG_EVENT_SMS_INBOUND,
    MAX_POLL_PAGES,
)
from pingram_gateway.core.config import SharedPingramConfig

if TYPE_CHECKING:
    from pingram_gateway.email.adapter import PingramEmailAdapter
    from pingram_gateway.sms.adapter import PingramSmsAdapter

logger = logging.getLogger(__name__)


class PingramPollCoordinator:
    """Singleton poll loop shared by pingram-sms and pingram-email."""

    _instance: Optional["PingramPollCoordinator"] = None

    @classmethod
    def instance(cls) -> "PingramPollCoordinator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._config: Optional[SharedPingramConfig] = None
        self._sms: Optional["PingramSmsAdapter"] = None
        self._email: Optional["PingramEmailAdapter"] = None
        self._refs = 0
        self._poll_task: Optional[asyncio.Task] = None
        self._watermark_ms = 0
        self._seen: Dict[str, float] = {}
        self._tasks: set = set()

    async def attach_sms(
        self, adapter: "PingramSmsAdapter", config: SharedPingramConfig, *, is_reconnect: bool = False
    ) -> None:
        self._sms = adapter
        self._config = config
        await self._acquire(is_reconnect=is_reconnect)

    async def attach_email(
        self, adapter: "PingramEmailAdapter", config: SharedPingramConfig, *, is_reconnect: bool = False
    ) -> None:
        self._email = adapter
        self._config = config
        await self._acquire(is_reconnect=is_reconnect)

    async def detach_sms(self) -> None:
        self._sms = None
        await self._release()

    async def detach_email(self) -> None:
        self._email = None
        await self._release()

    async def _acquire(self, *, is_reconnect: bool = False) -> None:
        self._refs += 1
        if self._poll_task is None and self._config:
            # Cold start skips historical logs. Reconnect keeps the watermark so
            # inbound that arrived during the outage is still delivered.
            if not is_reconnect or self._watermark_ms <= 0:
                self._watermark_ms = int(time.time() * 1000)
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info(
                "Pingram: polling logs.getLogs every %ss; no public endpoint required",
                self._config.poll_interval,
            )

    async def _release(self) -> None:
        self._refs = max(0, self._refs - 1)
        if self._refs == 0 and self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
            for task in list(self._tasks):
                if not task.done():
                    task.cancel()
            self._tasks.clear()

    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        if self._seen:
            expired = [k for k, ts in self._seen.items() if now - ts > DEDUP_TTL_SECONDS]
            for k in expired:
                self._seen.pop(k, None)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pingram: poll cycle failed")
            await asyncio.sleep(self._config.poll_interval if self._config else 15)

    async def _poll_once(self) -> None:
        if not self._config:
            return
        from pingram import Pingram

        new_messages: List[Any] = []
        highest_ms = self._watermark_ms
        cursor: Optional[str] = None
        pages = 0

        async with Pingram(api_key=self._config.api_key, region=self._config.region) as client:
            while pages < MAX_POLL_PAGES:
                resp = await client.logs.logs_get_logs(limit=self._config.poll_limit, cursor=cursor)
                messages = getattr(resp, "messages", None) or []
                if not messages:
                    break
                reached_old = False
                for msg in messages:
                    epoch = int(getattr(msg, "epoch_ms", 0) or 0)
                    if epoch <= self._watermark_ms:
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

        for msg in reversed(new_messages):
            self._handle_log_message(msg)
        self._watermark_ms = max(self._watermark_ms, highest_ms)

    def _handle_log_message(self, msg: Any) -> None:
        event_type = str(getattr(msg, "event_type", "") or "").lower()
        if event_type == LOG_EVENT_SMS_INBOUND:
            adapter = self._sms
            channel = "sms"
        elif event_type == LOG_EVENT_EMAIL_INBOUND:
            adapter = self._email
            channel = "email"
        else:
            return
        if adapter is None:
            return
        sender = getattr(msg, "var_from", None) or ""
        if not adapter.is_sender_allowed(sender):
            from pingram_gateway.core.helpers import redact_user

            logger.info(
                "Pingram: ignoring polled %s from unauthorized user %s",
                channel,
                redact_user(sender),
            )
            return
        tracking = str(getattr(msg, "tracking_id", "") or "")
        epoch = int(getattr(msg, "epoch_ms", 0) or 0)
        from pingram_gateway.core.helpers import norm_phone

        dedup_key = f"track:{tracking}" if tracking else f"poll:{channel}:{norm_phone(sender) or sender}:{epoch}"
        if self._is_duplicate(dedup_key):
            return
        payload = _log_msg_to_payload(channel, msg)
        task = asyncio.create_task(adapter.process_inbound(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _log_msg_to_payload(channel: str, msg: Any) -> dict:
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
