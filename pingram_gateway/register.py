"""Register Pingram SMS, Email, and Voice Hermes platforms."""

from pingram_gateway.core.config import channel_env_seed, email_configured, sms_configured, voice_configured
from pingram_gateway.core.constants import PLATFORM_EMAIL, PLATFORM_SMS, PLATFORM_VOICE
from pingram_gateway.core.helpers import check_shared_requirements
from pingram_gateway.core.send_message import install_send_message_parsers
from pingram_gateway.core.session_context import install_session_context_hook
from pingram_gateway.email.adapter import PingramEmailAdapter, standalone_send_email
from pingram_gateway.email.setup import setup_email
from pingram_gateway.sms.adapter import PingramSmsAdapter, standalone_send_sms
from pingram_gateway.sms.setup import setup_sms
from pingram_gateway.voice.adapter import PingramVoiceAdapter, standalone_send_voice
from pingram_gateway.voice.setup import setup_voice

INSTALL_HINT = "hermes plugins install pingram-io/hermes-gateway  (SDK auto-installs on first run)"

SMS_HINT = (
    "You are chatting over SMS via Pingram (pingram-sms). Reply in plain text only "
    "(no markdown), keep messages short (~160 characters per segment), and avoid links "
    "when possible. Inbound MMS images are provided as media. When the user asks to "
    "send an SMS (including from CLI), call send_message with target='pingram-sms' and "
    "the message — this uses PINGRAM_SMS_HOME_CHANNEL. Do not suggest Twilio or other "
    "SMS providers. Never use channel-directory display labels like '***8196'. "
    "You can also use 'pingram-sms:+15551234567' for a specific number."
)

EMAIL_HINT = (
    "You are chatting over Email via Pingram. Write outbound email bodies as HTML "
    "fragments (<p>, <strong>, <em>, <ul>/<li>, <br>) — never markdown (**bold**, "
    "# headings, - lists). For proactive emails (including from CLI), use send_message "
    "with target 'pingram-email', set subject to a short specific summary (no 'Re:' on "
    "new threads), and put HTML in message. Replies in an existing inbound thread are "
    "threaded automatically with Re:. Inbound email attachments are not available when "
    "polling. Never use channel-directory subject/topic labels. You can also use "
    "'pingram-email:user@example.com'."
)

VOICE_HINT = (
    "You start a live Pingram Voice Agent call via pingram-voice. Pingram hosts the "
    "two-way conversation on the phone; your send_message text is a briefing for the "
    "agent, not a script to read verbatim. When the call ends, you will receive a "
    "Pingram Voice call-ended message with outcome and transcript — use that to know "
    "whether they picked up and what was said. When the user asks you to call them, "
    "use send_message with target='pingram-voice'. You can also use "
    "'pingram-voice:+15551234567'. Do not use the CALL notification channel, Twilio, "
    "or other voice providers."
)


def _sms_env_enablement():
    import os

    seed = channel_env_seed("PINGRAM_SMS_HOME_CHANNEL", "PINGRAM_SMS_HOME_CHANNEL_NAME")
    if seed is None:
        return None
    allowed = os.getenv("PINGRAM_SMS_ALLOWED_USERS", "").strip()
    if allowed:
        seed["allowed_users"] = allowed
    from_sms = os.getenv("PINGRAM_FROM_SMS", "").strip()
    if from_sms:
        seed["from_sms"] = from_sms
    return seed


def _voice_env_enablement():
    import os

    seed = channel_env_seed("PINGRAM_VOICE_HOME_CHANNEL", "PINGRAM_VOICE_HOME_CHANNEL_NAME")
    if seed is None:
        return None
    allowed = os.getenv("PINGRAM_VOICE_ALLOWED_USERS", "").strip()
    if allowed:
        seed["allowed_users"] = allowed
    agent_id = os.getenv("PINGRAM_VOICE_AGENT_ID", "").strip()
    if agent_id:
        seed["voice_agent_id"] = agent_id
    return seed


def _email_env_enablement():
    import os

    seed = channel_env_seed("PINGRAM_EMAIL_HOME_CHANNEL", "PINGRAM_EMAIL_HOME_CHANNEL_NAME")
    if seed is None:
        return None
    allowed = os.getenv("PINGRAM_EMAIL_ALLOWED_USERS", "").strip()
    if allowed:
        seed["allowed_users"] = allowed
    for env_key, extra_key in (
        ("PINGRAM_FROM_EMAIL", "from_email"),
        ("PINGRAM_FROM_NAME", "from_name"),
    ):
        val = os.getenv(env_key, "").strip()
        if val:
            seed[extra_key] = val
    return seed


def register(ctx):
    """Plugin entry point."""
    ctx.register_platform(
        name=PLATFORM_SMS,
        label="Pingram SMS",
        adapter_factory=lambda cfg: PingramSmsAdapter(cfg),
        check_fn=check_shared_requirements,
        validate_config=sms_configured,
        is_connected=sms_configured,
        required_env=["PINGRAM_API_KEY", "PINGRAM_REGION"],
        install_hint=INSTALL_HINT,
        env_enablement_fn=_sms_env_enablement,
        setup_fn=setup_sms,
        allowed_users_env="PINGRAM_SMS_ALLOWED_USERS",
        allow_all_env="PINGRAM_ALLOW_ALL_USERS",
        cron_deliver_env_var="PINGRAM_SMS_HOME_CHANNEL",
        standalone_sender_fn=standalone_send_sms,
        emoji="📱",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=SMS_HINT,
    )

    ctx.register_platform(
        name=PLATFORM_EMAIL,
        label="Pingram Email",
        adapter_factory=lambda cfg: PingramEmailAdapter(cfg),
        check_fn=check_shared_requirements,
        validate_config=email_configured,
        is_connected=email_configured,
        required_env=["PINGRAM_API_KEY", "PINGRAM_REGION"],
        install_hint=INSTALL_HINT,
        env_enablement_fn=_email_env_enablement,
        setup_fn=setup_email,
        allowed_users_env="PINGRAM_EMAIL_ALLOWED_USERS",
        allow_all_env="PINGRAM_ALLOW_ALL_USERS",
        cron_deliver_env_var="PINGRAM_EMAIL_HOME_CHANNEL",
        standalone_sender_fn=standalone_send_email,
        emoji="📧",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=EMAIL_HINT,
    )

    ctx.register_platform(
        name=PLATFORM_VOICE,
        label="Pingram Voice",
        adapter_factory=lambda cfg: PingramVoiceAdapter(cfg),
        check_fn=check_shared_requirements,
        validate_config=voice_configured,
        is_connected=voice_configured,
        required_env=["PINGRAM_API_KEY", "PINGRAM_REGION"],
        install_hint=INSTALL_HINT,
        env_enablement_fn=_voice_env_enablement,
        setup_fn=setup_voice,
        allowed_users_env="PINGRAM_VOICE_ALLOWED_USERS",
        allow_all_env="PINGRAM_ALLOW_ALL_USERS",
        cron_deliver_env_var="PINGRAM_VOICE_HOME_CHANNEL",
        standalone_sender_fn=standalone_send_voice,
        emoji="📞",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=VOICE_HINT,
    )

    install_send_message_parsers()
    install_session_context_hook()
