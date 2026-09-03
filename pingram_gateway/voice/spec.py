"""Fallback Voice Agent spec when the Pingram account has no saved agents.

Matches the dashboard playground default (s2s openai:gpt-realtime, voice marin).
Saved-agent calls use the Pingram spec as-is; Hermes only overlays the briefing.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

# Dashboard DEFAULT_AGENT_SPEC / DEFAULT_S2S_MODEL (voiceAgentOptions.ts).
_DEFAULT_INSTRUCTIONS = (
    "You are Hermes, a helpful phone assistant on a live voice call.\n\n"
    "- Deliver the call briefing in your first spoken turn. Do not open with "
    "\"how can I help\" until you have done that.\n"
    "- Keep later responses to 1-2 sentences. Be friendly and natural."
)

_DEFAULT_OPENER = "Hi, this is your Hermes assistant calling."

_MAX_OPENER_CHARS = 1200

DEFAULT_HERMES_VOICE_SPEC: Dict[str, Any] = {
    "name": "Hermes",
    "instructions": _DEFAULT_INSTRUCTIONS,
    "inbound": {
        "firstAction": "speak",
        "greeting": "Hi, this is Hermes. How can I help you today?",
    },
    "outbound": {
        "firstAction": "speak",
        "opener": _DEFAULT_OPENER,
        "voicemailAction": "hangup",
    },
    "model": {
        "mode": "s2s",
        "model": "openai:gpt-realtime",
        "voiceId": "marin",
        "temperature": 0.8,
        "maxTokens": 250,
    },
    "tools": [],
    "variables": [],
    "conversation": {
        "turnDetection": "semantic",
        "minEndOfTurnSilenceMs": 500,
        "allowInterruptions": True,
        "minInterruptionDurationMs": 300,
        "silenceTimeoutSeconds": 30,
        "maxCallLengthSeconds": 600,
        "agentCanEndCall": True,
    },
    "compliance": {"recordingEnabled": True},
}


def default_spec_dict() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_HERMES_VOICE_SPEC)


def overlay_briefing(spec: Dict[str, Any], briefing: str) -> Dict[str, Any]:
    """Copy a spec and attach the Hermes message as this call's spoken task.

    Does not change model, voice, tokens, hang-up, voicemail, or conversation
    settings — those come from the Pingram Voice Agent.
    """
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
