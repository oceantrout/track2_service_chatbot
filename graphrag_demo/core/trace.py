"""G9b - Trace.

Every workflow run writes one trace record: the route chosen, the
evidence retrieved, the citations verified, the confidence, the latency
per step, and the token cost. The trace is the artefact that turns "the
bot answered" into "we can show how and for how much".

Two hard rules, from the README and the deck:

* **PII never enters the trace.** The message is masked in code before it
  is written. The masking is a regex over the known PII shapes - it does
  not ask a model, because a model cannot be trusted to remember to
  forget.
* The trace is append-only JSONL, so a bad run is evidence, not an
  overwritten silence.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .guardrail import mask_pii


@dataclass
class TraceStep:
    """One step of Workflow A."""

    name: str
    started_at: float
    duration_ms: float = 0.0
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "step": self.name,
            "duration_ms": round(self.duration_ms, 2),
            **self.detail,
        }


class StepTimer:
    """Context manager that times one workflow step."""

    def __init__(self, trace: "Trace", name: str):
        self.trace = trace
        self.name = name
        self.step = TraceStep(name=name, started_at=time.perf_counter())

    def __enter__(self) -> TraceStep:
        return self.step

    def __exit__(self, *exc_info) -> None:
        self.step.duration_ms = (time.perf_counter() - self.step.started_at) * 1000
        self.trace.steps.append(self.step)


class Trace:
    """Collects one workflow run and writes it, masked, to the trace log."""

    def __init__(self, message: str, path=None):
        self.raw_message = message  # kept only in memory, never written
        self.masked_message = mask_pii(message)
        self.path = path
        self.steps: list[TraceStep] = []
        self.result: dict = {}
        self.pii_found: list[dict] = []
        self.usage: dict | None = None

    def step(self, name: str) -> StepTimer:
        return StepTimer(self, name)

    def record_pii(self, findings: list[dict]) -> None:
        """Record *what kinds* of PII were found, never the values."""
        self.pii_found = [{"kind": item["kind"], "count": 1} for item in findings]

    def to_dict(self) -> dict:
        total_ms = sum(step.duration_ms for step in self.steps)
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": self.masked_message,
            "pii_detected": sorted({item["kind"] for item in self.pii_found}),
            "total_ms": round(total_ms, 2),
            "steps": [step.to_dict() for step in self.steps],
            "outcome": {
                "decision": self.result.get("decision", ""),
                "route": self.result.get("route", ""),
                "mode": self.result.get("mode", ""),
                "abstained": self.result.get("abstained", False),
                "confidence": self.result.get("confidence", 0.0),
                "citations_verified": len(self.result.get("citations", [])),
            },
            "usage": self.usage,
        }

    def write(self, path=None) -> dict:
        """Append the masked record to the trace log and return it."""
        record = self.to_dict()
        path = path or self.path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record
