"""Pingram Voice beta signup wizard."""

from pingram_gateway.core.constants import DEFAULT_FROM_NAME, VOICE_BETA_MESSAGE
from pingram_gateway.core.helpers import norm_email
from pingram_gateway.core.send import fetch_account_identities, send_voice_beta_signup
from pingram_gateway.core.setup_common import prompt_region_and_api_key, shared_credentials_configured


def setup_voice() -> None:
    from hermes_cli.setup import (
        color,
        Colors,
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt,
        prompt_yes_no,
    )

    print_header("Pingram Voice")
    print()
    print_info(VOICE_BETA_MESSAGE)
    print()
    if not prompt_yes_no("Would you like to enter the Voice beta?", False):
        print_info("No problem — run this setup again anytime you're ready.")
        return

    print()
    print_info("Please enter your email. We'll let you know when we enable voice on your account.")
    while True:
        email_raw = prompt("Your email address")
        email = norm_email(email_raw)
        if email and "@" in email:
            break
        print_warning("Please enter a valid email address.")

    if shared_credentials_configured():
        region = (get_env_value("PINGRAM_REGION") or "us").strip().lower()
        api_key = (get_env_value("PINGRAM_API_KEY") or "").strip()
    else:
        print()
        region, api_key = prompt_region_and_api_key(header="Pingram Voice beta")
        if not region or not api_key:
            return

    from_email = (get_env_value("PINGRAM_FROM_EMAIL") or "").strip()
    if not from_email:
        account_emails, _numbers = fetch_account_identities(api_key, region)
        if account_emails:
            from_email = account_emails[0]

    print()
    print_info("Submitting your beta signup...")
    if send_voice_beta_signup(
        api_key,
        region,
        email,
        from_email=from_email,
        from_name=(get_env_value("PINGRAM_FROM_NAME") or DEFAULT_FROM_NAME).strip(),
    ):
        print()
        print_success("You're on the Voice beta waitlist!")
        print_info(f"We'll reach out at {email} when voice is enabled on your account.")
    else:
        print_warning(
            "Couldn't submit your signup just now. Try again later or email hello@pingram.io directly."
        )
