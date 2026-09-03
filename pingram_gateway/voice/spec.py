"""Default Pingram Voice Agent spec for Hermes outbound calls.

Matches the dashboard playground default (s2s openai:gpt-realtime, voice marin),
with stay-on-the-line defaults so Hermes-placed calls are not dropped by AMD or
the built-in end_call tool.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

# Dashboard DEFAULT_AGENT_SPEC / DEFAULT_S2S_MODEL (voiceAgentOptions.ts).
_DEFAULT_INSTRUCTIONS = (
    "You are Hermes, a helpful phone assistant on a live voice call.\n\n"
    "- Deliver the call briefing in your first spoken turn. Do not open with "
    "\"how can I help\" until you have done that.\n"
    "- Then stay on the line and wait for the person to speak. Do not hang up "
    "after the greeting or after delivering the briefing.\n"
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
        "voicemailAction": "continue",
    },
    "model": {
        "mode": "s2s",
        "model": "openai:gpt-realtime",
        "voiceId": "marin",
        "temperature": 0.8,
        "maxTokens": 4096,
    },
    "tools": [],
    "variables": [],
    "conversation": {
        "turnDetection": "semantic",
        "minEndOfTurnSilenceMs": 500,
        "allowInterruptions": True,
        "minInterruptionDurationMs": 300,
        "silenceTimeoutSeconds": 90,
        "maxCallLengthSeconds": 600,
        "agentCanEndCall": False,
    },
    "compliance": {"recordingEnabled": True},
}


def default_spec_dict() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_HERMES_VOICE_SPEC)


def overlay_briefing(spec: Dict[str, Any], briefing: str) -> Dict[str, Any]:
    """Copy a spec and attach the Hermes message as this call's spoken task."""
    out = copy.deepcopy(spec)
    text = (briefing or "").strip()
    _apply_stay_on_line(out)
    if not text:
        return out

    instructions = str(out.get("instructions") or "").rstrip()
    out["instructions"] = (
        f"{instructions}\n\n---\n"
        "Hermes started this outbound call with the following briefing. "
        "Your opener should already be speaking it. After that, stay on the "
        "line and wait for a reply. Do not hang up or ask to end the call "
        "until the person is clearly finished.\n\n"
        f"{text}"
    )

    outbound = dict(out.get("outbound") or {})
    outbound["firstAction"] = "speak"
    outbound["opener"] = _spoken_opener(text)
    outbound["voicemailAction"] = "continue"
    out["outbound"] = outbound
    return out


def _apply_stay_on_line(spec: Dict[str, Any]) -> None:
    conversation = dict(spec.get("conversation") or {})
    conversation["agentCanEndCall"] = False
    try:
        silence = int(conversation.get("silenceTimeoutSeconds") or 0)
    except (TypeError, ValueError):
        silence = 0
    conversation["silenceTimeoutSeconds"] = max(silence, 90)
    spec["conversation"] = conversation

    model = dict(spec.get("model") or {})
    try:
        max_tokens = int(model.get("maxTokens") or 0)
    except (TypeError, ValueError):
        max_tokens = 0
    model["maxTokens"] = max(max_tokens, 4096)
    spec["model"] = model


def _spoken_opener(text: str) -> str:
    if len(text) <= _MAX_OPENER_CHARS:
        return text
    return text[: _MAX_OPENER_CHARS - 1].rstrip() + "…"
