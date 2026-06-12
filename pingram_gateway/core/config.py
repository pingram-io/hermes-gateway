"""Shared Pingram configuration loaded from env / PlatformConfig.extra."""

import os
from dataclasses import dataclass
from typing import Any, Optional

from pingram_gateway.core.constants import (
    DEFAULT_NOTIFICATION_TYPE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_LIMIT,
    MIN_POLL_INTERVAL,
)
from pingram_gateway.core.helpers import cfg_value, norm_email, norm_phone, normalize_phone_e164, parse_csv, truthy


@dataclass
class SharedPingramConfig:
    api_key: str
    region: str
    poll_interval: int
    poll_limit: int
    notification_type: str
    allow_all: bool


def load_shared_config(config) -> SharedPingramConfig:
    try:
        poll_interval = int(cfg_value(config, "PINGRAM_POLL_INTERVAL", "poll_interval", DEFAULT_POLL_INTERVAL))
    except (TypeError, ValueError):
        poll_interval = DEFAULT_POLL_INTERVAL
    poll_interval = max(MIN_POLL_INTERVAL, poll_interval)
    try:
        poll_limit = int(cfg_value(config, "PINGRAM_POLL_LIMIT", "poll_limit", DEFAULT_POLL_LIMIT))
    except (TypeError, ValueError):
        poll_limit = DEFAULT_POLL_LIMIT
    poll_limit = max(1, poll_limit)
    notification_type = str(
        cfg_value(config, "PINGRAM_NOTIFICATION_TYPE", "notification_type", DEFAULT_NOTIFICATION_TYPE)
    ).strip() or DEFAULT_NOTIFICATION_TYPE
    return SharedPingramConfig(
        api_key=str(cfg_value(config, "PINGRAM_API_KEY", "api_key", "")).strip(),
        region=str(cfg_value(config, "PINGRAM_REGION", "region", "us")).strip().lower() or "us",
        poll_interval=poll_interval,
        poll_limit=poll_limit,
        notification_type=notification_type,
        allow_all=truthy(cfg_value(config, "PINGRAM_ALLOW_ALL_USERS", "allow_all_users", False)),
    )


def shared_env_seed() -> Optional[dict]:
    api_key = os.getenv("PINGRAM_API_KEY", "").strip()
    if not api_key:
        return None
    seed: dict = {"api_key": api_key}
    if os.getenv("PINGRAM_REGION"):
        seed["region"] = os.getenv("PINGRAM_REGION").strip().lower()
    if os.getenv("PINGRAM_POLL_INTERVAL"):
        seed["poll_interval"] = os.getenv("PINGRAM_POLL_INTERVAL").strip()
    if os.getenv("PINGRAM_POLL_LIMIT"):
        seed["poll_limit"] = os.getenv("PINGRAM_POLL_LIMIT").strip()
    if os.getenv("PINGRAM_NOTIFICATION_TYPE"):
        seed["notification_type"] = os.getenv("PINGRAM_NOTIFICATION_TYPE").strip()
    return seed


def channel_env_seed(home_env: str, home_name_env: str = "") -> Optional[dict]:
    seed = shared_env_seed()
    if seed is None:
        return None
    home = os.getenv(home_env, "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": (os.getenv(home_name_env, "").strip() if home_name_env else "") or "Home",
        }
    return seed


def sms_configured(config) -> bool:
    shared = load_shared_config(config)
    if not shared.api_key:
        return False
    allowed = os.getenv("PINGRAM_SMS_ALLOWED_USERS") or cfg_value(config, "PINGRAM_SMS_ALLOWED_USERS", "allowed_users", "")
    home = os.getenv("PINGRAM_SMS_HOME_CHANNEL") or cfg_value(config, "PINGRAM_SMS_HOME_CHANNEL", "home_channel_chat_id", "")
    return bool(parse_csv(allowed) or str(home).strip() or shared.allow_all)


def email_configured(config) -> bool:
    shared = load_shared_config(config)
    if not shared.api_key:
        return False
    allowed = os.getenv("PINGRAM_EMAIL_ALLOWED_USERS") or cfg_value(
        config, "PINGRAM_EMAIL_ALLOWED_USERS", "allowed_users", ""
    )
    home = os.getenv("PINGRAM_EMAIL_HOME_CHANNEL") or cfg_value(
        config, "PINGRAM_EMAIL_HOME_CHANNEL", "home_channel_chat_id", ""
    )
    return bool(parse_csv(allowed) or str(home).strip() or shared.allow_all)


def load_sms_allowlist(config) -> set:
    allowed = parse_csv(os.getenv("PINGRAM_SMS_ALLOWED_USERS") or cfg_value(config, "PINGRAM_SMS_ALLOWED_USERS", "allowed_users", ""))
    return {norm_phone(normalize_phone_e164(a)) for a in allowed}


def load_email_allowlist(config) -> set:
    allowed = parse_csv(
        os.getenv("PINGRAM_EMAIL_ALLOWED_USERS") or cfg_value(config, "PINGRAM_EMAIL_ALLOWED_USERS", "allowed_users", "")
    )
    return {norm_email(a) for a in allowed if "@" in a}


def sms_home_channel(config) -> str:
    return str(
        os.getenv("PINGRAM_SMS_HOME_CHANNEL") or cfg_value(config, "PINGRAM_SMS_HOME_CHANNEL", "home_channel_chat_id", "")
    ).strip()


def email_home_channel(config) -> str:
    return str(
        os.getenv("PINGRAM_EMAIL_HOME_CHANNEL") or cfg_value(config, "PINGRAM_EMAIL_HOME_CHANNEL", "home_channel_chat_id", "")
    ).strip()


def sms_inbound_ready(config) -> bool:
    if load_sms_allowlist(config):
        return True
    return load_shared_config(config).allow_all


def email_inbound_ready(config) -> bool:
    if load_email_allowlist(config):
        return True
    return load_shared_config(config).allow_all
