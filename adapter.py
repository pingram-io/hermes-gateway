"""
Pingram Platform Adapter for Hermes Agent.

A single Hermes *platform plugin* that lets users chat with their Hermes agent
over Pingram-managed **SMS** and **Email**.  One combined ``pingram`` platform
serves both channels (Pingram uses one API key for both); the channel is encoded
in the Hermes ``chat_id`` prefix (``sms:`` / ``email:``).

chat_id shapes:
  * SMS   — ``sms:<E.164-number>`` (the number is the recipient).
  * Email — ``email:<recipient-address>#<thread-token>`` (or ``email:<address>``
    when there's no thread token). The recipient is embedded directly so replies
    — including cron/home-channel deliveries and replies after a restart — can be
    addressed without relying on in-memory state, while ``#<thread-token>`` keeps
    each email thread in its own Hermes session.

Inbound messages are received by **polling** Pingram's logs API on a timer — no
public endpoint or webhook registration is required.

Flow::

    Human --SMS/Email--> Pingram
                            |  (poll logs.getLogs every N seconds)
                            v
                  PingramAdapter -> MessageEvent -> Hermes agent
                            |
    Human <--SMS/Email-- Pingram <-- Pingram SDK <-- PingramAdapter.send()

Configuration (env vars override config.yaml ``extra``):
    PINGRAM_API_KEY            (required) pingram_sk_...
    PINGRAM_REGION             us | eu | ca (default: us)
    PINGRAM_CHANNELS           csv of channels to enable (sms,email); this turns
                               channels on. Default: inferred from any sender.
    PINGRAM_POLL_INTERVAL      seconds between polls (default 15)
    PINGRAM_POLL_LIMIT         max messages fetched per poll page (default 50)
    PINGRAM_FROM_SMS           optional SMS sender number (E.164); blank -> Pingram
                               account default number
    PINGRAM_FROM_EMAIL         optional email sender address; blank -> Pingram default
    PINGRAM_FROM_NAME          email sender display name; always sent (default: Hermes)
    PINGRAM_ALLOWED_USERS      csv of phones/emails allowed to talk to the agent
    PINGRAM_ALLOW_ALL_USERS    true to allow everyone (dev only; default false)
    PINGRAM_NOTIFICATION_TYPE  Pingram notification `type` for replies
                               (default: hermes_agent_reply)

The Pingram SDK is auto-installed into the active Hermes venv on first run if it
isn't already present (so ``hermes plugins install <repo>`` works without a
separate ``pip install``). This is venv-scoped and can be disabled with
``security.allow_lazy_installs: false`` in ``config.yaml``.

Note: inbound email *attachments* are not downloaded in polling mode (Pingram's
logs API returns attachment metadata only). Inbound SMS/MMS images work fully.
"""

import asyncio
import base64
import datetime
import html as html_lib
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
    cache_image_from_bytes,
    cache_document_from_bytes,
)
from gateway.config import Platform

logger = logging.getLogger(__name__)

DEFAULT_NOTIFICATION_TYPE = "hermes_agent"
DEFAULT_FROM_NAME = "Hermes"
_DEDUP_TTL_SECONDS = 3600
_DOWNLOAD_TIMEOUT = 30

# Runtime Python dependencies. `hermes plugins install <repo>` clones the plugin
# but does NOT pip-install its deps, so we fetch the Pingram SDK on first run if
# it's missing. aiohttp ships with Hermes' messaging stack but is listed here for
# completeness/robustness. Specs are bare PyPI package names only (no URLs / index
# overrides) so this stays a safe, venv-scoped install path.
_PINGRAM_IMPORT = "pingram"
_PINGRAM_PACKAGE = "pingram-python"
_AIOHTTP_IMPORT = "aiohttp"
_AIOHTTP_PACKAGE = "aiohttp"
_INSTALL_TIMEOUT = 300

# Inbound polling.
DEFAULT_POLL_INTERVAL = 15
MIN_POLL_INTERVAL = 3
DEFAULT_POLL_LIMIT = 50
# Bound on how many log pages a single poll cycle will walk back through.
_MAX_POLL_PAGES = 10
# logs.getLogs event_type values for inbound messages.
_LOG_EVENT_SMS_INBOUND = "sms_inbound"
_LOG_EVENT_EMAIL_INBOUND = "inbound"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _norm_phone(value: Any) -> str:
    """Reduce a phone number to comparable digits."""
    return re.sub(r"\D", "", str(value or ""))


def _normalize_phone_e164(raw: Any) -> str:
    """Best-effort cleanup of a user-entered phone number into E.164 (``+digits``).

    Tolerates the common "weird" formats people paste in:

      * separators are stripped — ``(500) 500-5000``, ``500.500.5000``,
        ``500 500 5000`` all collapse to digits;
      * a leading international ``00`` prefix becomes ``+`` (``0015005005000`` ->
        ``+15005005000``);
      * an existing ``+`` is preserved;
      * an 11-digit number starting with ``1`` gets a leading ``+``;
      * a 10-digit (or shorter, e.g. 9-digit) number is assumed North American
        and gets ``+1``;
      * anything longer is assumed to already carry a country code and just
        gets a ``+``.

    Returns ``""`` for empty/garbage input so callers can treat it as "unset".
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    had_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if digits.startswith("00"):  # international 00 prefix -> +
        rest = digits[2:]
        return "+" + rest if rest else ""
    if had_plus:
        return "+" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) <= 10:  # 10-digit US local, or shorter -> assume +1
        return "+1" + digits
    return "+" + digits  # already includes a country code


def _norm_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _redact_user(value: Any) -> str:
    """Redact a phone/email for safe logging."""
    s = str(value or "")
    if "@" in s:
        local, _, domain = s.partition("@")
        head = local[:2]
        return f"{head}***@{domain}"
    digits = _norm_phone(s)
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return "***"


_CT_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
}


def _ext_for_content_type(content_type: str) -> str:
    return _CT_EXT.get((content_type or "").split(";")[0].strip().lower(), ".bin")


def _is_image(content_type: str) -> bool:
    return (content_type or "").split("/", 1)[0].strip().lower() == "image"


# ---------------------------------------------------------------------------
# First-run dependency bootstrap
#
# Mirrors Hermes core's tools/lazy_deps.py behaviour (uv → pip → ensurepip
# ladder, venv-scoped to sys.executable, gated by security.allow_lazy_installs)
# but kept self-contained: tools.lazy_deps.ensure() only installs packages that
# appear in its hard-coded allowlist, which a third-party plugin can't extend.
# ---------------------------------------------------------------------------

def _lazy_installs_allowed() -> bool:
    """Honour the same opt-out as Hermes' lazy installer (defaults to on).

    Fails open (returns True) when config can't be read, matching core
    behaviour — refusing to install would lock users out of their own backend.
    """
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1":
        return False
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        return True
    sec = (cfg.get("security") or {}) if isinstance(cfg, dict) else {}
    return bool(sec.get("allow_lazy_installs", True))


def _venv_pip_install(specs: List[str], *, timeout: int = _INSTALL_TIMEOUT) -> Tuple[bool, str]:
    """Install ``specs`` into the active venv (uv → pip → ensurepip ladder).

    Venv-scoped: targets ``sys.executable``'s environment, never the system
    Python. Returns ``(success, error_output)``.
    """
    if not specs:
        return True, ""

    # sys.executable is <venv>/bin/python; the venv root is two levels up.
    venv_root = Path(sys.executable).parent.parent
    uv_env = {**os.environ, "VIRTUAL_ENV": str(venv_root)}

    # Tier 1: uv (preferred — fast, and Hermes' venv is uv-managed without pip).
    uv_bin = shutil.which("uv")
    if uv_bin:
        try:
            r = subprocess.run(
                [uv_bin, "pip", "install", "--python", sys.executable, *specs],
                capture_output=True, text=True, timeout=timeout, env=uv_env,
            )
            if r.returncode == 0:
                return True, ""
            logger.debug("Pingram: uv pip install failed: %s", r.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.debug("Pingram: uv invocation failed: %s", e)

    # Tier 2: python -m pip (bootstrap with ensurepip if pip isn't present).
    pip_cmd = [sys.executable, "-m", "pip"]
    try:
        probe = subprocess.run(pip_cmd + ["--version"], capture_output=True, text=True, timeout=15)
        if probe.returncode != 0:
            raise FileNotFoundError("pip not in venv")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            subprocess.run(
                [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                capture_output=True, text=True, timeout=120, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            return False, f"pip unavailable and ensurepip failed: {e}"

    try:
        r = subprocess.run(pip_cmd + ["install", *specs], capture_output=True, text=True, timeout=timeout)
        return (r.returncode == 0), (r.stderr or r.stdout or "")
    except subprocess.TimeoutExpired as e:
        return False, f"pip install timed out: {e}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _ensure_importable(import_name: str, pip_spec: str) -> bool:
    """Ensure ``import_name`` is importable, installing ``pip_spec`` if not.

    Blocking (subprocess + import); call via ``asyncio.to_thread`` from async
    code. Returns True once the module is importable, False if the install was
    gated off or failed.
    """
    import importlib

    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        pass

    if not _lazy_installs_allowed():
        logger.error(
            "Pingram: '%s' is not installed and lazy installs are disabled "
            "(security.allow_lazy_installs=false). Install it manually with: "
            "uv pip install %s",
            import_name, pip_spec,
        )
        return False

    logger.info("Pingram: '%s' missing — installing %s into the active venv ...", import_name, pip_spec)
    ok, err = _venv_pip_install([pip_spec])
    if not ok:
        logger.error("Pingram: failed to install %s: %s", pip_spec, (err or "").strip()[-800:])
        return False

    # importlib caches negative imports and metadata per process; refresh both
    # before retrying so the freshly installed package is visible.
    importlib.invalidate_caches()
    try:
        import importlib.metadata as _md

        if hasattr(_md, "_cache_clear"):
            _md._cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass

    try:
        importlib.import_module(import_name)
        logger.info("Pingram: installed %s.", pip_spec)
        return True
    except ImportError:
        logger.error(
            "Pingram: installed %s but '%s' is still not importable "
            "(a gateway restart may be required).",
            pip_spec, import_name,
        )
        return False


def _text_to_html(text: str) -> str:
    """Render a plain-text agent reply as minimal, safe email HTML."""
    escaped = html_lib.escape(text or "")
    body = escaped.replace("\n", "<br>")
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,'
        f'Arial,sans-serif;white-space:normal;">{body}</div>'
    )


# Characters allowed verbatim in the thread discriminator of an email chat_id.
# Everything else (angle brackets, spaces, exotic punctuation) is collapsed to
# keep the identifier compact and safe to use as a session/path key.
_THREAD_TOKEN_KEEP = re.compile(r"[^A-Za-z0-9._@=+-]")


def _sanitize_thread_token(token: Any) -> str:
    """Reduce a raw email thread token (a Message-ID/References value) to a
    compact, identifier-safe discriminator. Returns ``""`` for empty input."""
    t = str(token or "").strip().strip("<>").strip()
    if not t:
        return ""
    return _THREAD_TOKEN_KEEP.sub("_", t)[:120]


def _email_chat_id(address: str, thread_token: Any) -> str:
    """Build an email ``chat_id`` of the form ``email:<address>#<thread>``.

    Embedding the recipient address directly in the chat_id means a reply can
    be addressed without any in-memory state (so cron/home-channel delivery and
    post-restart replies work), while the ``#<thread>`` discriminator keeps each
    email thread in its own Hermes session. When there's no thread token, the
    chat_id collapses to ``email:<address>``.
    """
    disc = _sanitize_thread_token(thread_token)
    addr = _norm_email(address)
    return f"email:{addr}#{disc}" if disc else f"email:{addr}"


def _recipient_from_email_chat_id(chat_id: str) -> str:
    """Extract the recipient address from an email chat_id.

    Inverse of :func:`_email_chat_id`; also handles the legacy/plain
    ``email:<address>`` form. Returns ``""`` if no address is present (e.g. a
    thread-token-only legacy chat_id), so callers fall back to live context.
    """
    rest = chat_id[len("email:"):] if chat_id.startswith("email:") else chat_id
    address = rest.split("#", 1)[0].strip()
    return address if "@" in address else ""


def _fetch_account_identities(api_key: str, region: str) -> Tuple[List[str], List[str]]:
    """Best-effort: return ``(email_addresses, phone_numbers)`` for the account.

    Queries Pingram's ``addresses.listAddresses`` and ``numbers.list`` so the
    wizard can default the email sender to the account's first address and show
    the user their agent's contact points. Each lookup is independent — one
    failing still returns the other. Returns empty lists on any problem (SDK
    unavailable, bad key, network/region error) so the wizard degrades quietly.
    Also doubles as a light validation that the API key + region work.
    """
    try:
        import pingram  # noqa: F401
    except ImportError:
        if not _ensure_importable(_PINGRAM_IMPORT, _PINGRAM_PACKAGE):
            return [], []

    try:
        from pingram import Pingram

        async def _query() -> Tuple[List[str], List[str]]:
            emails: List[str] = []
            numbers: List[str] = []
            async with Pingram(api_key=api_key, region=region) as client:
                try:
                    resp = await client.addresses.addresses_list_addresses()
                    for addr in getattr(resp, "addresses", None) or []:
                        full = getattr(addr, "full_address", None)
                        if full:
                            emails.append(str(full).strip())
                except Exception:
                    logger.debug("Pingram: addresses.listAddresses failed", exc_info=True)
                try:
                    resp = await client.numbers.numbers_list()
                    for num in getattr(resp, "numbers", None) or []:
                        number = getattr(num, "phone_number", None)
                        if number:
                            numbers.append(str(number).strip())
                except Exception:
                    logger.debug("Pingram: numbers.list failed", exc_info=True)
            return emails, numbers

        return asyncio.run(_query())
    except Exception:
        logger.debug("Pingram: account identity lookup failed", exc_info=True)
        return [], []


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class PingramAdapter(BasePlatformAdapter):
    """Async Pingram adapter implementing the BasePlatformAdapter interface."""

    def __init__(self, config, **kwargs):
        platform = Platform("pingram")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        def cfg(env_key: str, extra_key: str, default: Any = "") -> Any:
            env_val = os.getenv(env_key)
            if env_val is not None and env_val != "":
                return env_val
            return extra.get(extra_key, default)

        self.api_key: str = str(cfg("PINGRAM_API_KEY", "api_key", "")).strip()
        self.region: str = str(cfg("PINGRAM_REGION", "region", "us")).strip().lower() or "us"

        # Sender identity is OPTIONAL on every channel — when left blank, Pingram
        # fills in the account default (a Pingram-managed number / sender). We
        # only send these fields when the user explicitly set them.
        self.from_sms: str = _normalize_phone_e164(cfg("PINGRAM_FROM_SMS", "from_sms", ""))
        self.from_email: str = _norm_email(cfg("PINGRAM_FROM_EMAIL", "from_email", ""))
        # Sender display name is always sent on emails; defaults to "Hermes" when
        # the user leaves it blank (unlike the address/number, which fall back to
        # Pingram's account defaults).
        self.from_name: str = str(cfg("PINGRAM_FROM_NAME", "from_name", DEFAULT_FROM_NAME)).strip() or DEFAULT_FROM_NAME

        # Channels are an explicit choice (PINGRAM_CHANNELS), independent of
        # whether a sender is configured. Fall back to inferring from configured
        # senders for legacy setups that predate the channel selector.
        requested = set(_parse_csv(cfg("PINGRAM_CHANNELS", "channels", ""))) & {"sms", "email"}
        if requested:
            self.channels: set = requested
        else:
            inferred: set = set()
            if self.from_sms:
                inferred.add("sms")
            if self.from_email:
                inferred.add("email")
            self.channels = inferred

        # Inbound polling cadence.
        try:
            self.poll_interval: int = int(cfg("PINGRAM_POLL_INTERVAL", "poll_interval", DEFAULT_POLL_INTERVAL))
        except (TypeError, ValueError):
            self.poll_interval = DEFAULT_POLL_INTERVAL
        self.poll_interval = max(MIN_POLL_INTERVAL, self.poll_interval)
        try:
            self.poll_limit: int = int(cfg("PINGRAM_POLL_LIMIT", "poll_limit", DEFAULT_POLL_LIMIT))
        except (TypeError, ValueError):
            self.poll_limit = DEFAULT_POLL_LIMIT
        self.poll_limit = max(1, self.poll_limit)

        self.notification_type: str = str(
            cfg("PINGRAM_NOTIFICATION_TYPE", "notification_type", DEFAULT_NOTIFICATION_TYPE)
        ).strip() or DEFAULT_NOTIFICATION_TYPE

        self.allow_all: bool = _truthy(cfg("PINGRAM_ALLOW_ALL_USERS", "allow_all_users", False))
        allowed = _parse_csv(cfg("PINGRAM_ALLOWED_USERS", "allowed_users", ""))
        # Normalize allowlisted phones to E.164 before reducing to comparable
        # digits, so a hand-edited "5005005000" still matches an inbound
        # "+15005005000" (both collapse to the same digits after +1 is added).
        self._allowed_phones: set = {
            _norm_phone(_normalize_phone_e164(a)) for a in allowed if "@" not in a
        }
        self._allowed_emails: set = {_norm_email(a) for a in allowed if "@" in a}

        # Runtime state
        self._reply_ctx: Dict[str, Dict[str, Any]] = {}
        self._seen: Dict[str, float] = {}
        self._tasks: set = set()
        self._poll_task = None
        # Only messages newer than this watermark are processed; set at startup
        # so we don't replay history on the first poll.
        self._poll_watermark_ms = 0

    @property
    def name(self) -> str:
        return "Pingram"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        if not self.api_key:
            logger.error("Pingram: PINGRAM_API_KEY is required")
            self._set_fatal_error("config_missing", "PINGRAM_API_KEY must be set", retryable=False)
            return False

        if not self.channels:
            logger.error("Pingram: enable at least one channel via PINGRAM_CHANNELS (sms and/or email)")
            self._set_fatal_error(
                "config_missing",
                "PINGRAM_CHANNELS must enable sms and/or email",
                retryable=False,
            )
            return False

        # Ensure runtime deps. When installed via `hermes plugins install <repo>`
        # (a git clone), the plugin's Python deps aren't pip-installed, so fetch
        # the Pingram SDK on first run if it's missing. Runs in a thread so the
        # blocking pip/uv subprocess doesn't stall the event loop. aiohttp is
        # used to download inbound MMS media; the SDK polls + sends.
        if not await asyncio.to_thread(_ensure_importable, _AIOHTTP_IMPORT, _AIOHTTP_PACKAGE):
            self._set_fatal_error("dependency_missing", "aiohttp not installed", retryable=False)
            return False
        if not await asyncio.to_thread(_ensure_importable, _PINGRAM_IMPORT, _PINGRAM_PACKAGE):
            self._set_fatal_error(
                "dependency_missing",
                "Pingram SDK not installed (run: uv pip install pingram-python)",
                retryable=False,
            )
            return False

        # Start the watermark at "now" so we don't replay the account's history
        # on the first poll — only messages that arrive after startup are
        # delivered.
        self._poll_watermark_ms = int(time.time() * 1000)
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._mark_connected()
        logger.info(
            "Pingram: polling logs.getLogs every %ss (channels: %s); no public endpoint required",
            self.poll_interval,
            ",".join(sorted(self.channels)),
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    # ── Inbound helpers ───────────────────────────────────────────────────

    def _is_allowed(self, channel: str, sender: Any) -> bool:
        if self.allow_all:
            return True
        if channel == "sms":
            return _norm_phone(_normalize_phone_e164(sender)) in self._allowed_phones
        return _norm_email(sender) in self._allowed_emails

    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        # Prune expired entries opportunistically.
        if self._seen:
            expired = [k for k, ts in self._seen.items() if now - ts > _DEDUP_TTL_SECONDS]
            for k in expired:
                self._seen.pop(k, None)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    async def _process_inbound(self, channel: str, payload: dict) -> None:
        try:
            if channel == "sms":
                await self._dispatch_sms(payload)
            else:
                await self._dispatch_email(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pingram: error processing inbound %s", channel)

    # ── Poll-mode inbound ─────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Periodically pull new inbound messages via logs.getLogs.

        Each cycle fetches the newest messages, keeps those newer than a
        watermark, and dispatches the inbound ones. trackingId dedup is the
        authoritative guard against reprocessing; the watermark only bounds how
        far back we page.
        """
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pingram: poll cycle failed")
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self) -> None:
        from pingram import Pingram

        new_messages: List[Any] = []
        highest_ms = self._poll_watermark_ms
        cursor: Optional[str] = None
        pages = 0

        async with Pingram(api_key=self.api_key, region=self.region) as client:
            while pages < _MAX_POLL_PAGES:
                resp = await client.logs.logs_get_logs(limit=self.poll_limit, cursor=cursor)
                messages = getattr(resp, "messages", None) or []
                if not messages:
                    break
                # Messages are newest-first; once we cross the watermark every
                # remaining (and subsequent page) entry is older, so we stop.
                reached_old = False
                for msg in messages:
                    epoch = int(getattr(msg, "epoch_ms", 0) or 0)
                    if epoch <= self._poll_watermark_ms:
                        reached_old = True
                        break
                    new_messages.append(msg)
                    if epoch > highest_ms:
                        highest_ms = epoch
                if reached_old:
                    break
                cursor = getattr(resp, "next_cursor", None)
                if not cursor:
                    break
                pages += 1

        # Dispatch oldest-first so a burst arrives in natural order.
        for msg in reversed(new_messages):
            self._handle_log_message(msg)

        # Advance the watermark so the next cycle only sees newer messages.
        self._poll_watermark_ms = max(self._poll_watermark_ms, highest_ms)

    def _handle_log_message(self, msg: Any) -> None:
        event_type = str(getattr(msg, "event_type", "") or "").lower()
        if event_type == _LOG_EVENT_SMS_INBOUND:
            channel = "sms"
        elif event_type == _LOG_EVENT_EMAIL_INBOUND:
            channel = "email"
        else:
            return  # not an inbound message (sent/delivered/opened/etc.)

        if channel not in self.channels:
            return

        sender = getattr(msg, "var_from", None) or ""
        if not self._is_allowed(channel, sender):
            logger.info("Pingram: ignoring polled %s from unauthorized user %s", channel, _redact_user(sender))
            return

        tracking = str(getattr(msg, "tracking_id", "") or "")
        epoch = int(getattr(msg, "epoch_ms", 0) or 0)
        dedup_key = f"track:{tracking}" if tracking else f"poll:{channel}:{_norm_phone(sender) or sender}:{epoch}"
        if self._is_duplicate(dedup_key):
            return

        payload = self._log_msg_to_payload(channel, msg)

        # Run dispatch in the background so a long agent turn doesn't stall the
        # poll loop.
        task = asyncio.create_task(self._process_inbound(channel, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _log_msg_to_payload(channel: str, msg: Any) -> dict:
        """Adapt a logs.getLogs message summary to the dict the ``_dispatch_*``
        methods consume."""
        tracking = getattr(msg, "tracking_id", None)
        if channel == "sms":
            media = []
            for m in getattr(msg, "media", None) or []:
                url = getattr(m, "url", None)
                if url:
                    media.append({"url": url, "contentType": getattr(m, "content_type", None)})
            return {
                "from": getattr(msg, "var_from", None) or "",
                "text": getattr(msg, "body_text", None) or "",
                "media": media,
                "trackingId": tracking,
            }
        # Email. Inbound attachment content is not available via logs.getLogs
        # (only metadata), so attachments are intentionally omitted here.
        return {
            "from": getattr(msg, "var_from", None) or "",
            "subject": getattr(msg, "subject", None) or "",
            "bodyText": getattr(msg, "body_text", None) or "",
            "bodyHtml": getattr(msg, "body_html", None) or "",
            "references": getattr(msg, "references", None),
            "inReplyTo": getattr(msg, "in_reply_to", None),
            "messageId": getattr(msg, "message_id", None),
            "fromName": getattr(msg, "from_name", None),
            "trackingId": tracking,
        }

    # ── SMS inbound ───────────────────────────────────────────────────────

    async def _dispatch_sms(self, payload: dict) -> None:
        if not self._message_handler:
            return
        sender = str(payload.get("from", ""))
        chat_id = f"sms:{sender}"
        text = payload.get("text") or ""

        media_urls, media_types = await self._collect_sms_media(payload)

        self._reply_ctx[chat_id] = {
            "channel": "sms",
            "to_number": sender,
            "user_id": payload.get("userId"),
        }

        source = self.build_source(
            chat_id=chat_id,
            chat_name=_redact_user(sender),
            chat_type="dm",
            user_id=sender,
            user_name=_redact_user(sender),
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.PHOTO if media_urls else MessageType.TEXT,
            source=source,
            message_id=str(payload.get("trackingId") or int(time.time() * 1000)),
            timestamp=datetime.datetime.now(),
            media_urls=media_urls,
            media_types=media_types,
        )
        await self.handle_message(event)

    async def _collect_sms_media(self, payload: dict) -> Tuple[List[str], List[str]]:
        media_urls: List[str] = []
        media_types: List[str] = []
        for item in payload.get("media") or []:
            url = item.get("url") if isinstance(item, dict) else None
            if not url:
                continue
            content_type = (item.get("contentType") if isinstance(item, dict) else "") or ""
            data, fetched_ct = await self._download(url)
            if not data:
                continue
            content_type = content_type or fetched_ct
            try:
                if _is_image(content_type):
                    path = cache_image_from_bytes(data, _ext_for_content_type(content_type))
                else:
                    fname = url.split("/")[-1].split("?")[0] or f"attachment{_ext_for_content_type(content_type)}"
                    path = cache_document_from_bytes(data, fname)
                media_urls.append(path)
                media_types.append(content_type or "application/octet-stream")
            except Exception:
                logger.debug("Pingram: failed to cache SMS media", exc_info=True)
        return media_urls, media_types

    # ── Email inbound ─────────────────────────────────────────────────────

    async def _dispatch_email(self, payload: dict) -> None:
        if not self._message_handler:
            return
        sender = _norm_email(payload.get("from", ""))
        subject = payload.get("subject") or ""
        # The thread token (References root / In-Reply-To / Message-ID) is stable
        # across a conversation, so the first message and its replies map to the
        # same chat_id. The recipient address is embedded in the chat_id so we
        # can address replies without relying on in-memory state.
        thread_key = self._email_thread_key(payload)
        chat_id = _email_chat_id(sender, thread_key)
        text = payload.get("bodyText") or self._html_to_text(payload.get("bodyHtml") or "")

        media_urls, media_types = self._collect_email_attachments(payload)

        self._reply_ctx[chat_id] = {
            "channel": "email",
            "to_email": sender,
            "subject": subject,
            "message_id": payload.get("messageId"),
            "references": payload.get("references"),
            "user_id": payload.get("userId"),
        }

        source = self.build_source(
            chat_id=chat_id,
            chat_name=subject or _redact_user(sender),
            chat_type="dm",
            user_id=sender,
            user_name=payload.get("fromName") or _redact_user(sender),
            thread_id=thread_key or None,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.PHOTO if media_urls else MessageType.TEXT,
            source=source,
            message_id=str(payload.get("messageId") or payload.get("trackingId") or int(time.time() * 1000)),
            timestamp=datetime.datetime.now(),
            media_urls=media_urls,
            media_types=media_types,
        )
        await self.handle_message(event)

    @staticmethod
    def _email_thread_key(payload: dict) -> str:
        references = payload.get("references")
        if references:
            if isinstance(references, (list, tuple)):
                refs = [str(r).strip() for r in references if str(r).strip()]
                if refs:
                    return refs[0]
            else:
                refs = str(references).split()
                if refs:
                    return refs[0].strip()
        for key in ("inReplyTo", "messageId"):
            value = payload.get(key)
            if value:
                return str(value).strip()
        return ""

    def _collect_email_attachments(self, payload: dict) -> Tuple[List[str], List[str]]:
        media_urls: List[str] = []
        media_types: List[str] = []
        for att in payload.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            content = att.get("content")
            if not content:
                continue
            try:
                data = base64.b64decode(content)
            except Exception:
                logger.debug("Pingram: failed to decode email attachment", exc_info=True)
                continue
            content_type = att.get("contentType") or "application/octet-stream"
            filename = att.get("filename") or f"attachment{_ext_for_content_type(content_type)}"
            try:
                if _is_image(content_type):
                    path = cache_image_from_bytes(data, _ext_for_content_type(content_type))
                else:
                    path = cache_document_from_bytes(data, filename)
                media_urls.append(path)
                media_types.append(content_type)
            except Exception:
                logger.debug("Pingram: failed to cache email attachment", exc_info=True)
        return media_urls, media_types

    @staticmethod
    def _html_to_text(html: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html or "")
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p>", "\n\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        return html_lib.unescape(text).strip()

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if chat_id.startswith("sms:"):
            number = chat_id[len("sms:"):]
            return await self._send_sms(number, content)
        if chat_id.startswith("email:"):
            return await self._send_email(chat_id, content)
        return SendResult(success=False, error=f"Unknown chat_id prefix: {chat_id}")

    async def _send_sms(self, number: str, content: str, *, attachment_note: str = "") -> SendResult:
        if "sms" not in self.channels:
            return SendResult(success=False, error="SMS channel not configured")
        message = (content or "").strip()
        if attachment_note:
            message = f"{message}\n\n{attachment_note}".strip()
        if not message:
            return SendResult(success=False, error="Empty SMS message")

        # ``from`` is optional — omit it so Pingram uses the account's default
        # number unless the user pinned a specific verified sender.
        sms_block: Dict[str, Any] = {"message": message}
        if self.from_sms:
            sms_block["from"] = self.from_sms
        body = {
            "type": self.notification_type,
            "to": {"id": number, "number": number},
            "sms": sms_block,
        }
        return await self._pingram_send(body)

    async def _send_email(self, chat_id: str, content: str, *, attachments: Optional[List[dict]] = None) -> SendResult:
        if "email" not in self.channels:
            return SendResult(success=False, error="Email channel not configured")
        ctx = self._reply_ctx.get(chat_id)
        # Prefer live context (it also carries subject + threading headers), but
        # fall back to the address embedded in the chat_id. The latter is what
        # makes cron/home-channel delivery and post-restart replies work without
        # any persisted state.
        to_email = (ctx or {}).get("to_email") or _recipient_from_email_chat_id(chat_id)
        if not to_email:
            return SendResult(success=False, error="No recipient email for thread")

        subject = (ctx or {}).get("subject") or "Message from your Hermes agent"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        email_block: Dict[str, Any] = {"subject": subject, "html": _text_to_html(content)}
        # Sender display name is always sent (defaults to "Hermes"). The address
        # is an optional override — omit it and Pingram uses the account default
        # (e.g. noreply@pingram.io).
        email_block["senderName"] = self.from_name or DEFAULT_FROM_NAME
        if self.from_email:
            email_block["senderEmail"] = self.from_email
        options: Optional[Dict[str, Any]] = None
        if attachments:
            options = {"email": {"attachments": attachments}}

        body: Dict[str, Any] = {
            "type": self.notification_type,
            "to": {"id": to_email, "email": to_email},
            "email": email_block,
        }
        if options:
            body["options"] = options
        return await self._pingram_send(body)

    async def _pingram_send(self, body: Dict[str, Any]) -> SendResult:
        try:
            from pingram import Pingram
            from pingram.models.sender_post_body import SenderPostBody
        except ImportError:
            return SendResult(success=False, error="pingram SDK not installed")

        try:
            sender_body = SenderPostBody.from_dict(body)
        except Exception as e:
            logger.debug("Pingram: failed to build send body", exc_info=True)
            return SendResult(success=False, error=f"invalid send body: {e}")

        try:
            async with Pingram(api_key=self.api_key, region=self.region) as client:
                response = await client.send(sender_body)
            return SendResult(success=True, message_id=getattr(response, "tracking_id", None))
        except Exception as e:
            logger.warning("Pingram: send failed: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # SMS/Email have no typing indicator.
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        channel = "sms" if chat_id.startswith("sms:") else "email" if chat_id.startswith("email:") else "unknown"
        return {"name": chat_id, "type": "dm", "channel": channel}

    # ── Outbound media ────────────────────────────────────────────────────

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, image_url, caption, is_url=True)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media(chat_id, image_path, caption, is_url=False)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media(chat_id, file_path, caption, is_url=False, file_name=file_name)

    async def _send_media(
        self,
        chat_id: str,
        source: str,
        caption: Optional[str],
        *,
        is_url: bool,
        file_name: Optional[str] = None,
    ) -> SendResult:
        caption = caption or ""
        if chat_id.startswith("email:"):
            attachment = await self._build_email_attachment(source, is_url=is_url, file_name=file_name)
            if attachment is None:
                return await self._send_email(chat_id, caption or "(attachment unavailable)")
            return await self._send_email(chat_id, caption, attachments=[attachment])

        if chat_id.startswith("sms:"):
            # Outbound SMS MMS is not supported by the Pingram send SDK (no
            # sms.mediaUrls field). Forward a link when we already have a
            # public URL; otherwise note the limitation in the SMS body.
            number = chat_id[len("sms:"):]
            if is_url and source.lower().startswith(("http://", "https://")):
                note = f"Attachment: {source}"
            else:
                note = "(An attachment was generated but can't be sent over SMS.)"
            return await self._send_sms(number, caption, attachment_note=note)

        return SendResult(success=False, error=f"Unknown chat_id prefix: {chat_id}")

    async def _build_email_attachment(
        self,
        source: str,
        *,
        is_url: bool,
        file_name: Optional[str],
    ) -> Optional[dict]:
        try:
            if is_url:
                data, content_type = await self._download(source)
                if not data:
                    return None
                filename = file_name or source.split("/")[-1].split("?")[0] or f"file{_ext_for_content_type(content_type)}"
            else:
                with open(source, "rb") as fh:
                    data = fh.read()
                content_type = ""
                filename = file_name or os.path.basename(source) or "file"
            return {
                "filename": filename,
                "content": base64.b64encode(data).decode("ascii"),
                "contentType": content_type or "application/octet-stream",
            }
        except Exception:
            logger.debug("Pingram: failed to build email attachment", exc_info=True)
            return None

    # ── Networking ────────────────────────────────────────────────────────

    async def _download(self, url: str) -> Tuple[Optional[bytes], str]:
        try:
            import aiohttp
        except ImportError:
            return None, ""
        try:
            timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.debug("Pingram: download %s returned %s", url, resp.status)
                        return None, ""
                    data = await resp.read()
                    return data, resp.headers.get("Content-Type", "")
        except Exception:
            logger.debug("Pingram: download failed for %s", url, exc_info=True)
            return None, ""


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """True when the Pingram SDK + aiohttp are importable and a key is set.

    The gateway uses this as the activation gate: ``create_adapter`` skips the
    platform (never calling ``connect()``) when this returns False, so this is
    also where the SDK is auto-installed on first run.

    Installing deps is gated only on the user having *enabled* the plugin —
    which is implicit here, since Hermes only loads/registers (and thus only
    calls ``check_requirements``) for enabled plugins. Enabling Pingram is a
    clear opt-in, so we fetch its deps regardless of whether a key is set yet.
    We still require ``PINGRAM_API_KEY`` for the *activation* result so the
    gateway doesn't spin up a keyless adapter that just errors in ``connect()``.
    The install is venv-scoped and honours ``security.allow_lazy_installs``.
    """
    deps_ok = _ensure_importable(_AIOHTTP_IMPORT, _AIOHTTP_PACKAGE) and _ensure_importable(
        _PINGRAM_IMPORT, _PINGRAM_PACKAGE
    )
    return bool(os.getenv("PINGRAM_API_KEY")) and deps_ok


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    api_key = os.getenv("PINGRAM_API_KEY") or extra.get("api_key")
    # A channel must be enabled. Senders are optional (Pingram defaults them),
    # so fall back to legacy sender-presence inference when PINGRAM_CHANNELS is
    # not set.
    has_channel = (
        os.getenv("PINGRAM_CHANNELS") or extra.get("channels")
        or os.getenv("PINGRAM_FROM_SMS") or extra.get("from_sms")
        or os.getenv("PINGRAM_FROM_EMAIL") or extra.get("from_email")
    )
    return bool(api_key and has_channel)


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars during gateway config load."""
    api_key = os.getenv("PINGRAM_API_KEY", "").strip()
    channels = os.getenv("PINGRAM_CHANNELS", "").strip()
    from_sms = os.getenv("PINGRAM_FROM_SMS", "").strip()
    from_email = os.getenv("PINGRAM_FROM_EMAIL", "").strip()
    from_name = os.getenv("PINGRAM_FROM_NAME", "").strip()
    # Enable when a channel is selected; senders are optional (Pingram defaults
    # them). Legacy setups enable implicitly via a configured sender.
    if not (api_key and (channels or from_sms or from_email)):
        return None
    seed: dict = {"api_key": api_key}
    if os.getenv("PINGRAM_REGION"):
        seed["region"] = os.getenv("PINGRAM_REGION").strip().lower()
    if channels:
        seed["channels"] = channels
    if from_sms:
        seed["from_sms"] = from_sms
    if from_email:
        seed["from_email"] = from_email
    if from_name:
        seed["from_name"] = from_name
    if os.getenv("PINGRAM_POLL_INTERVAL"):
        seed["poll_interval"] = os.getenv("PINGRAM_POLL_INTERVAL").strip()
    if os.getenv("PINGRAM_POLL_LIMIT"):
        seed["poll_limit"] = os.getenv("PINGRAM_POLL_LIMIT").strip()
    if os.getenv("PINGRAM_NOTIFICATION_TYPE"):
        seed["notification_type"] = os.getenv("PINGRAM_NOTIFICATION_TYPE").strip()
    return seed


def interactive_setup() -> None:
    """Guided ``hermes setup gateway`` flow for Pingram.

    Reached by selecting Pingram in the messaging-platforms menu. Lazy-imports
    the CLI prompt helpers so the plugin stays importable in non-CLI contexts
    (gateway runtime, tests).

    Intentionally minimal: it asks only for the region, API key, and an access
    allowlist (the one safety-critical setting). Everything else — channels
    (defaults to SMS+Email), sender identity (Pingram defaults; display name
    "Hermes"), and poll interval (15s) — uses defaults that remain editable via
    env vars in ``~/.hermes/.env``.
    """
    from hermes_cli.setup import (
        prompt,
        prompt_choice,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("Pingram")
    if get_env_value("PINGRAM_API_KEY") and not prompt_yes_no("Pingram is already configured. Reconfigure?", False):
        return

    print_info("Chat with your Hermes agent over SMS and Email, routed through Pingram.")
    print_info("You just need your Pingram region and API key — everything else uses")
    print_info("sensible defaults you can tweak later in ~/.hermes/.env.")
    print()

    # Region first — it selects the API endpoint, so it must match the account's
    # region before anything else (e.g. the key) is used.
    print_info("Pick the region your Pingram account lives in (selects the API endpoint).")
    regions = ["us", "eu", "ca"]
    current_region = (get_env_value("PINGRAM_REGION") or "us").strip().lower()
    default_idx = regions.index(current_region) if current_region in regions else 0
    region = regions[prompt_choice("Region", ["US (Default)", "EU", "CA"], default_idx)]
    save_env_value("PINGRAM_REGION", region)

    api_key = prompt("Pingram API key (pingram_sk_...)", password=True)
    if not api_key:
        print_warning("API key is required — skipping Pingram setup.")
        return
    save_env_value("PINGRAM_API_KEY", api_key.strip())

    # Look up the account's configured email addresses + phone numbers once.
    # Used to (a) default the email sender to the account's first verified
    # address and (b) show the user their agent's contact points at the end.
    # Best-effort — empty on failure, so the runtime falls back to defaults.
    print_info("Checking your Pingram account...")
    account_emails, account_numbers = _fetch_account_identities(api_key.strip(), region)
    if account_emails and not (get_env_value("PINGRAM_FROM_EMAIL") or "").strip():
        save_env_value("PINGRAM_FROM_EMAIL", account_emails[0])

    # Access control — the one safety-critical setting we still ask for. An empty
    # allowlist makes Hermes ignore everyone; "allow all" would let anyone who
    # texts/emails run the agent. One combined prompt covers both channels.
    print()
    print_info("Access control: who is allowed to message your agent?")
    existing = _parse_csv(get_env_value("PINGRAM_ALLOWED_USERS") or "")
    answer = prompt(
        "What numbers and emails can message Hermes? "
        "(comma-separated, e.g. +15005005000, you@example.com)",
        default=",".join(existing),
    )
    allowed: List[str] = []
    for item in _parse_csv(answer):
        if "@" in item:
            allowed.append(_norm_email(item))
        else:
            normalized = _normalize_phone_e164(item)
            if normalized:
                allowed.append(normalized)
    save_env_value("PINGRAM_ALLOWED_USERS", ",".join(allowed))
    save_env_value("PINGRAM_ALLOW_ALL_USERS", "false")
    if not allowed:
        print_warning("No one allowlisted yet — Hermes will ignore inbound messages until you "
                      "add allowed numbers/emails (PINGRAM_ALLOWED_USERS in ~/.hermes/.env).")

    # Defaults applied without prompting (all editable via env vars):
    #   • Channels: both SMS + Email. This also gates platform enablement, so we
    #     must persist it; only set when unset to preserve an existing choice.
    #   • Sender name "Hermes", default sender number/address, and a 15s poll
    #     interval are applied at runtime, so they need no env entry here.
    if not get_env_value("PINGRAM_CHANNELS"):
        save_env_value("PINGRAM_CHANNELS", "sms,email")

    print()
    print_success("Pingram configured!")

    # Show the user how to reach their agent. The first email is the one we set
    # as the default sender; numbers are display-only (we leave PINGRAM_FROM_SMS
    # on Pingram's account default rather than picking from this list).
    if account_emails:
        print()
        print_info(f"Your agent's email address{'es' if len(account_emails) > 1 else ''}:")
        for i, addr in enumerate(account_emails):
            print_info(f"  • {addr}{'   [default]' if i == 0 else ''}")
    if account_numbers:
        print()
        print_info(f"Your agent's number{'s' if len(account_numbers) > 1 else ''}:")
        for number in account_numbers:
            print_info(f"  • {number}")

    print()
    print_info("Next steps:")
    print_info("  1. Start the gateway: hermes gateway")
    print_info("  2. Text or email your Pingram number/address to chat — Hermes polls every "
               f"{DEFAULT_POLL_INTERVAL}s for new messages.")
    print()
    print_info("Defaults applied (edit ~/.hermes/.env to change):")
    print_info("  • Channels: SMS + Email                  (PINGRAM_CHANNELS)")
    print_info(f"  • Sender name: {DEFAULT_FROM_NAME}                     (PINGRAM_FROM_NAME)")
    print_info("  • Sender number: Pingram account default (PINGRAM_FROM_SMS)")
    print_info("  • Sender email: your account's address   (PINGRAM_FROM_EMAIL)")
    print_info(f"  • Poll interval: {DEFAULT_POLL_INTERVAL}s                     (PINGRAM_POLL_INTERVAL)")


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="pingram",
        label="Pingram (SMS, Email, Voice)",
        adapter_factory=lambda cfg: PingramAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["PINGRAM_API_KEY", "PINGRAM_REGION"],
        install_hint="hermes plugins install pingram-io/hermes-gateway  (SDK auto-installs on first run; or: pip install hermes-pingram-gateway)",
        env_enablement_fn=_env_enablement,
        setup_fn=interactive_setup,
        allowed_users_env="PINGRAM_ALLOWED_USERS",
        allow_all_env="PINGRAM_ALLOW_ALL_USERS",
        # Makes ``pingram`` a valid cron/home-channel delivery target. The chat
        # set via /sethome is stored here; for email it embeds the recipient
        # address (see _email_chat_id) so delivery survives restarts.
        cron_deliver_env_var="PINGRAM_HOME_CHANNEL",
        emoji="📨",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are chatting over SMS and/or Email via Pingram. The channel is "
            "encoded in the chat_id prefix: 'sms:' or 'email:'. For SMS, reply in "
            "plain text only (no markdown), keep it short (messages are split into "
            "~160-character segments), and avoid links where possible. For Email, a "
            "subject and light HTML are fine; replies are threaded as 'Re:'. "
            "Inbound MMS images are provided to you as media (inbound email "
            "attachments are not available)."
        ),
    )
