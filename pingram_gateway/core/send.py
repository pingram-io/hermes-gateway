"""Pingram SDK send helpers."""

import asyncio
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


async def pingram_place_voice_call(
    api_key: str,
    region: str,
    phone_number: str,
    briefing: str,
    *,
    agent_id: Optional[str] = None,
):
    """Place an outbound Voice Agent call (POST /voice/call), never send() CALL."""
    from gateway.platforms.base import SendResult
    from pingram_gateway.voice.spec import default_spec_dict, overlay_briefing

    try:
        from pingram import Pingram
        from pingram.models.voice_call_request import VoiceCallRequest
    except ImportError:
        return SendResult(success=False, error="pingram SDK not installed")

    try:
        async with Pingram(api_key=api_key, region=region) as client:
            spec_dict = default_spec_dict()
            saved_id = (agent_id or "").strip() or None
            if saved_id:
                try:
                    got = await client.voice.voice_get_agent(saved_id)
                    if got and got.agent and got.agent.spec:
                        spec_dict = got.agent.spec.to_dict()
                except Exception as e:
                    logger.warning("Pingram Voice: failed to load agent %s, using default spec: %s", saved_id, e)
                    saved_id = None
            spec_dict = overlay_briefing(spec_dict, briefing)
            body: Dict[str, Any] = {"phoneNumber": phone_number, "spec": spec_dict}
            if saved_id:
                body["agentId"] = saved_id
            request = VoiceCallRequest.from_dict(body)
            response = await client.voice.voice_call(request)
        return SendResult(success=True, message_id=getattr(response, "tracking_id", None))
    except Exception as e:
        logger.warning("Pingram Voice: voice.call failed: %s", e)
        return SendResult(success=False, error=str(e))


def fetch_voice_agents(api_key: str, region: str) -> List[Dict[str, str]]:
    try:
        import pingram  # noqa: F401
    except ImportError:
        if not ensure_importable(PINGRAM_IMPORT, PINGRAM_PACKAGE):
            return []

    try:
        from pingram import Pingram

        async def _query() -> List[Dict[str, str]]:
            out: List[Dict[str, str]] = []
            async with Pingram(api_key=api_key, region=region) as client:
                resp = await client.voice.voice_list_agents()
            for agent in getattr(resp, "agents", None) or []:
                agent_id = str(getattr(agent, "agent_id", "") or "").strip()
                if not agent_id:
                    continue
                spec = getattr(agent, "spec", None)
                name = str(getattr(spec, "name", "") or "").strip() or agent_id
                out.append({"agent_id": agent_id, "name": name})
            return out

        return asyncio.run(_query())
    except Exception:
        logger.debug("Pingram: voice agent list failed", exc_info=True)
        return []


def send_welcome_voice_call(
    api_key: str,
    region: str,
    number: str,
    *,
    agent_id: Optional[str] = None,
) -> bool:
    if not number:
        return False
    briefing = (
        "This is a short Hermes setup test. Greet the person, confirm the Voice Agent "
        "call connected, and offer to hang up when they are done."
    )
    try:
        async def _send() -> bool:
            result = await pingram_place_voice_call(
                api_key, region, number, briefing, agent_id=agent_id
            )
            return bool(result.success)

        return asyncio.run(_send())
    except Exception:
        logger.debug("Pingram: welcome voice call failed", exc_info=True)
        return False
