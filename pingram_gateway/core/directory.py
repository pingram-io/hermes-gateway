"""Channel directory helpers for Pingram SMS/Email platforms."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pingram_gateway.core.constants import PLATFORM_EMAIL, PLATFORM_SMS
from pingram_gateway.core.helpers import (
    normalize_email_chat_id,
    normalize_sms_chat_id,
    parse_sender_email,
    redact_user,
)

logger = logging.getLogger(__name__)

_LEGACY_PLATFORM = "pingram"


def _hermes_home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        return get_hermes_home()
    except ImportError:
        return Path(os.path.expanduser("~/.hermes"))


def home_from_env(platform_name: str) -> Optional[Dict[str, str]]:
    if platform_name == PLATFORM_SMS:
        chat_id = os.getenv("PINGRAM_SMS_HOME_CHANNEL", "").strip()
        name = os.getenv("PINGRAM_SMS_HOME_CHANNEL_NAME", "").strip() or "Home"
    elif platform_name == PLATFORM_EMAIL:
        chat_id = os.getenv("PINGRAM_EMAIL_HOME_CHANNEL", "").strip()
        name = os.getenv("PINGRAM_EMAIL_HOME_CHANNEL_NAME", "").strip() or "Home"
    else:
        return None
    if not chat_id:
        return None
    return {"chat_id": chat_id, "name": name}


def home_from_config(config, platform_name: str) -> Optional[Dict[str, str]]:
    try:
        from gateway.config import Platform

        home = config.get_home_channel(Platform(platform_name))
    except Exception:
        home = None
    if home and home.chat_id:
        return {"chat_id": str(home.chat_id), "name": str(home.name or "Home")}
    return home_from_env(platform_name)


def ensure_home_channel(config, platform_name: str) -> None:
    """Ensure gateway config has a home channel (env fallback, Weixin-style)."""
    if platform_name not in {PLATFORM_SMS, PLATFORM_EMAIL}:
        return
    try:
        from gateway.config import HomeChannel, Platform
    except ImportError:
        return

    platform = Platform(platform_name)
    pconfig = config.platforms.get(platform)
    if not pconfig:
        return
    if pconfig.home_channel and pconfig.home_channel.chat_id:
        return
    home = home_from_env(platform_name)
    if not home:
        return
    pconfig.home_channel = HomeChannel(
        platform=platform,
        chat_id=home["chat_id"],
        name=home["name"],
    )


def _legacy_session_entries(platform_name: str) -> List[Dict[str, Any]]:
    sessions_path = _hermes_home() / "sessions" / "sessions.json"
    if not sessions_path.exists():
        return []

    entries: List[Dict[str, Any]] = []
    seen_ids = set()
    try:
        with open(sessions_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        logger.debug("Pingram directory: failed to read sessions.json", exc_info=True)
        return []

    if not isinstance(data, dict):
        return []

    for key, session in data.items():
        # Hermes writes a string "_README" sentinel into sessions.json.
        # Keys starting with "_" and non-dict values are not session entries.
        if str(key).startswith("_") or not isinstance(session, dict):
            continue
        origin = session.get("origin") or {}
        if origin.get("platform") != _LEGACY_PLATFORM:
            continue
        raw_chat_id = str(origin.get("chat_id") or "")
        if platform_name == PLATFORM_SMS:
            if raw_chat_id.lower().startswith("email:"):
                continue
            chat_id = normalize_sms_chat_id(raw_chat_id)
            if not chat_id:
                continue
        else:
            if raw_chat_id.lower().startswith("sms:"):
                continue
            chat_id = _legacy_email_chat_id(raw_chat_id, origin)
            if not chat_id:
                continue

        entry_id = chat_id
        thread_id = origin.get("thread_id")
        if platform_name == PLATFORM_EMAIL and thread_id:
            token = str(thread_id).strip("<>")
            if token and "#" not in entry_id:
                entry_id = f"{entry_id}#{token}"

        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)

        display = origin.get("chat_name") or origin.get("user_name") or redact_user(chat_id)
        if looks_like_junk_label(str(display)):
            display = redact_user(chat_id)

        entry: Dict[str, Any] = {
            "id": entry_id,
            "name": display,
            "type": session.get("chat_type", "dm"),
        }
        if thread_id and platform_name == PLATFORM_EMAIL:
            entry["thread_id"] = thread_id
        entries.append(entry)
    return entries


def _legacy_email_chat_id(raw_chat_id: str, origin: Dict[str, Any]) -> str:
    chat_id = normalize_email_chat_id(raw_chat_id)
    if chat_id and "@" in chat_id.split("#", 1)[0]:
        return chat_id
    user = parse_sender_email(origin.get("user_id")) or parse_sender_email(origin.get("user_name"))
    if not user:
        return ""
    thread_id = origin.get("thread_id")
    token = str(thread_id or "").strip("<>")
    if token:
        return f"{user}#{token}"
    return user


def looks_like_junk_label(label: str) -> bool:
    s = (label or "").strip()
    if not s:
        return True
    if "*" in s:
        return True
    if s.lower().startswith("email:") or s.lower().startswith("sms:"):
        return True
    if "<" in s and "@" in s:
        return True
    return False


def _entry_key(entry: Dict[str, Any]) -> str:
    entry_id = str(entry.get("id") or "")
    thread_id = entry.get("thread_id")
    if thread_id:
        return f"{entry_id}:{thread_id}"
    return entry_id


def collect_directory_entries(platform_name: str, config=None) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    seen = set()

    home = home_from_env(platform_name)
    if config is not None:
        home = home_from_config(config, platform_name) or home
    if home:
        entry = {
            "id": home["chat_id"],
            "name": home["name"],
            "type": "home",
        }
        key = _entry_key(entry)
        if key not in seen:
            seen.add(key)
            entries.append(entry)

    for entry in _legacy_session_entries(platform_name):
        key = _entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)

    directory_path = _hermes_home() / "channel_directory.json"
    if directory_path.exists():
        try:
            with open(directory_path, encoding="utf-8") as handle:
                directory = json.load(handle)
            for entry in directory.get("platforms", {}).get(platform_name, []):
                key = _entry_key(entry)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(entry)
        except Exception:
            logger.debug("Pingram directory: failed to read channel_directory.json", exc_info=True)

    return entries


def format_platform_targets(platform_name: str, entries: List[Dict[str, Any]]) -> List[str]:
    if not entries:
        return []
    label = "Pingram SMS" if platform_name == PLATFORM_SMS else "Pingram Email"
    home_hint = "SMS" if platform_name == PLATFORM_SMS else "email"
    lines = [f"{label}:"]
    for entry in entries:
        name = entry.get("name") or entry.get("id")
        entry_type = entry.get("type") or "dm"
        if entry_type == "home":
            lines.append(f"  {platform_name}  (home channel — use for proactive {home_hint})")
        else:
            lines.append(f"  {platform_name}:{name} ({entry_type})")
    lines.append("")
    return lines


def format_list_supplement(config=None) -> str:
    blocks: List[str] = []
    for platform_name in (PLATFORM_SMS, PLATFORM_EMAIL):
        entries = collect_directory_entries(platform_name, config=config)
        if entries:
            blocks.extend(format_platform_targets(platform_name, entries))
    return "\n".join(blocks).rstrip()


def seed_platform_directory(platform_name: str, config) -> None:
    """Merge home channel + legacy sessions into channel_directory.json."""
    entries = collect_directory_entries(platform_name, config=config)
    if not entries:
        return

    directory_path = _hermes_home() / "channel_directory.json"
    directory: Dict[str, Any] = {"updated_at": None, "platforms": {}}
    if directory_path.exists():
        try:
            with open(directory_path, encoding="utf-8") as handle:
                directory = json.load(handle)
        except Exception:
            logger.debug("Pingram directory: failed to load existing directory", exc_info=True)

    platforms = directory.setdefault("platforms", {})
    existing = { _entry_key(item): item for item in platforms.get(platform_name, []) }
    for entry in entries:
        existing.setdefault(_entry_key(entry), entry)
    platforms[platform_name] = list(existing.values())
    directory["updated_at"] = datetime.now().isoformat()

    try:
        directory_path.parent.mkdir(parents=True, exist_ok=True)
        with open(directory_path, "w", encoding="utf-8") as handle:
            json.dump(directory, handle, indent=2)
            handle.write("\n")
    except Exception:
        logger.debug("Pingram directory: failed to write channel_directory.json", exc_info=True)
