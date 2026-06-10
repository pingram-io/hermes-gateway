"""Convert outbound email message bodies to Pingram HTML."""

import re

from pingram_gateway.core.helpers import text_to_html

_EMAIL_WRAPPER_STYLE = (
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    "white-space:normal;"
)

_HTML_FRAGMENT_RE = re.compile(
    r"<\s*(p|div|br|strong|em|b|i|ul|ol|li|h[1-6]|span|a|table|tr|td|th|blockquote|pre|code)\b",
    re.IGNORECASE,
)

_DOCUMENT_WRAPPER_RE = re.compile(
    r"(?is)^\s*(?:<!doctype[^>]*>\s*)?"
    r"(?:<html[^>]*>\s*)?(?:<head>.*?</head>\s*)?"
    r"<body[^>]*>(.*)</body>\s*(?:</html>\s*)?$"
)


def _looks_like_html_fragment(text: str) -> bool:
    return bool(_HTML_FRAGMENT_RE.search(text or ""))


def _strip_document_wrapper(text: str) -> str:
    match = _DOCUMENT_WRAPPER_RE.match(text or "")
    if match:
        return match.group(1).strip()
    return (text or "").strip()


def _wrap_html_fragment(fragment: str) -> str:
    body = _strip_document_wrapper(fragment)
    if re.match(r"(?is)^\s*<div\b", body):
        return body
    return f'<div style="{_EMAIL_WRAPPER_STYLE}">{body}</div>'


def email_body_to_html(text: str) -> str:
    """Render agent content as email HTML.

    HTML fragments are passed through (wrapped in a styled div). Plain text is
    escaped and line-broken; markdown is not interpreted — callers should send HTML.
    """
    raw = (text or "").strip()
    if not raw:
        return text_to_html("")
    if _looks_like_html_fragment(raw):
        return _wrap_html_fragment(raw)
    return text_to_html(raw)
