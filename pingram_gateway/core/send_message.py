"""Hermes send_message target parsing for Pingram platforms."""

from typing import Optional, Tuple

from pingram_gateway.core.constants import PLATFORM_EMAIL, PLATFORM_SMS
from pingram_gateway.core.helpers import (
    looks_like_directory_label,
    normalize_email_chat_id,
    normalize_sms_chat_id,
)


def parse_sms_target_ref(target_ref: str) -> Optional[Tuple[str, None, bool]]:
    ref = (target_ref or "").strip()
    if looks_like_directory_label(ref):
        return None
    chat_id = normalize_sms_chat_id(ref)
    if chat_id:
        return chat_id, None, True
    return None


def parse_email_target_ref(target_ref: str) -> Optional[Tuple[str, None, bool]]:
    ref = (target_ref or "").strip()
    if looks_like_directory_label(ref):
        return None
    chat_id = normalize_email_chat_id(ref)
    if chat_id and "@" in chat_id.split("#", 1)[0]:
        return chat_id, None, True
    return None


def coerce_send_target(target: str) -> str:
    parts = target.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return target
    platform = parts[0].strip().lower()
    ref = parts[1].strip()
    if platform not in {PLATFORM_SMS, PLATFORM_EMAIL}:
        return target
    if looks_like_directory_label(ref):
        return platform
    if platform == PLATFORM_SMS:
        chat_id = normalize_sms_chat_id(ref)
    else:
        chat_id = normalize_email_chat_id(ref)
    if not chat_id:
        return platform
    return target


def install_send_message_parsers() -> None:
    try:
        import tools.send_message_tool as smt
    except ImportError:
        return
    if getattr(smt, "_pingram_split_target_parser_installed", False):
        return

    original_parse = smt._parse_target_ref
    original_handle = smt._handle_send

    def _parse_target_ref(platform_name: str, target_ref: str):
        if platform_name == PLATFORM_SMS:
            parsed = parse_sms_target_ref(target_ref)
            if parsed is not None:
                return parsed
        elif platform_name == PLATFORM_EMAIL:
            parsed = parse_email_target_ref(target_ref)
            if parsed is not None:
                return parsed
        return original_parse(platform_name, target_ref)

    def _handle_send(args):
        target = str(args.get("target", "") or "")
        lower = target.lower()
        if lower.startswith(f"{PLATFORM_SMS}:") or lower.startswith(f"{PLATFORM_EMAIL}:"):
            coerced = coerce_send_target(target)
            if coerced != target:
                args = dict(args)
                args["target"] = coerced
        return original_handle(args)

    smt._parse_target_ref = _parse_target_ref
    smt._handle_send = _handle_send
    smt._pingram_split_target_parser_installed = True
