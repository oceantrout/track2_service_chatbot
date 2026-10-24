"""core/gateway.py — every model call, through one metered door.

Deck (cross-cutting): max 2 model calls per message, with tokens, cost and
latency recorded. This gateway enforces the ceiling structurally: the
counter resets each turn, and a third call raises instead of quietly
spending. Handoff, out_of_scope and guardrail blocks use 0 calls, and the
default build (rules-first, deviation §11) uses 0 for every judged turn —
the ceiling then reads as headroom, not usage.

The client is graphrag_demo.llm (Anthropic-compatible endpoint); no model
call happens unless someone constructs Gateway(use_model=True) — in the
sandbox the acceptor runs with the default off.
"""

from __future__ import annotations

import json
import re
import time

from core import config
from router.route_schema import Route, validate_model_json


class CallBudgetExceeded(Exception):
    pass


class Gateway:
    def __init__(self, use_model: bool = False):
        self.use_model = use_model
        self._client = None
        self.calls_this_turn = 0
        self.turns_tokens = {"in": 0, "out": 0}
        self.turn_start = time.perf_counter()
        self.last_latency_ms = 0.0

    # ---- turn lifecycle ------------------------------------------------------

    def start_turn(self) -> None:
        self.calls_this_turn = 0
        self.turns_tokens = {"in": 0, "out": 0}
        self.turn_start = time.perf_counter()

    def _client_lazy(self):
        if self._client is None:
            from graphrag_demo.llm import LLMClient  # shared client, one config

            self._client = LLMClient()
        return self._client

    def complete(self, prompt: str, max_tokens: int = 400) -> str:
        if not self.use_model:
            raise RuntimeError("gateway asked for a model call while use_model=False")
        if self.calls_this_turn >= config.MAX_MODEL_CALLS_PER_TURN:
            raise CallBudgetExceeded(
                f"model-call ceiling ({config.MAX_MODEL_CALLS_PER_TURN}) hit this turn"
            )
        self.calls_this_turn += 1
        t0 = time.perf_counter()
        text, usage = self._client_lazy().complete_with_usage(prompt, max_tokens=max_tokens)
        self.last_latency_ms = (time.perf_counter() - t0) * 1000
        self.turns_tokens["in"] += usage.get("input_tokens", 0)
        self.turns_tokens["out"] += usage.get("output_tokens", 0)
        return text

    # ---- the one structured job a model gets: routing -----------------------

    def route_json(self, text: str) -> Route | None:
        """Model suggestion through the schema gate; any failure → None →
        the rules fallback owns the decision (deck: bad JSON → faq)."""
        if not self.use_model:
            return None
        prompt = (
            "You are a router for a car-workshop chatbot. Before routing, "
            "silently correct any spelling errors or typos in the customer "
            "message so you interpret what the customer meant; never mention "
            "the correction. Reply with JSON "
            '{"intent": one of ["booking","faq","handoff","out_of_scope"], '
            '"confidence": 0..1, "reason": short string}. No other text. '
            "Customer message: " + text
        )
        try:
            raw = self.complete(prompt, max_tokens=120)
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                return None
            return validate_model_json(json.loads(match.group(0)))
        except Exception:
            return None
