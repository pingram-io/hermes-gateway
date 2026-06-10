"""Proactive and reply subject handling for Pingram Email."""

import contextvars
import re
from typing import Optional, Tuple

_SUBJECT_LINE_RE = re.compile(r"^SUBJECT:\s*(.+?)(?:\n\n|\r\n\r\n|\n|\r\n)", re.IGNORECASE | re.DOTALL)
_MAX_SUBJECT_LEN = 120
_DEFAULT_PROACTIVE_SUBJECT = "Message from Hermes"

_proactive_subject: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "pingram_proactive_email_subject", default=None
)


def set_proactive_subject(subject: Optional[str]):
    return _proactive_subject.set((subject or "").strip() or None)


def reset_proactive_subject(token) -> None:
    _proactive_subject.reset(token)


def get_proactive_subject() -> Optional[str]:
    return _proactive_subject.get()


def normalize_subject(value: str) -> str:
    subject = re.sub(r"\s+", " ", (value or "").strip())
    if len(subject) > _MAX_SUBJECT_LEN:
        subject = subject[: _MAX_SUBJECT_LEN - 1].rstrip() + "…"
    return subject


def parse_subject_line(message: str) -> Tuple[Optional[str], str]:
    """Extract an optional ``SUBJECT: ...`` header from the message body."""
    text = message or ""
    match = _SUBJECT_LINE_RE.match(text)
    if not match:
        return None, text
    subject = normalize_subject(match.group(1))
    body = text[match.end() :].lstrip("\r\n")
    return subject or None, body


def subject_from_message_preview(message: str) -> str:
    """Fallback subject when the agent omits an explicit one."""
    line = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    line = re.sub(r"\s+", " ", line.strip())
    if not line:
        return _DEFAULT_PROACTIVE_SUBJECT
    return normalize_subject(line)


def reply_subject(base_subject: str) -> str:
    subject = normalize_subject(base_subject) or _DEFAULT_PROACTIVE_SUBJECT
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def resolve_outbound_subject(
    content: str,
    reply_ctx: Optional[dict],
    *,
    explicit_subject: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(subject, body)`` for an outbound email."""
    inbound_subject = (reply_ctx or {}).get("subject") if reply_ctx else None
    if inbound_subject:
        return reply_subject(str(inbound_subject)), content

    body = content or ""
    subject = normalize_subject(explicit_subject or "") if explicit_subject else ""
    if not subject:
        subject = get_proactive_subject() or ""
    if subject:
        subject = normalize_subject(subject)
    else:
        inline_subject, body = parse_subject_line(body)
        subject = inline_subject or subject_from_message_preview(body)

    return subject or _DEFAULT_PROACTIVE_SUBJECT, body
