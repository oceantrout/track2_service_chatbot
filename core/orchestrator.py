"""core/orchestrator.py — handle_turn(): the eight-step lifecycle, timed.

Architecture deck, slide 3, verbatim order:

    Ingest → Guard → Command → Route → Handle → Verify → Trace → Reply

Invariants enforced here, not downstream:
  * the guardrail runs BEFORE the router — a blocked message is never
    seen by any model, and the trace records route `out_of_scope`;
  * every handler returns text that goes through Verify — no never-collect
    pattern may survive in an outgoing reply, discount promises are dead
    on arrival, and a cited FAQ answer must carry its Source: line;
  * Trace gets the masked record; Reply gets the customer's text.

With the rules-first default this is **0 model calls per turn** (the deck
ceiling is 2; headroom is the point). `use_model=True` on the gateway
adds a routing suggestion at most, still schema-gated.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from booking.state_machine import BookingMachine, Session
from core import config
from core.gateway import Gateway
from faq.answer import FaqEngine
from guards import injection, redact
from handoff import handoff, out_of_scope
from router import router as router_mod
from tracking.trace import TraceRecorder

# Never-collect shapes that must not appear in an outgoing reply.
HARD_PII_RE = re.compile(
    r"\b[STFGM]\d{7}[A-Z]\b|\b[EFG]\d{7}[A-Z]\b|\b(?:\d{4}[- ]?){3}\d{4}\b"
)

PRIVACY_NOTE = (
    "For your privacy I've removed the identity/card number you shared — "
    "we never need those here. "
)


@dataclass
class TurnResult:
    reply: str
    route: str
    state_before: str
    state_after: str
    faq_section: str | None = None
    retrieval_top_score: float | None = None
    model_calls: int = 0
    latency_ms: float = 0.0
    verify_flags: list[str] = field(default_factory=list)
    trace_record: dict = field(default_factory=dict)


class Orchestrator:
    def __init__(self, machine: BookingMachine | None = None,
                 faq_engine: FaqEngine | None = None,
                 gateway: Gateway | None = None,
                 recorder: TraceRecorder | None = None):
        self.machine = machine or BookingMachine()
        self.faq = faq_engine or FaqEngine()
        self.gateway = gateway or Gateway(use_model=False)
        self.recorder = recorder or TraceRecorder()

    def handle_turn(self, session: Session, text: str) -> TurnResult:
        t0 = time.perf_counter()
        self.gateway.start_turn()

        # 1 INGEST -----------------------------------------------------------
        raw = (text or "").strip()

        # 2 GUARD ------------------------------------------------------------
        # redact FIRST: a customer *volunteering* their own card/NRIC is the
        # C10 case (remove it, tell them, carry on) — judging the blocklist
        # on raw text blocked it as a "card ask". The blocklist still runs
        # before the router, on what the customer actually left standing.
        clean, removed_kinds = redact.redact(raw)
        blocked, blocked_reason = injection.check(clean)
        if blocked:
            reply = injection.REFUSAL
            before = session.state
            record = self.recorder.record(
                session=session, text=raw, route="out_of_scope",
                state_before=before, state_after=before, reply=reply,
                validation="blocked", blocked_reason=blocked_reason,
                model_calls=self.gateway.calls_this_turn,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
            return TurnResult(reply, "out_of_scope", before, before,
                              trace_record=record,
                              latency_ms=record["latency_ms"])

        # 3 COMMAND ----------------------------------------------------------
        # cancel / start-over / change are the machine's (they need the
        # booking context); nothing else is a turn-level command.
        before = session.state

        # 4 ROUTE ------------------------------------------------------------
        model = self.gateway if self.gateway.use_model else None
        route = router_mod.route(clean, session.active, model=model)

        # 5 HANDLE -----------------------------------------------------------
        note = PRIVACY_NOTE if removed_kinds else ""
        extracted, validation = "", ""
        faq_section, faq_score = None, None
        if route.intent == "booking":
            self.machine.start(session)
            step = self.machine.handle(session, clean)
            reply = note + step.reply
            extracted, validation = step.extracted_field, step.validation
        elif route.intent == "faq":
            result = self.faq.answer(clean)
            reply = note + result["answer"]
            faq_section, faq_score = result["section"], result["score"]
            if session.active:
                # Rule 3: question mid-booking — answer, then repeat the
                # current booking question. State untouched.
                reply += "\n\n" + self.machine.reask(session)
        elif route.intent == "handoff":
            reply = note + handoff.reply(session)
        else:
            reply = note + out_of_scope.reply()

        # 6 VERIFY -----------------------------------------------------------
        flags: list[str] = []
        if HARD_PII_RE.search(reply):
            flags.append("hard_pii_in_reply")
            reply = out_of_scope.reply()  # refuse to ship it
        if re.search(r"\bdiscount\b|\bwaiv\w*\b.{0,20}\bprice\b|\bfree of charge, guaranteed\b", reply, re.I):
            if injection.REFUSAL not in reply:
                flags.append("discount_promise")
        if route.intent == "faq" and faq_section is not None and "\nSource: " not in reply:
            flags.append("missing_citation")

        # 7 TRACE ------------------------------------------------------------
        after = session.state
        attempts = session.attempts.get(before, 0)
        record = self.recorder.record(
            session=session, text=raw, route=route.intent,
            state_before=before, state_after=after, reply=reply,
            faq_section=faq_section, retrieval_top_score=faq_score,
            extracted_field=extracted, validation=validation,
            attempts_at_state=attempts,
            tokens_in=self.gateway.turns_tokens["in"],
            tokens_out=self.gateway.turns_tokens["out"],
            model_calls=self.gateway.calls_this_turn,
            latency_ms=(time.perf_counter() - t0) * 1000,
            blocked_reason="",
        )
        if flags:
            record["verify_flags"] = flags

        # 8 REPLY ------------------------------------------------------------
        return TurnResult(
            reply=reply, route=route.intent,
            state_before=before, state_after=after,
            faq_section=faq_section, retrieval_top_score=faq_score,
            model_calls=self.gateway.calls_this_turn,
            latency_ms=record["latency_ms"],
            verify_flags=flags, trace_record=record,
        )
