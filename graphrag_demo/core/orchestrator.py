"""G9c - The orchestrator: Workflow A, six steps, all in code.

    INGEST -> GUARDRAIL -> ROUTER -> ROUTE HANDLER -> COMPOSE -> TRACE

The deck's central rule: the model writes language, the code owns every
decision. Each step is timed, and the whole run is written to the trace
log with PII masked before anything is persisted.

``route handler`` is the only step that touches retrieval, and ``compose``
is the only step that may touch a model - and even then only to phrase
material the handler already selected and the citation layer already
verified against the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from ..answer import (
    ABSTAIN_TEXT,
    AnswerEngine,
    compose_answer_deterministically,
    compose_answer_with_model,
)
from ..citation import build_citations
from ..query import QueryEngine
from ..router import Router
from .guardrail import Guardrail
from .trace import Trace

REFUSAL_TEXT = (
    "I can only answer questions about this workshop's services and "
    "policies, and I can't change my own rules or prices. If you need a "
    "price adjustment, please speak to our service desk."
)

OUT_OF_SCOPE_TEXT = (
    "That's outside what I can help with - I only know about this "
    "workshop's services, prices and policies. Ask me about those, or "
    "call the service desk on anything else."
)

HANDOFF_TEXT = (
    "I'm sorry - that sounds frustrating, and it's not something I should "
    "answer from a FAQ. Let me get a human to you: please call the service "
    "desk during opening hours (Mon-Fri 8:30am-6:00pm, Sat 8:30am-1:00pm) "
    "or leave your mobile number and we'll call you back."
)


@dataclass
class WorkflowResult:
    """Everything one run produced, for the front-end and the trace."""

    message: str
    decision: str  # faq | out_of_scope | handoff | blocked
    route: str
    mode: str
    answer: str
    abstained: bool
    confidence: float
    citations: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    communities: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    guardrail_findings: list = field(default_factory=list)
    composer: str = ""
    usage: dict | None = None
    trace_record: dict | None = None
    total_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "message": self.message,
            "decision": self.decision,
            "route": self.route,
            "mode": self.mode,
            "answer": self.answer,
            "abstained": self.abstained,
            "confidence": self.confidence,
            "citations": self.citations,
            "entities": self.entities[:5],
            "communities": self.communities[:5],
            "reasons": self.reasons,
            "guardrail_findings": self.guardrail_findings,
            "composer": self.composer,
            "usage": self.usage,
            "total_ms": round(self.total_ms, 2),
        }


class Orchestrator:
    """Runs Workflow A over one message."""

    def __init__(self, use_model: bool = False) -> None:
        self.guardrail = Guardrail()
        self.engine = QueryEngine()
        self.router = Router(self.engine)
        # The model phrases answers only when explicitly allowed; the
        # demo's default is the deterministic composer so a run is free.
        self.answer_engine = AnswerEngine(self.engine, llm=None)
        self.use_model = use_model

    def run(self, message: str, write_trace: bool = True) -> WorkflowResult:
        trace = Trace(message, path=config.TRACE_PATH if write_trace else None)

        # -- 1. INGEST ------------------------------------------------------
        with trace.step("ingest") as step:
            text = (message or "").strip()
            step.detail = {"characters": len(text)}

        return self._run_after_ingest(text, trace)

    def _run_after_ingest(self, text: str, trace: Trace) -> WorkflowResult:
        # -- 2. GUARDRAIL ---------------------------------------------------
        with trace.step("guardrail") as step:
            pii = self.guardrail.detect_pii(text)
            trace.record_pii(pii)
            decision = self.guardrail.check(text)
            step.detail = {
                "decision": decision["decision"],
                "reason": decision["reason"],
                "pii_types": sorted({item["kind"] for item in pii}),
            }

        if decision["decision"] != "faq":
            answer = {
                "blocked": REFUSAL_TEXT,
                "out_of_scope": OUT_OF_SCOPE_TEXT,
                "handoff": HANDOFF_TEXT,
            }[decision["decision"]]
            return self._finish(
                trace,
                decision=decision["decision"],
                route=decision["decision"],
                mode="-",
                answer=answer,
                abstained=False,
                confidence=1.0,
                reasons=[decision["reason"]],
                guardrail_findings=decision["findings"],
                composer="static",
            )

        # -- 3. ROUTER -------------------------------------------------------
        with trace.step("router") as step:
            routing = self.router.route(text)
            step.detail = {
                "mode": routing["mode"],
                "intent": routing["intent"],
                "confidence": routing["confidence"],
                "reasons": routing["reasons"],
            }

        # -- 4. ROUTE HANDLER (retrieval) -------------------------------------
        with trace.step("handler") as step:
            retrieval = self.engine.query(text, mode=routing["mode"], top_k=6)
            step.detail = {
                "hits": len(retrieval["evidence"]),
                "best_score": retrieval["best_score"],
                "abstain": retrieval["abstain"],
                "entities": [
                    item["name"] for item in retrieval["entities"][:3]
                ],
                "communities": [
                    item["community_id"] for item in retrieval["communities"][:3]
                ],
            }

        # -- 5. COMPOSE --------------------------------------------------------
        with trace.step("compose") as step:
            if retrieval["abstain"]:
                composed = {"answer": ABSTAIN_TEXT, "composer": "abstain"}
                citations = []
            else:
                evidence = retrieval["evidence"]
                llm = self.answer_engine.llm if self.use_model else None
                composed = (
                    compose_answer_with_model(text, evidence, llm)
                    or compose_answer_deterministically(text, evidence)
                )
                # Verified against the file on disk. An unverified quote
                # is dropped, never warned about.
                citations = build_citations(evidence)
            step.detail = {
                "composer": composed["composer"],
                "citations_verified": len(citations),
                "abstained": retrieval["abstain"],
            }

        return self._finish(
            trace,
            decision="faq",
            route="faq",
            mode=routing["mode"],
            answer=composed["answer"],
            abstained=retrieval["abstain"],
            confidence=retrieval["best_score"],
            citations=citations,
            evidence=retrieval["evidence"],
            entities=retrieval["entities"],
            communities=retrieval["communities"],
            reasons=routing["reasons"],
            guardrail_findings=decision["findings"],
            composer=composed["composer"],
        )

    # -- 6. TRACE ---------------------------------------------------------

    def _finish(self, trace: Trace, **fields) -> WorkflowResult:
        usage = None
        llm = getattr(self.answer_engine, "_llm", None)
        if llm is not None and getattr(llm, "usage", None) is not None:
            if llm.usage.calls:
                usage = {
                    **llm.usage.to_dict(),
                    "cost": llm.usage.format_cost(),
                }

        trace.result = {
            "decision": fields["decision"],
            "route": fields["route"],
            "mode": fields.get("mode", "-"),
            "abstained": fields.get("abstained", False),
            "confidence": fields.get("confidence", 0.0),
            "citations": fields.get("citations", []),
        }
        record = trace.write()

        return WorkflowResult(
            message=trace.masked_message,
            decision=fields["decision"],
            route=fields["route"],
            mode=fields.get("mode", "-"),
            answer=fields["answer"],
            abstained=fields.get("abstained", False),
            confidence=fields.get("confidence", 0.0),
            citations=fields.get("citations", []),
            evidence=fields.get("evidence", []),
            entities=fields.get("entities", []),
            communities=fields.get("communities", []),
            reasons=fields.get("reasons", []),
            guardrail_findings=fields.get("guardrail_findings", []),
            composer=fields.get("composer", ""),
            usage=usage,
            trace_record=record,
            total_ms=record["total_ms"],
        )


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    orchestrator = Orchestrator()
    for question in sys.argv[1:] or ["How much does a maintenance cost?"]:
        result = orchestrator.run(question)
        print(f"Q: {question}")
        print(f"   decision={result.decision} route={result.route} "
              f"mode={result.mode} conf={result.confidence:.2f}")
        print(f"   A: {result.answer}")
        for citation in result.citations:
            print(f"      [{citation['label']}]")
        print(f"   total {result.total_ms:.0f} ms")
        print()
