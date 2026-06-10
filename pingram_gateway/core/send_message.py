"""Hermes send_message target parsing for Pingram platforms."""

import json
from typing import Optional, Tuple

from pingram_gateway.core.constants import PLATFORM_EMAIL, PLATFORM_SMS
from pingram_gateway.core.directory import ensure_home_channel, format_list_supplement
from pingram_gateway.core.helpers import (
    looks_like_directory_label,
    normalize_email_chat_id,
    normalize_sms_chat_id,
)

_PLATFORM_ALIASES = {
    "sms": PLATFORM_SMS,
    "pingram": PLATFORM_SMS,
    "email": PLATFORM_EMAIL,
}


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


def normalize_platform_target(target: str) -> str:
    """Map legacy/short platform names to pingram-sms / pingram-email."""
    raw = (target or "").strip()
    if not raw:
        return raw
    parts = raw.split(":", 1)
    platform = parts[0].strip().lower()
    mapped = _PLATFORM_ALIASES.get(platform)
    if not mapped:
        return raw
    if len(parts) > 1:
        return f"{mapped}:{parts[1]}"
    return mapped


def coerce_send_target(target: str) -> str:
    target = normalize_platform_target(target)
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


def _ensure_plugin_platforms_discovered() -> None:
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
    except Exception:
        pass


def install_send_message_parsers() -> None:
    try:
        import tools.send_message_tool as smt
    except ImportError:
        return
    if getattr(smt, "_pingram_split_target_parser_installed", False):
        return

    original_parse = smt._parse_target_ref
    original_handle = smt._handle_send
    original_list = smt._handle_list

    _extend_send_message_schema(smt)

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

    def _handle_list():
        _ensure_plugin_platforms_discovered()
        try:
            from gateway.channel_directory import format_directory_for_display
            from gateway.config import load_gateway_config

            config = load_gateway_config()
            display = format_directory_for_display()
            supplement = format_list_supplement(config=config)
            if supplement:
                if display.startswith("No messaging platforms"):
                    display = "Available messaging targets:\n\n" + supplement
                else:
                    display = display.rstrip() + "\n\n" + supplement
                display = display.rstrip() + '\n\nUse "pingram-sms" alone to send to the SMS home channel.'
            return json.dumps({"targets": display})
        except Exception as exc:
            return original_list() if callable(original_list) else json.dumps({"error": str(exc)})

    def _handle_send(args):
        _ensure_plugin_platforms_discovered()
        args = dict(args or {})
        target = str(args.get("target", "") or "")
        if target:
            target = coerce_send_target(normalize_platform_target(target))
            args["target"] = target

        platform_name = target.split(":", 1)[0].strip().lower() if target else ""
        if platform_name in {PLATFORM_SMS, PLATFORM_EMAIL}:
            try:
                from gateway.config import load_gateway_config

                config = load_gateway_config()
                ensure_home_channel(config, platform_name)
            except Exception:
                pass

        subject = str(args.get("subject", "") or "").strip()
        token = None
        if subject and (not platform_name or platform_name == PLATFORM_EMAIL):
            from pingram_gateway.email.subject import reset_proactive_subject, set_proactive_subject

            token = set_proactive_subject(subject)
        try:
            return original_handle(args)
        finally:
            if token is not None:
                reset_proactive_subject(token)

    smt._parse_target_ref = _parse_target_ref
    smt._handle_send = _handle_send
    smt._handle_list = _handle_list
    smt._pingram_split_target_parser_installed = True


def _extend_send_message_schema(smt) -> None:
    props = smt.SEND_MESSAGE_SCHEMA["parameters"]["properties"]
    if "subject" not in props:
        props["subject"] = {
            "type": "string",
            "description": (
                "Email subject for proactive pingram-email sends (new threads). "
                "Write a short, specific subject summarizing the message — do not "
                "use 'Re:' for new threads. Omit for SMS and when replying in an "
                "existing email thread."
            ),
        }
    description = smt.SEND_MESSAGE_SCHEMA.get("description") or ""
    if "pingram-email" not in description:
        smt.SEND_MESSAGE_SCHEMA["description"] = (
            description.rstrip()
            + "\n\nFor proactive pingram-email sends, always provide a concise, "
            "descriptive subject parameter."
        )
