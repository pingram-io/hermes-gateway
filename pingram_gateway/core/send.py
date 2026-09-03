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
    from pingram_gateway.voice.spec import overlay_briefing

    try:
        from pingram import Pingram
        from pingram.models.voice_call_request import VoiceCallRequest
    except ImportError:
        return SendResult(success=False, error="pingram SDK not installed")

    try:
        async with Pingram(api_key=api_key, region=region) as client:
            spec_dict, saved_id = await _resolve_voice_agent_spec(client, agent_id)
            if not spec_dict or not saved_id:
                return SendResult(
                    success=False,
                    error=(
                        "No Pingram Voice Agent found. Create one in the Pingram dashboard, "
                        "then set PINGRAM_VOICE_AGENT_ID or re-run hermes setup gateway."
                    ),
                )
            spec_dict = overlay_briefing(spec_dict, briefing)
            body: Dict[str, Any] = {
                "phoneNumber": phone_number,
                "spec": spec_dict,
                "agentId": saved_id,
            }
            request = VoiceCallRequest.from_dict(body)
            response = await client.voice.voice_call(request)
        return SendResult(success=True, message_id=getattr(response, "tracking_id", None))
    except Exception as e:
        logger.warning("Pingram Voice: voice.call failed: %s", e)
        return SendResult(success=False, error=str(e))


async def pingram_get_voice_call(api_key: str, region: str, tracking_id: str):
    """Fetch a Voice call record, or None if it is not ready / not found."""
    tid = (tracking_id or "").strip()
    if not tid:
        return None
    try:
        from pingram import Pingram
    except ImportError:
        return None
    try:
        async with Pingram(api_key=api_key, region=region) as client:
            response = await client.voice.voice_get_call(tid)
        return getattr(response, "call", None)
    except Exception:
        logger.debug("Pingram Voice: get call %s failed", tid, exc_info=True)
        return None


def _spec_to_dict(spec: Any) -> Optional[Dict[str, Any]]:
    if spec is None:
        return None
    if isinstance(spec, dict):
        return spec
    to_dict = getattr(spec, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
        except Exception:
            return None
        return data if isinstance(data, dict) else None
    return None


async def _resolve_voice_agent_spec(
    client, preferred_id: Optional[str]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Load a saved Pingram Voice Agent spec. No bundled fallback."""
    wanted = (preferred_id or "").strip() or None
    if wanted:
        try:
            got = await client.voice.voice_get_agent(wanted)
            agent = getattr(got, "agent", None)
            spec_dict = _spec_to_dict(getattr(agent, "spec", None))
            if spec_dict:
                return spec_dict, wanted
        except Exception as e:
            logger.warning("Pingram Voice: failed to load agent %s: %s", wanted, e)

    try:
        resp = await client.voice.voice_list_agents()
        for agent in getattr(resp, "agents", None) or []:
            agent_id = str(getattr(agent, "agent_id", "") or "").strip()
            spec_dict = _spec_to_dict(getattr(agent, "spec", None))
            if agent_id and spec_dict:
                if wanted and agent_id != wanted:
                    logger.warning("Pingram Voice: falling back to saved agent %s", agent_id)
                else:
                    logger.info("Pingram Voice: using saved agent %s", agent_id)
                return spec_dict, agent_id
    except Exception:
        logger.debug("Pingram Voice: list agents failed", exc_info=True)

    logger.warning("Pingram Voice: no saved Voice Agent on this account")
    return None, None


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
