"""Pingram SMS setup wizard."""

from typing import Optional

from pingram_gateway.core.constants import PLATFORM_SMS
from pingram_gateway.core.helpers import parse_csv
from pingram_gateway.core.send import fetch_account_identities, send_welcome_sms
from pingram_gateway.core.setup_common import (
    enable_gateway_platform,
    existing_allow_all_default,
    prompt_allow_all_senders,
    prompt_from_sms,
    prompt_home_channel_sms,
    prompt_region_and_api_key,
    prompt_setup_mode,
    prompt_sms_allowlist,
    prompt_sms_phone,
    prompt_use_as_home_channel,
    save_allowlist_env,
    save_home_channel_env,
    seed_display_overrides,
    set_gateway_home_channel,
    sms_platform_configured,
)


def setup_sms() -> None:
    from hermes_cli.setup import (
        color,
        Colors,
        get_env_value,
        load_config,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt_yes_no,
        save_config,
        save_env_value,
    )

    if sms_platform_configured() and not prompt_yes_no("Pingram SMS is already configured. Reconfigure?", False):
        return

    print_header("Pingram SMS")
    mode = prompt_setup_mode()

    region, api_key = prompt_region_and_api_key(header="Pingram SMS", skip_header=True)
    if not region or not api_key:
        return

    existing_allowed = parse_csv(get_env_value("PINGRAM_SMS_ALLOWED_USERS") or "")
    existing_home = (get_env_value("PINGRAM_SMS_HOME_CHANNEL") or "").strip()
    default_allowed = ",".join(existing_allowed)
    advanced = mode == "advanced"

    print()
    home_channel: Optional[str] = None
    welcome_to: Optional[str] = None

    if not advanced:
        contact = prompt_sms_phone(
            label="Your phone number (E.164, e.g. +15005005000)",
            default=existing_home or (existing_allowed[0] if existing_allowed else ""),
        )
        save_allowlist_env("PINGRAM_SMS_ALLOWED_USERS", [contact])
        save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")
        home_channel = contact
        welcome_to = contact
    else:
        allow_all = prompt_allow_all_senders(default=existing_allow_all_default())
        if allow_all:
            save_allowlist_env("PINGRAM_SMS_ALLOWED_USERS", [])
            save_env_value("PINGRAM_ALLOW_ALL_USERS", "true")
            contact = prompt_sms_phone(
                label="Your phone number (E.164, e.g. +15005005000)",
                default=existing_home or (existing_allowed[0] if existing_allowed else ""),
            )
            delivery_choices = [contact]
            welcome_to = contact
        else:
            save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")
            delivery_choices = prompt_sms_allowlist(default=default_allowed)
            save_allowlist_env("PINGRAM_SMS_ALLOWED_USERS", delivery_choices)
            welcome_to = delivery_choices[0]
        if prompt_use_as_home_channel(channel_label="SMS", default=True):
            if len(delivery_choices) == 1:
                home_channel = delivery_choices[0]
            else:
                home_channel = prompt_home_channel_sms(
                    allowed=delivery_choices,
                    default=existing_home or delivery_choices[0],
                )
    save_home_channel_env("PINGRAM_SMS_HOME_CHANNEL", home_channel)

    print_info("Checking your Pingram account...")
    _emails, numbers = fetch_account_identities(api_key, region)

    from_sms = prompt_from_sms(
        numbers=numbers,
        default=(get_env_value("PINGRAM_FROM_SMS") or "").strip(),
        advanced=advanced,
    )
    if from_sms:
        save_env_value("PINGRAM_FROM_SMS", from_sms)
    elif not advanced:
        save_env_value("PINGRAM_FROM_SMS", "")

    seed_display_overrides(load_config, save_config, PLATFORM_SMS)
    enable_gateway_platform(load_config, save_config, PLATFORM_SMS)
    set_gateway_home_channel(PLATFORM_SMS, home_channel)

    print()
    print_success("Pingram SMS configured!")
    if numbers:
        print()
        print(color("  📱 Your agent is reachable by SMS at:", Colors.BOLD, Colors.MAGENTA))
        for number in numbers:
            print(color(f"     ➜  {number}", Colors.BOLD, Colors.GREEN))
    if home_channel:
        print_info("SMS is set as a Hermes home channel (cron and default proactive delivery).")
    else:
        print_info("SMS is not a Hermes home channel — use explicit pingram-sms:+1… targets for outbound.")

    if welcome_to:
        print()
        print_info("Sending you a hello text...")
        if send_welcome_sms(api_key, region, welcome_to, from_sms=(from_sms or "")):
            print(color("  💬 Sent — check your phone!", Colors.BOLD, Colors.GREEN))
        else:
            print_warning("Couldn't send a welcome SMS just now — try texting your agent once the gateway is running.")
