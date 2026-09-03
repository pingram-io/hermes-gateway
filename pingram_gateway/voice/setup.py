"""Pingram Voice setup wizard — outbound Voice Agent calls."""

from typing import Optional

from pingram_gateway.core.constants import PLATFORM_VOICE
from pingram_gateway.core.helpers import parse_csv
from pingram_gateway.core.send import fetch_voice_agents, send_welcome_voice_call
from pingram_gateway.core.setup_common import (
    enable_gateway_platform,
    existing_allow_all_default,
    prompt_allow_all_senders,
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
    voice_platform_configured,
)


def setup_voice() -> None:
    from hermes_cli.setup import (
        color,
        Colors,
        get_env_value,
        load_config,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt_choice,
        prompt_yes_no,
        save_config,
        save_env_value,
    )

    if voice_platform_configured() and not prompt_yes_no("Pingram Voice is already configured. Reconfigure?", False):
        return

    print_header("Pingram Voice")
    print()
    print_info(
        "Voice starts a live Pingram Voice Agent call. Hermes briefs the agent; "
        "Pingram hosts the two-way conversation on the phone. This is not a one-way "
        "text-to-speech notification."
    )
    mode = prompt_setup_mode()

    region, api_key = prompt_region_and_api_key(header="Pingram Voice", skip_header=True)
    if not region or not api_key:
        return

    existing_allowed = parse_csv(get_env_value("PINGRAM_VOICE_ALLOWED_USERS") or "")
    existing_home = (get_env_value("PINGRAM_VOICE_HOME_CHANNEL") or "").strip()
    sms_home = (get_env_value("PINGRAM_SMS_HOME_CHANNEL") or "").strip()
    default_contact = existing_home or (existing_allowed[0] if existing_allowed else "") or sms_home
    default_allowed = ",".join(existing_allowed)
    advanced = mode == "advanced"

    print()
    home_channel: Optional[str] = None
    welcome_to: Optional[str] = None

    if not advanced:
        contact = prompt_sms_phone(
            label="Phone number to call (E.164, e.g. +15005005000)",
            default=default_contact,
        )
        save_allowlist_env("PINGRAM_VOICE_ALLOWED_USERS", [contact])
        save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")
        welcome_to = contact
        save_home_channel_env("PINGRAM_VOICE_HOME_CHANNEL", contact)
        # Explicit pingram-voice sends use the env number; do not make Voice a
        # Hermes home/cron channel or every proactive reply will ring the phone.
        home_channel = None
    else:
        allow_all = prompt_allow_all_senders(default=existing_allow_all_default())
        if allow_all:
            save_allowlist_env("PINGRAM_VOICE_ALLOWED_USERS", [])
            save_env_value("PINGRAM_ALLOW_ALL_USERS", "true")
            contact = prompt_sms_phone(
                label="Phone number to call (E.164, e.g. +15005005000)",
                default=default_contact,
            )
            delivery_choices = [contact]
            welcome_to = contact
        else:
            save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")
            print_info("Only these numbers may be called (comma-separated, E.164).")
            delivery_choices = prompt_sms_allowlist(default=default_allowed or default_contact)
            save_allowlist_env("PINGRAM_VOICE_ALLOWED_USERS", delivery_choices)
            welcome_to = delivery_choices[0]
        if prompt_use_as_home_channel(channel_label="Voice", default=False):
            if len(delivery_choices) == 1:
                home_channel = delivery_choices[0]
            else:
                home_channel = prompt_home_channel_sms(
                    allowed=delivery_choices,
                    default=existing_home or delivery_choices[0],
                )
        save_home_channel_env("PINGRAM_VOICE_HOME_CHANNEL", home_channel or welcome_to)

    print()
    print_info("Checking your Pingram Voice Agents...")
    agents = fetch_voice_agents(api_key, region)
    existing_agent = (get_env_value("PINGRAM_VOICE_AGENT_ID") or "").strip()
    agent_id = ""
    if agents:
        labels = ["Default Hermes spec (no saved agent)"] + [
            f"{a['name']} ({a['agent_id']})" for a in agents
        ]
        default_idx = 0
        if existing_agent:
            for i, agent in enumerate(agents):
                if agent["agent_id"] == existing_agent:
                    default_idx = i + 1
                    break
        choice = prompt_choice("Voice Agent for outbound calls", labels, default_idx)
        if choice > 0:
            agent_id = agents[choice - 1]["agent_id"]
    else:
        print_info(
            "No saved Voice Agents on this account — outbound calls will use a default "
            "Hermes conversational spec. You can create agents in the Pingram dashboard."
        )
        if existing_agent:
            save_env_value("PINGRAM_VOICE_AGENT_ID", "")
    save_env_value("PINGRAM_VOICE_AGENT_ID", agent_id)

    seed_display_overrides(load_config, save_config, PLATFORM_VOICE)
    enable_gateway_platform(load_config, save_config, PLATFORM_VOICE)
    set_gateway_home_channel(PLATFORM_VOICE, home_channel)

    print()
    print_success("Pingram Voice configured!")
    if home_channel:
        print_info("Voice is set as a Hermes home channel — cron and default proactive delivery will place calls.")
    else:
        print_info(
            "Voice is not a Hermes home channel. Hermes will only call when you explicitly "
            "ask it to (send_message to pingram-voice)."
        )
    if agent_id:
        print_info(f"Outbound calls use saved Voice Agent {agent_id}.")
    else:
        print_info("Outbound calls use the default Hermes Voice Agent spec.")

    if welcome_to and prompt_yes_no("Place a short test Voice Agent call now?", False):
        print()
        print_info("Calling you...")
        if send_welcome_voice_call(api_key, region, welcome_to, agent_id=agent_id or None):
            print(color("  📞 Placed — your phone should ring shortly.", Colors.BOLD, Colors.GREEN))
        else:
            print_warning(
                "Couldn't place a test call just now — try send_message to pingram-voice once the gateway is running."
            )
