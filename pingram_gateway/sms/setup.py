"""Pingram SMS setup wizard."""

from pingram_gateway.core.constants import PLATFORM_SMS
from pingram_gateway.core.helpers import normalize_phone_e164, parse_csv
from pingram_gateway.core.send import fetch_account_identities, send_welcome_sms
from pingram_gateway.core.setup_common import enable_gateway_platform, prompt_region_and_api_key, seed_display_overrides


def setup_sms() -> None:
    from hermes_cli.setup import (
        color,
        Colors,
        get_env_value,
        load_config,
        print_info,
        print_success,
        print_warning,
        prompt,
        save_config,
        save_env_value,
    )

    region, api_key = prompt_region_and_api_key(header="Pingram SMS")
    if not region or not api_key:
        return

    print()
    print_info("Tell Hermes which phone number may text your agent.")
    existing = parse_csv(get_env_value("PINGRAM_SMS_ALLOWED_USERS") or "")
    default_phone = existing[0] if existing else (get_env_value("PINGRAM_SMS_HOME_CHANNEL") or "")
    while True:
        phone_raw = prompt("Your phone number (E.164, e.g. +15005005000)", default=default_phone)
        phone = normalize_phone_e164(phone_raw)
        if phone:
            break
        print_warning("Please enter a valid phone number.")
    save_env_value("PINGRAM_SMS_ALLOWED_USERS", phone)
    save_env_value("PINGRAM_SMS_HOME_CHANNEL", phone)
    save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")

    from_sms = prompt(
        "Optional: pin a specific Pingram sender number (leave blank for account default)",
        default=get_env_value("PINGRAM_FROM_SMS") or "",
    )
    if from_sms.strip():
        save_env_value("PINGRAM_FROM_SMS", normalize_phone_e164(from_sms))

    print_info("Checking your Pingram account...")
    _emails, numbers = fetch_account_identities(api_key, region)

    seed_display_overrides(load_config, save_config, PLATFORM_SMS)
    enable_gateway_platform(load_config, save_config, PLATFORM_SMS)

    print()
    print_success("Pingram SMS configured!")
    if numbers:
        print()
        print(color("  📱 Your agent is reachable by SMS at:", Colors.BOLD, Colors.MAGENTA))
        for number in numbers:
            print(color(f"     ➜  {number}", Colors.BOLD, Colors.GREEN))

    print()
    print_info("Sending you a hello text...")
    if send_welcome_sms(api_key, region, phone, from_sms=(get_env_value("PINGRAM_FROM_SMS") or "").strip()):
        print(color("  💬 Sent — check your phone!", Colors.BOLD, Colors.GREEN))
    else:
        print_warning("Couldn't send a welcome SMS just now — try texting your agent once the gateway is running.")
