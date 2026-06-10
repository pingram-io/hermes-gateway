"""Shared setup wizard helpers."""

import logging
from typing import Any, Dict, Optional, Tuple

from pingram_gateway.core.constants import DISPLAY_OVERRIDES

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


def shared_credentials_configured() -> bool:
    from hermes_cli.setup import get_env_value

    return bool((get_env_value("PINGRAM_API_KEY") or "").strip())


def prompt_region_and_api_key(*, header: str, reconfigure_default: bool = False) -> Tuple[Optional[str], Optional[str]]:
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

    print_header(header)

    if shared_credentials_configured():
        print_info("Shared Pingram region and API key are already set.")
        if not prompt_yes_no("  Reconfigure region and API key?", reconfigure_default):
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
