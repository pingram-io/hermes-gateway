"""Pingram SDK send helpers."""

import asyncio
import html as html_lib
import logging
from typing import Any, Dict, List, Optional, Tuple

from pingram_gateway.core.constants import DEFAULT_FROM_NAME, DEFAULT_NOTIFICATION_TYPE, PINGRAM_IMPORT, PINGRAM_PACKAGE
from pingram_gateway.core.helpers import ensure_importable, text_to_html

logger = logging.getLogger(__name__)


async def pingram_send(api_key: str, region: str, body: Dict[str, Any]):
    from gateway.platforms.base import SendResult

    try:
        from pingram import Pingram
        from pingram.models.sender_post_body import SenderPostBody
    except ImportError:
        return SendResult(success=False, error="pingram SDK not installed")
    try:
        sender_body = SenderPostBody.from_dict(body)
    except Exception as e:
        return SendResult(success=False, error=f"invalid send body: {e}")
    try:
        async with Pingram(api_key=api_key, region=region) as client:
            response = await client.send(sender_body)
        return SendResult(success=True, message_id=getattr(response, "tracking_id", None))
    except Exception as e:
        logger.warning("Pingram: send failed: %s", e)
        return SendResult(success=False, error=str(e))


def fetch_account_identities(api_key: str, region: str) -> Tuple[List[str], List[str], Optional[str]]:
    try:
        import pingram  # noqa: F401
    except ImportError:
        if not ensure_importable(PINGRAM_IMPORT, PINGRAM_PACKAGE):
            return [], [], None

    try:
        from pingram import Pingram

        async def _query() -> Tuple[List[str], List[str], Optional[str]]:
            emails: List[str] = []
            numbers: List[str] = []
            shared_number: Optional[str] = None
            async with Pingram(api_key=api_key, region=region) as client:
                try:
                    resp = await client.addresses.addresses_list_addresses()
                    for addr in getattr(resp, "addresses", None) or []:
                        full = getattr(addr, "full_address", None)
                        if full:
                            emails.append(str(full).strip())
                except Exception:
                    logger.debug("Pingram: addresses.listAddresses failed", exc_info=True)
                try:
                    resp = await client.numbers.numbers_list()
                    for num in getattr(resp, "numbers", None) or []:
                        number = getattr(num, "phone_number", None)
                        if number:
                            numbers.append(str(number).strip())
                    shared = getattr(resp, "shared_number", None)
                    if shared:
                        shared_number = str(shared).strip() or None
                except Exception:
                    logger.debug("Pingram: numbers.list failed", exc_info=True)
            return emails, numbers, shared_number

        return asyncio.run(_query())
    except Exception:
        logger.debug("Pingram: account identity lookup failed", exc_info=True)
        return [], [], None


def send_welcome_sms(api_key: str, region: str, number: str, *, from_sms: str = "") -> bool:
    if not number:
        return False
    text = (
        "Hey! This is your Hermes agent. I'm all set up and connected — "
        "reply here anytime and I'll help you out."
    )
    try:
        from pingram import Pingram
        from pingram.models.sender_post_body import SenderPostBody

        async def _send() -> bool:
            sms_block: Dict[str, Any] = {"message": text}
            if from_sms:
                sms_block["from"] = from_sms
            body = {
                "type": DEFAULT_NOTIFICATION_TYPE,
                "to": {"id": number, "number": number},
                "sms": sms_block,
            }
            async with Pingram(api_key=api_key, region=region) as client:
                await client.send(SenderPostBody.from_dict(body))
            return True

        return asyncio.run(_send())
    except Exception:
        logger.debug("Pingram: welcome SMS failed", exc_info=True)
        return False


def send_welcome_email(
    api_key: str,
    region: str,
    address: str,
    *,
    from_email: str = "",
    from_name: str = DEFAULT_FROM_NAME,
) -> bool:
    if not address:
        return False
    text = (
        "Hey! This is your Hermes agent. I'm all set up and connected — "
        "reply here anytime and I'll help you out."
    )
    subject = "Hey, this is your Hermes agent"
    try:
        from pingram import Pingram
        from pingram.models.sender_post_body import SenderPostBody

        async def _send() -> bool:
            email_block: Dict[str, Any] = {
                "subject": subject,
                "html": text_to_html(text),
                "senderName": from_name or DEFAULT_FROM_NAME,
            }
            if from_email:
                email_block["senderEmail"] = from_email
            body = {
                "type": DEFAULT_NOTIFICATION_TYPE,
                "to": {"id": address, "email": address},
                "email": email_block,
            }
            async with Pingram(api_key=api_key, region=region) as client:
                await client.send(SenderPostBody.from_dict(body))
            return True

        return asyncio.run(_send())
    except Exception:
        logger.debug("Pingram: welcome email failed", exc_info=True)
        return False


def send_voice_beta_signup(
    api_key: str,
    region: str,
    user_email: str,
    *,
    from_email: str = "",
    from_name: str = DEFAULT_FROM_NAME,
) -> bool:
    """Notify Pingram that a user wants Voice beta access."""
    from pingram_gateway.core.constants import VOICE_BETA_CONTACT

    if not user_email:
        return False
    subject = "Hermes Voice beta signup"
    safe_email = html_lib.escape(user_email)
    html = (
        "<p>A Hermes user requested access to the Pingram Voice beta.</p>"
        f"<p><strong>Contact email:</strong> {safe_email}</p>"
        "<p>Submitted via the Hermes Pingram Voice setup wizard.</p>"
    )
    try:
        from pingram import Pingram
        from pingram.models.sender_post_body import SenderPostBody

        async def _send() -> bool:
            email_block: Dict[str, Any] = {
                "subject": subject,
                "html": html,
                "senderName": from_name or DEFAULT_FROM_NAME,
            }
            if from_email:
                email_block["senderEmail"] = from_email
            body = {
                "type": DEFAULT_NOTIFICATION_TYPE,
                "to": {"id": VOICE_BETA_CONTACT, "email": VOICE_BETA_CONTACT},
                "email": email_block,
            }
            async with Pingram(api_key=api_key, region=region) as client:
                await client.send(SenderPostBody.from_dict(body))
            return True

        return asyncio.run(_send())
    except Exception:
        logger.debug("Pingram: voice beta signup email failed", exc_info=True)
        return False
