"""Shared helpers for Pingram gateway adapters."""

import asyncio
import email.utils
import html as html_lib
import importlib
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

from pingram_gateway.core.constants import (
    AIOHTTP_IMPORT,
    AIOHTTP_PACKAGE,
    CT_EXT,
    DEFAULT_FROM_NAME,
    INSTALL_TIMEOUT,
    MIN_SMS_DIGITS,
    PINGRAM_IMPORT,
    PINGRAM_PACKAGE,
)

logger = logging.getLogger(__name__)

_THREAD_TOKEN_KEEP = re.compile(r"[^A-Za-z0-9._@=+-]")


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def norm_phone(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_phone_e164(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    had_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if digits.startswith("00"):
        rest = digits[2:]
        return "+" + rest if rest else ""
    if had_plus:
        return "+" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) <= 10:
        return "+1" + digits
    return "+" + digits


def norm_email(value: Any) -> str:
    return str(value or "").strip().lower()


def is_plausible_sms_number(raw: Any) -> bool:
    normalized = normalize_phone_e164(raw)
    return len(norm_phone(normalized)) >= MIN_SMS_DIGITS


def is_routing_or_message_id_address(addr: str) -> bool:
    addr = norm_email(addr)
    if not addr or "@" not in addr:
        return True
    local, _, domain = addr.partition("@")
    if domain in {"mail.gmail.com", "mail.google.com"}:
        if local.startswith(("ca+", "cac", "bounce", "btdp")):
            return True
    if local.startswith("<") or local.endswith(">"):
        return True
    return False


def is_deliverable_email(addr: str) -> bool:
    addr = norm_email(addr)
    if not addr or "@" not in addr or is_routing_or_message_id_address(addr):
        return False
    local, _, domain = addr.rpartition("@")
    return bool(local and domain and "." in domain)


def parse_sender_email(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    _display, addr = email.utils.parseaddr(text)
    addr = norm_email(addr)
    return addr if is_deliverable_email(addr) else ""


def looks_like_directory_label(ref: str) -> bool:
    s = (ref or "").strip()
    if not s:
        return False
    if "*" in s:
        return True
    if " / topic " in s.lower():
        return True
    if "<" in s and "@" in s:
        return True
    return False


def redact_user(value: Any) -> str:
    s = str(value or "")
    if "@" in s:
        local, _, domain = s.partition("@")
        return f"{local[:2]}***@{domain}"
    digits = norm_phone(s)
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return "***"


def ext_for_content_type(content_type: str) -> str:
    return CT_EXT.get((content_type or "").split(";")[0].strip().lower(), ".bin")


def is_image(content_type: str) -> bool:
    return (content_type or "").split("/", 1)[0].strip().lower() == "image"


def text_to_html(text: str) -> str:
    escaped = html_lib.escape(text or "")
    body = escaped.replace("\n", "<br>")
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,'
        f'Arial,sans-serif;white-space:normal;">{body}</div>'
    )


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def sanitize_thread_token(token: Any) -> str:
    t = str(token or "").strip().strip("<>").strip()
    if not t:
        return ""
    return _THREAD_TOKEN_KEEP.sub("_", t)[:120]


def email_chat_id(address: str, thread_token: Any) -> str:
    disc = sanitize_thread_token(thread_token)
    addr = norm_email(address)
    return f"{addr}#{disc}" if disc else addr


def recipient_from_email_chat_id(chat_id: str) -> str:
    rest = chat_id
    if rest.startswith("email:"):
        rest = rest[len("email:"):]
    if "#" in rest:
        addr_part, disc_part = rest.split("#", 1)
        if ":" in disc_part:
            disc_part = disc_part.split(":", 1)[0]
        rest = f"{addr_part}#{disc_part}" if disc_part else addr_part
    address = rest.split("#", 1)[0].strip()
    return address if is_deliverable_email(address) else ""


def normalize_sms_chat_id(raw: str) -> str:
    s = (raw or "").strip()
    if not s or looks_like_directory_label(s):
        return ""
    if s.startswith("sms:"):
        s = s[len("sms:"):]
    if "*" in s:
        return ""
    number = normalize_phone_e164(s)
    return number if is_plausible_sms_number(number) else ""


def normalize_email_chat_id(raw: str) -> str:
    s = (raw or "").strip()
    if not s or looks_like_directory_label(s):
        return ""
    if s.startswith("email:"):
        s = s[len("email:"):]
    if "#" in s:
        addr = recipient_from_email_chat_id(s)
        if not addr:
            return ""
        _, _, disc = s.partition("#")
        disc = disc.split(":", 1)[0].strip()
        disc = sanitize_thread_token(disc) if disc else ""
        return f"{addr}#{disc}" if disc else addr
    if "@" in s:
        addr = parse_sender_email(s) or (norm_email(s) if is_deliverable_email(s) else "")
        return addr if addr else ""
    return ""


def cfg_value(config, env_key: str, extra_key: str, default: Any = "") -> Any:
    extra = getattr(config, "extra", {}) or {}
    env_val = os.getenv(env_key)
    if env_val is not None and env_val != "":
        return env_val
    return extra.get(extra_key, default)


def lazy_installs_allowed() -> bool:
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1":
        return False
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        return True
    sec = (cfg.get("security") or {}) if isinstance(cfg, dict) else {}
    return bool(sec.get("allow_lazy_installs", True))


def venv_pip_install(specs: List[str], *, timeout: int = INSTALL_TIMEOUT) -> Tuple[bool, str]:
    if not specs:
        return True, ""
    venv_root = Path(sys.executable).parent.parent
    uv_env = {**os.environ, "VIRTUAL_ENV": str(venv_root)}
    uv_bin = shutil.which("uv")
    if uv_bin:
        try:
            r = subprocess.run(
                [uv_bin, "pip", "install", "--python", sys.executable, *specs],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=uv_env,
            )
            if r.returncode == 0:
                return True, ""
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    pip_cmd = [sys.executable, "-m", "pip"]
    try:
        probe = subprocess.run(pip_cmd + ["--version"], capture_output=True, text=True, timeout=15)
        if probe.returncode != 0:
            raise FileNotFoundError("pip not in venv")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            subprocess.run(
                [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
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


def ensure_importable(import_name: str, pip_spec: str) -> bool:
    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        pass
    if not lazy_installs_allowed():
        logger.error(
            "Pingram: '%s' is not installed and lazy installs are disabled. "
            "Install manually: uv pip install %s",
            import_name,
            pip_spec,
        )
        return False
    logger.info("Pingram: installing %s into the active venv ...", pip_spec)
    ok, err = venv_pip_install([pip_spec])
    if not ok:
        logger.error("Pingram: failed to install %s: %s", pip_spec, (err or "").strip()[-800:])
        return False
    importlib.invalidate_caches()
    try:
        import importlib.metadata as md

        if hasattr(md, "_cache_clear"):
            md._cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        logger.error("Pingram: installed %s but '%s' is still not importable", pip_spec, import_name)
        return False


async def ensure_runtime_deps() -> bool:
    aio = await asyncio.to_thread(ensure_importable, AIOHTTP_IMPORT, AIOHTTP_PACKAGE)
    ping = await asyncio.to_thread(ensure_importable, PINGRAM_IMPORT, PINGRAM_PACKAGE)
    return aio and ping


def check_shared_requirements() -> bool:
    deps_ok = ensure_importable(AIOHTTP_IMPORT, AIOHTTP_PACKAGE) and ensure_importable(
        PINGRAM_IMPORT, PINGRAM_PACKAGE
    )
    return bool(os.getenv("PINGRAM_API_KEY")) and deps_ok
