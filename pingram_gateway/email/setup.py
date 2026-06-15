"""Pingram Email setup wizard."""

from typing import Optional

from pingram_gateway.core.constants import DEFAULT_FROM_NAME, PLATFORM_EMAIL
from pingram_gateway.core.helpers import parse_csv
from pingram_gateway.core.send import fetch_account_identities, send_welcome_email
from pingram_gateway.core.setup_common import (
    enable_gateway_platform,
    email_platform_configured,
    existing_allow_all_default,
    prompt_allow_all_senders,
    prompt_email_address,
    prompt_email_allowlist,
    prompt_from_email,
    prompt_home_channel_email,
    prompt_region_and_api_key,
    prompt_setup_mode,
    prompt_use_as_home_channel,
    save_allowlist_env,
    save_home_channel_env,
    seed_display_overrides,
    set_gateway_home_channel,
)


def setup_email() -> None:
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

    if email_platform_configured() and not prompt_yes_no("Pingram Email is already configured. Reconfigure?", False):
        return

    print_header("Pingram Email")
    mode = prompt_setup_mode()

    region, api_key = prompt_region_and_api_key(header="Pingram Email", skip_header=True)
    if not region or not api_key:
        return

    existing_allowed = parse_csv(get_env_value("PINGRAM_EMAIL_ALLOWED_USERS") or "")
    existing_home = (get_env_value("PINGRAM_EMAIL_HOME_CHANNEL") or "").strip()
    default_allowed = ",".join(existing_allowed)
    advanced = mode == "advanced"

    print()
    home_channel: Optional[str] = None
    welcome_to: Optional[str] = None

    if not advanced:
        contact = prompt_email_address(
            label="Your email address",
            default=existing_home or (existing_allowed[0] if existing_allowed else ""),
        )
        save_allowlist_env("PINGRAM_EMAIL_ALLOWED_USERS", [contact])
        save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")
        home_channel = contact
        welcome_to = contact
    else:
        allow_all = prompt_allow_all_senders(default=existing_allow_all_default())
        if allow_all:
            save_allowlist_env("PINGRAM_EMAIL_ALLOWED_USERS", [])
            save_env_value("PINGRAM_ALLOW_ALL_USERS", "true")
            contact = prompt_email_address(
                label="Your email address",
                default=existing_home or (existing_allowed[0] if existing_allowed else ""),
            )
            delivery_choices = [contact]
            welcome_to = contact
        else:
            save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")
            delivery_choices = prompt_email_allowlist(default=default_allowed)
            save_allowlist_env("PINGRAM_EMAIL_ALLOWED_USERS", delivery_choices)
            welcome_to = delivery_choices[0]
        if prompt_use_as_home_channel(channel_label="Email", default=True):
            if len(delivery_choices) == 1:
                home_channel = delivery_choices[0]
            else:
                home_channel = prompt_home_channel_email(
                    allowed=delivery_choices,
                    default=existing_home or delivery_choices[0],
                )
    save_home_channel_env("PINGRAM_EMAIL_HOME_CHANNEL", home_channel)

    print_info("Checking your Pingram account...")
    account_emails, _, _ = fetch_account_identities(api_key, region)

    from_email, from_name = prompt_from_email(
        emails=account_emails,
        default=(get_env_value("PINGRAM_FROM_EMAIL") or "").strip(),
        advanced=advanced,
    )
    if from_email:
        save_env_value("PINGRAM_FROM_EMAIL", from_email)
    elif not advanced and account_emails:
        save_env_value("PINGRAM_FROM_EMAIL", account_emails[0])
    if from_name:
        save_env_value("PINGRAM_FROM_NAME", from_name)

    seed_display_overrides(load_config, save_config, PLATFORM_EMAIL)
    enable_gateway_platform(load_config, save_config, PLATFORM_EMAIL)
    set_gateway_home_channel(PLATFORM_EMAIL, home_channel)

    print()
    print_success("Pingram Email configured!")
    if account_emails:
        print()
        print(color("  📧 Your agent is reachable by email at:", Colors.BOLD, Colors.MAGENTA))
        for addr in account_emails:
            print(color(f"     ➜  {addr}", Colors.BOLD, Colors.GREEN))
    if home_channel:
        print_info("Email is set as a Hermes home channel (cron and default proactive delivery).")
    else:
        print_info("Email is not a Hermes home channel — use explicit pingram-email:addr@… targets for outbound.")

    if welcome_to:
        print()
        print_info("Sending you a hello email...")
        if send_welcome_email(
            api_key,
            region,
            welcome_to,
            from_email=(from_email or (get_env_value("PINGRAM_FROM_EMAIL") or "").strip()),
            from_name=(from_name or (get_env_value("PINGRAM_FROM_NAME") or DEFAULT_FROM_NAME).strip()),
        ):
            print(color("  💬 Sent — check your inbox!", Colors.BOLD, Colors.GREEN))
        else:
            print_warning("Couldn't send a welcome email just now — try emailing your agent once the gateway is running.")
