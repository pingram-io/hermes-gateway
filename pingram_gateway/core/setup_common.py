"""Shared setup wizard helpers."""

import logging
from typing import Any, Dict, List, Optional, Tuple

from pingram_gateway.core.constants import DEFAULT_FROM_NAME, DISPLAY_OVERRIDES
from pingram_gateway.core.helpers import norm_email, normalize_phone_e164, parse_csv

logger = logging.getLogger(__name__)


def _empty_platform_config():
    from gateway.config import PlatformConfig

    return PlatformConfig(enabled=True)


def sms_platform_configured() -> bool:
    from pingram_gateway.core.config import sms_configured

    return sms_configured(_empty_platform_config())


def email_platform_configured() -> bool:
    from pingram_gateway.core.config import email_configured

    return email_configured(_empty_platform_config())


def voice_platform_configured() -> bool:
    from pingram_gateway.core.config import voice_configured

    return voice_configured(_empty_platform_config())


def shared_credentials_configured() -> bool:
    from hermes_cli.setup import get_env_value

    return bool((get_env_value("PINGRAM_API_KEY") or "").strip())


def prompt_region_and_api_key(*, header: str, skip_header: bool = False) -> Tuple[Optional[str], Optional[str]]:
    from hermes_cli.setup import (
        get_env_value,
        print_header,
        print_info,
        print_warning,
        prompt,
        prompt_choice,
        prompt_yes_no,
        save_env_value,
    )

    if not skip_header:
        print_header(header)
    else:
        print()
        print_info(f"Pingram credentials ({header}).")

    if shared_credentials_configured():
        print_info("Shared Pingram region and API key are already set.")
        if prompt_yes_no("  Re-use existing Pingram region and API key?", True):
            api_key = (get_env_value("PINGRAM_API_KEY") or "").strip()
            region = (get_env_value("PINGRAM_REGION") or "us").strip().lower()
            return region, api_key

    print_info("You need your Pingram region and API key — other settings use sensible defaults.")
    print()
    print_info("Pick the region your Pingram account lives in (selects the API endpoint).")
    regions = ["us", "eu", "ca"]
    current_region = (get_env_value("PINGRAM_REGION") or "us").strip().lower()
    default_idx = regions.index(current_region) if current_region in regions else 0
    region = regions[prompt_choice("Region", ["US (Default)", "EU", "CA"], default_idx)]
    save_env_value("PINGRAM_REGION", region)

    existing_key = (get_env_value("PINGRAM_API_KEY") or "").strip()
    api_key = prompt("Pingram API key (pingram_sk_...)", default=existing_key, password=not existing_key)
    if not api_key:
        print_warning("API key is required — skipping setup.")
        return None, None
    save_env_value("PINGRAM_API_KEY", api_key.strip())
    return region, api_key.strip()


def prompt_setup_mode() -> str:
    from hermes_cli.setup import print_info, prompt_choice

    print()
    print_info("How do you want to set this up?")
    mode = prompt_choice(
        "Setup mode",
        ["Quick setup (recommended)", "Advanced configuration"],
        0,
    )
    return "quick" if mode == 0 else "advanced"


def prompt_sms_phone(*, label: str, default: str = "", allow_empty: bool = False) -> Optional[str]:
    from hermes_cli.setup import print_warning, prompt

    while True:
        phone_raw = prompt(label, default=default)
        if allow_empty and not (phone_raw or "").strip():
            return None
        phone = normalize_phone_e164(phone_raw)
        if phone:
            return phone
        print_warning("Please enter a valid phone number (E.164, e.g. +15005005000).")


def prompt_email_address(*, label: str, default: str = "") -> str:
    from hermes_cli.setup import print_warning, prompt

    while True:
        email_raw = prompt(label, default=default)
        email = norm_email(email_raw)
        if email and "@" in email:
            return email
        print_warning("Please enter a valid email address.")


def prompt_sms_allowlist(*, default: str = "") -> List[str]:
    from hermes_cli.setup import print_info, print_warning, prompt

    print_info("Only these phone numbers may text your agent (comma-separated, E.164).")
    while True:
        raw = prompt("Allowed phone numbers", default=default)
        phones = []
        for part in parse_csv(raw):
            phone = normalize_phone_e164(part)
            if phone:
                phones.append(phone)
        if phones:
            return phones
        print_warning("Enter at least one valid phone number.")


def prompt_email_allowlist(*, default: str = "") -> List[str]:
    from hermes_cli.setup import print_info, print_warning, prompt

    print_info("Only these email addresses may reach your agent (comma-separated).")
    while True:
        raw = prompt("Allowed email addresses", default=default)
        emails = []
        for part in parse_csv(raw):
            email = norm_email(part)
            if email and "@" in email:
                emails.append(email)
        if emails:
            return emails
        print_warning("Enter at least one valid email address.")


def prompt_from_sms(*, numbers: List[str], default: str = "", advanced: bool, exclude: str = "") -> Optional[str]:
    from hermes_cli.setup import prompt_yes_no

    existing = (default or "").strip()
    if exclude and existing == (exclude or "").strip():
        existing = ""
    if not advanced:
        if existing:
            return existing
        return numbers[0] if numbers else None

    if numbers:
        from hermes_cli.setup import prompt_choice

        labels = numbers + ["Other (enter manually)"]
        default_idx = numbers.index(existing) if existing in numbers else 0
        choice = prompt_choice("Outbound SMS sender number", labels, default_idx)
        if choice < len(numbers):
            return numbers[choice]
        manual_default = existing if existing and existing not in numbers else ""
        return prompt_sms_phone(label="Outbound SMS sender number (E.164)", default=manual_default)

    if existing:
        if prompt_yes_no(f"  Keep outbound sender {existing}?", True):
            return existing
    optional = prompt_sms_phone(
        label="Outbound SMS sender number (E.164, leave blank to skip)",
        default="",
        allow_empty=True,
    )
    return optional


def prompt_from_email(
    *,
    emails: List[str],
    default: str = "",
    advanced: bool,
) -> Tuple[Optional[str], Optional[str]]:
    from hermes_cli.setup import prompt, prompt_choice, prompt_yes_no

    existing = (default or "").strip()
    from_email: Optional[str] = None

    if not advanced:
        from_email = existing or (emails[0] if emails else None)
    elif emails:
        labels = emails + ["Other (enter manually)"]
        default_idx = emails.index(existing) if existing in emails else 0
        choice = prompt_choice("Outbound email sender address", labels, default_idx)
        if choice < len(emails):
            from_email = emails[choice]
        else:
            manual_default = existing if existing and existing not in emails else ""
            from_email = prompt_email_address(label="Outbound email sender address", default=manual_default)
    elif existing:
        if prompt_yes_no(f"  Keep outbound sender {existing}?", True):
            from_email = existing
        else:
            from_email = prompt_email_address(label="Outbound email sender address", default="")
    else:
        from_email = prompt_email_address(label="Outbound email sender address (optional)", default="")

    from_name: Optional[str] = None
    if advanced:
        from hermes_cli.setup import get_env_value, prompt

        existing_name = (get_env_value("PINGRAM_FROM_NAME") or DEFAULT_FROM_NAME).strip()
        from_name = prompt("Display name on outbound emails", default=existing_name).strip() or DEFAULT_FROM_NAME

    return from_email, from_name


def existing_allow_all_default() -> bool:
    from hermes_cli.setup import get_env_value

    from pingram_gateway.core.helpers import truthy

    return truthy(get_env_value("PINGRAM_ALLOW_ALL_USERS"))


def prompt_allow_all_senders(*, default: bool = False) -> bool:
    from hermes_cli.setup import print_info, prompt_yes_no

    print()
    print_info("When enabled, anyone who texts or emails your Pingram address can reach your agent.")
    return prompt_yes_no("Allow anyone to reach your agent?", default)


def prompt_use_as_home_channel(*, channel_label: str, default: bool = True) -> bool:
    from hermes_cli.setup import print_info, prompt_yes_no

    print()
    print_info(
        f"Hermes uses a platform's home channel for scheduled/cron delivery and "
        f"default proactive send_message targets."
    )
    return prompt_yes_no(f"Use {channel_label} as a Hermes home channel?", default)


def prompt_home_channel_sms(*, allowed: List[str], default: str = "") -> str:
    from hermes_cli.setup import prompt_choice

    if len(allowed) == 1:
        return allowed[0]
    default_idx = allowed.index(default) if default in allowed else 0
    choice = prompt_choice("Default delivery phone number", allowed, default_idx)
    return allowed[choice]


def prompt_home_channel_email(*, allowed: List[str], default: str = "") -> str:
    from hermes_cli.setup import prompt_choice

    if len(allowed) == 1:
        return allowed[0]
    default_idx = allowed.index(default) if default in allowed else 0
    choice = prompt_choice("Default delivery email address", allowed, default_idx)
    return allowed[choice]


def save_home_channel_env(env_key: str, chat_id: Optional[str]) -> None:
    from hermes_cli.setup import save_env_value

    save_env_value(env_key, (chat_id or "").strip())


def save_allowlist_env(key: str, values: List[str]) -> None:
    from hermes_cli.setup import save_env_value

    save_env_value(key, ",".join(values) if values else "")


def seed_display_overrides(load_config, save_config, platform_key: str) -> None:
    try:
        config = load_config()
        display = config.setdefault("display", {})
        platforms = display.setdefault("platforms", {})
        plat_cfg = platforms.setdefault(platform_key, {})
        if not isinstance(plat_cfg, dict):
            return
        changed = False
        for key, value in DISPLAY_OVERRIDES.items():
            if key not in plat_cfg:
                plat_cfg[key] = value
                changed = True
        if changed:
            save_config(config)
    except Exception:
        logger.debug("Pingram: failed to seed display overrides for %s", platform_key, exc_info=True)


def enable_gateway_platform(load_config, save_config, platform_key: str) -> None:
    try:
        config = load_config()
        gateway = config.setdefault("gateway", {})
        platforms: Dict[str, Any] = gateway.setdefault("platforms", {})
        entry = platforms.setdefault(platform_key, {})
        if isinstance(entry, dict):
            entry["enabled"] = True
        save_config(config)
    except Exception:
        logger.debug("Pingram: failed to enable platform %s in config", platform_key, exc_info=True)


def set_gateway_home_channel(platform_key: str, chat_id: Optional[str]) -> None:
    try:
        from gateway.config import HomeChannel, Platform, load_gateway_config

        config = load_gateway_config()
        platform = Platform(platform_key)
        pconfig = config.platforms.get(platform)
        if pconfig is None:
            return
        if chat_id:
            pconfig.home_channel = HomeChannel(platform=platform, chat_id=chat_id, name="Home")
        else:
            pconfig.home_channel = None
        save_fn = getattr(config, "save", None) or getattr(
            __import__("gateway.config", fromlist=["save_gateway_config"]),
            "save_gateway_config",
            None,
        )
        if callable(save_fn):
            save_fn(config)
    except Exception:
        logger.debug("Pingram: failed to set home channel for %s", platform_key, exc_info=True)
