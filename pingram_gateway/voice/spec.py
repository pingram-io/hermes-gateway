"""Attach a Hermes briefing to a Voice Agent from the Pingram app."""

from __future__ import annotations

import copy
from typing import Any, Dict

_MAX_OPENER_CHARS = 1200


def overlay_briefing(spec: Dict[str, Any], briefing: str) -> Dict[str, Any]:
    """Copy the Pingram app agent spec and attach this call's spoken task."""
    out = copy.deepcopy(spec)
    text = (briefing or "").strip()
    if not text:
        return out

    instructions = str(out.get("instructions") or "").rstrip()
    out["instructions"] = (
        f"{instructions}\n\n---\n"
        "Hermes started this outbound call with the following briefing. "
        "Your opener should already be speaking it.\n\n"
        f"{text}"
    )

    outbound = dict(out.get("outbound") or {})
    outbound["firstAction"] = "speak"
    outbound["opener"] = _spoken_opener(text)
    out["outbound"] = outbound
    return out


def _spoken_opener(text: str) -> str:
    if len(text) <= _MAX_OPENER_CHARS:
        return text
    return text[: _MAX_OPENER_CHARS - 1].rstrip() + "…"
