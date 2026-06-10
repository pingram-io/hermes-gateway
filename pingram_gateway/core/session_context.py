"""Inject Pingram delivery guidance into local CLI session context."""

from pingram_gateway.core.constants import PLATFORM_EMAIL, PLATFORM_SMS
from pingram_gateway.core.discovery import ensure_plugins_discovered


def build_cli_delivery_note(context) -> str:
    """Return agent-only notes for proactive SMS/email from the local CLI."""
    try:
        from gateway.config import Platform, PlatformConfig, load_gateway_config
    except ImportError:
        return ""

    if context.source.platform != Platform.LOCAL:
        return ""

    connected = {p.value for p in (context.connected_platforms or [])}
    has_sms = PLATFORM_SMS in connected
    has_email = PLATFORM_EMAIL in connected

    if not has_sms or not has_email:
        try:
            from pingram_gateway.core.config import email_configured, sms_configured

            config = load_gateway_config()
            empty = PlatformConfig()
            if not has_sms:
                sms_cfg = config.platforms.get(Platform(PLATFORM_SMS)) or empty
                has_sms = sms_configured(sms_cfg)
            if not has_email:
                email_cfg = config.platforms.get(Platform(PLATFORM_EMAIL)) or empty
                has_email = email_configured(email_cfg)
        except Exception:
            pass

    if not has_sms and not has_email:
        return ""

    lines = [
        "",
        "**Platform notes:** You are in the local Hermes CLI. When the user asks "
        "you to text or email them proactively, use send_message — not the himalaya "
        "skill, IMAP/SMTP tools, or other mailbox CLIs.",
    ]
    if has_sms:
        lines.append(
            '- SMS: send_message(target="pingram-sms", message="...") '
            "(delivers to the Pingram SMS home channel)."
        )
    if has_email:
        lines.append(
            '- Email: send_message(target="pingram-email", subject="Short descriptive subject", '
            'message="<p>...</p>") — write message as HTML (not markdown); always set subject on '
            'new threads; do not prefix with "Re:" proactively.'
        )
    lines.append(
        "- Do not load or use the himalaya skill for outbound delivery when Pingram is configured."
    )
    return "\n".join(lines)


def install_session_context_hook() -> None:
    try:
        import gateway.session as session_mod
    except ImportError:
        return
    if getattr(session_mod, "_pingram_session_context_installed", False):
        return

    original = session_mod.build_session_context_prompt

    def build_session_context_prompt(context, *, redact_pii=False):
        ensure_plugins_discovered()
        prompt = original(context, redact_pii=redact_pii)
        note = build_cli_delivery_note(context)
        if note:
            return prompt + note
        return prompt

    session_mod.build_session_context_prompt = build_session_context_prompt
    session_mod._pingram_session_context_installed = True
