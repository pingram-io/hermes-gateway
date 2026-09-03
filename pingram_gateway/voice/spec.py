"""Default Pingram Voice Agent spec for Hermes outbound calls.

Matches the dashboard playground default (s2s openai:gpt-realtime, voice marin).
The Hermes send_message text is a briefing for the live agent — not TTS of a script.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

# Dashboard DEFAULT_AGENT_SPEC / DEFAULT_S2S_MODEL (voiceAgentOptions.ts).
_DEFAULT_INSTRUCTIONS = (
    "You are Hermes, a helpful phone assistant. Keep your responses concise and "
    "conversational — this is a voice call, not a text chat.\n\n"
    "- Keep responses to 1-2 sentences when possible\n"
    "- Be friendly and natural"
)

_DEFAULT_OPENER = "Hi, this is your Hermes assistant calling. How can I help today?"

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

_MAX_SPOKEN_OPENER_CHARS = 400


def default_spec_dict() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_HERMES_VOICE_SPEC)


def overlay_briefing(spec: Dict[str, Any], briefing: str) -> Dict[str, Any]:
    """Copy a spec and attach the Hermes message as this call's task."""
    out = copy.deepcopy(spec)
    text = (briefing or "").strip()
    if not text:
        return out

    instructions = str(out.get("instructions") or "").rstrip()
    out["instructions"] = (
        f"{instructions}\n\n---\n"
        "Hermes started this outbound call with the following briefing. "
        "Carry it out conversationally. Do not read the briefing verbatim unless "
        "it is clearly a scripted greeting.\n\n"
        f"{text}"
    )

    outbound = dict(out.get("outbound") or {})
    if _looks_like_spoken_opener(text):
        outbound["opener"] = text
        outbound.setdefault("firstAction", "speak")
        outbound.setdefault("voicemailAction", "hangup")
    elif not outbound.get("opener"):
        outbound["opener"] = _DEFAULT_OPENER
        outbound.setdefault("firstAction", "speak")
        outbound.setdefault("voicemailAction", "hangup")
    out["outbound"] = outbound
    return out


def _looks_like_spoken_opener(text: str) -> bool:
    if len(text) > _MAX_SPOKEN_OPENER_CHARS:
        return False
    first = text.lstrip()[:12].lower()
    return first.startswith(("hi", "hey", "hello", "good "))
