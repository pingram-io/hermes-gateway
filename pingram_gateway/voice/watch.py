"""Watch Hermes-placed Voice Agent calls and turn finished records into messages."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PENDING_MAX_AGE_SECONDS = 45 * 60
DONE_TTL_SECONDS = 24 * 3600


def _hermes_home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        return get_hermes_home()
    except ImportError:
        return Path(os.path.expanduser("~/.hermes"))


def _watch_path() -> Path:
    return _hermes_home() / "pingram_voice_watches.json"


def _load() -> Dict[str, Any]:
    path = _watch_path()
    if not path.exists():
        return {"pending": {}, "done": {}}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        logger.debug("Pingram Voice: failed to read call watches", exc_info=True)
        return {"pending": {}, "done": {}}
    if not isinstance(data, dict):
        return {"pending": {}, "done": {}}
    pending = data.get("pending") if isinstance(data.get("pending"), dict) else {}
    done = data.get("done") if isinstance(data.get("done"), dict) else {}
    return {"pending": pending, "done": done}


def _save(data: Dict[str, Any]) -> None:
    path = _watch_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        logger.debug("Pingram Voice: failed to write call watches", exc_info=True)


def watch_call(tracking_id: str, chat_id: str) -> None:
    tid = (tracking_id or "").strip()
    number = (chat_id or "").strip()
    if not tid or not number:
        return
    data = _load()
    if tid in data["done"]:
        return
    data["pending"][tid] = {"chat_id": number, "placed_at": time.time()}
    _prune_done(data)
    _save(data)


def list_pending() -> List[Tuple[str, str, float]]:
    data = _load()
    out: List[Tuple[str, str, float]] = []
    for tid, entry in list(data["pending"].items()):
        if not isinstance(entry, dict):
            continue
        chat_id = str(entry.get("chat_id") or "").strip()
        if not chat_id:
            continue
        placed = float(entry.get("placed_at") or time.time())
        out.append((tid, chat_id, placed))
    return out


def mark_done(tracking_id: str) -> None:
    tid = (tracking_id or "").strip()
    if not tid:
        return
    data = _load()
    data["pending"].pop(tid, None)
    data["done"][tid] = time.time()
    _prune_done(data)
    _save(data)


def _prune_done(data: Dict[str, Any]) -> None:
    now = time.time()
    done = data.get("done") or {}
    data["done"] = {k: v for k, v in done.items() if now - float(v or 0) < DONE_TTL_SECONDS}


def format_call_report(call: Any, *, expired: bool = False) -> str:
    if expired or call is None:
        return (
            "[Pingram Voice] Timed out waiting for a finished call record. "
            "The call may still be in the dashboard Voice history."
        )

    status = str(getattr(call, "status", "") or "unknown")
    outcome = str(getattr(call, "outcome", "") or "") or "unknown"
    end_reason = str(getattr(call, "end_reason", "") or "")
    end_detail = str(getattr(call, "end_detail", "") or "")
    to_number = str(getattr(call, "to", "") or "")
    duration = getattr(call, "duration_seconds", None)
    try:
        duration_s = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_s = None

    lines = ["[Pingram Voice call ended — FYI only. Do not place another call unless the user asked.]"]
    if to_number:
        lines.append(f"To: {to_number}")
    lines.append(f"Status: {status}")
    lines.append(f"Outcome: {outcome}")
    if end_reason:
        lines.append(f"End reason: {end_reason}")
    if end_detail:
        lines.append(f"Detail: {end_detail}")
    if duration_s is not None:
        lines.append(f"Duration: {duration_s}s")

    transcript = _transcript_lines(getattr(call, "timeline", None) or [])
    if transcript:
        lines.append("")
        lines.append("Transcript:")
        lines.extend(transcript)
    else:
        lines.append("")
        lines.append("No speech transcript was stored for this call.")
    return "\n".join(lines)


def _transcript_lines(timeline: List[Any]) -> List[str]:
    rows: List[str] = []
    for item in timeline:
        kind = str(getattr(item, "type", "") or "").lower()
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            continue
        if kind == "user":
            rows.append(f"Them: {text}")
        elif kind == "assistant":
            rows.append(f"Agent: {text}")
        elif kind == "tool" and text:
            name = str(getattr(item, "tool_name", "") or "tool")
            rows.append(f"Tool ({name}): {text}")
    return rows
