"""Pingram Email setup wizard."""

from pingram_gateway.core.constants import DEFAULT_FROM_NAME, PLATFORM_EMAIL
from pingram_gateway.core.helpers import norm_email, parse_csv
from pingram_gateway.core.send import fetch_account_identities, send_welcome_email
from pingram_gateway.core.setup_common import (
    enable_gateway_platform,
    email_platform_configured,
    prompt_region_and_api_key,
    seed_display_overrides,
)


def setup_email() -> None:
    from hermes_cli.setup import (
        color,
        Colors,
        get_env_value,
        load_config,
        print_info,
        print_success,
        print_warning,
        prompt,
        prompt_yes_no,
        save_config,
        save_env_value,
    )

    if email_platform_configured() and not prompt_yes_no("Pingram Email is already configured. Reconfigure?", False):
        return

    region, api_key = prompt_region_and_api_key(header="Pingram Email")
    if not region or not api_key:
        return

    print()
    print_info("Tell Hermes which email address may reach your agent.")
    existing = parse_csv(get_env_value("PINGRAM_EMAIL_ALLOWED_USERS") or "")
    default_email = existing[0] if existing else (get_env_value("PINGRAM_EMAIL_HOME_CHANNEL") or "")
    while True:
        email_raw = prompt("Your email address", default=default_email)
        email = norm_email(email_raw)
        if email and "@" in email:
            break
        print_warning("Please enter a valid email address.")
    save_env_value("PINGRAM_EMAIL_ALLOWED_USERS", email)
    save_env_value("PINGRAM_EMAIL_HOME_CHANNEL", email)
    save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")

    print_info("Checking your Pingram account...")
    account_emails, _numbers = fetch_account_identities(api_key, region)
    if account_emails and not (get_env_value("PINGRAM_FROM_EMAIL") or "").strip():
        save_env_value("PINGRAM_FROM_EMAIL", account_emails[0])

    from_email = prompt(
        "Optional: sender email address (leave blank for Pingram default)",
        default=get_env_value("PINGRAM_FROM_EMAIL") or "",
    )
    if from_email.strip():
        save_env_value("PINGRAM_FROM_EMAIL", norm_email(from_email))
    from_name = prompt(
        "Email sender display name",
        default=get_env_value("PINGRAM_FROM_NAME") or DEFAULT_FROM_NAME,
    )
    if from_name.strip():
        save_env_value("PINGRAM_FROM_NAME", from_name.strip())

    seed_display_overrides(load_config, save_config, PLATFORM_EMAIL)
    enable_gateway_platform(load_config, save_config, PLATFORM_EMAIL)

    print()
    print_success("Pingram Email configured!")
    if account_emails:
        print()
        print(color("  📧 Your agent is reachable by email at:", Colors.BOLD, Colors.MAGENTA))
        for addr in account_emails:
            print(color(f"     ➜  {addr}", Colors.BOLD, Colors.GREEN))

    print()
    print_info("Sending you a hello email...")
    if send_welcome_email(
        api_key,
        region,
        email,
        from_email=(get_env_value("PINGRAM_FROM_EMAIL") or "").strip(),
        from_name=(get_env_value("PINGRAM_FROM_NAME") or DEFAULT_FROM_NAME).strip(),
    ):
        print(color("  💬 Sent — check your inbox!", Colors.BOLD, Colors.GREEN))
    else:
        print_warning("Couldn't send a welcome email just now — try emailing your agent once the gateway is running.")
