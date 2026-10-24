"""router/route_schema.py — the four intents, as a validated shape.

Deck (workflow A step 3): the router may be a model, but its output must
be JSON against a schema — `intent` restricted to exactly these four —
and bad JSON falls back to `faq`. This file is that contract; nothing
downstream trusts a router output that fails it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Intent = Literal["booking", "faq", "handoff", "out_of_scope"]
VALID_INTENTS = ("booking", "faq", "handoff", "out_of_scope")

SCHEMA_DOC = {
    "type": "object",
    "required": ["intent", "confidence"],
    "properties": {
        "intent": {"type": "string", "enum": list(VALID_INTENTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
}


@dataclass(frozen=True)
class Route:
    intent: str
    confidence: float
    reason: str
    source: str = "rules"  # rules | model | guardrail | fallback


def validate_model_json(obj) -> Route | None:
    """A model's routing JSON, trusted only through this gate. Bad shape → None."""
    if not isinstance(obj, dict):
        return None
    intent = obj.get("intent")
    conf = obj.get("confidence")
    if intent not in VALID_INTENTS:
        return None
    if not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
        return None
    return Route(intent, float(conf), str(obj.get("reason", "model")), "model")
