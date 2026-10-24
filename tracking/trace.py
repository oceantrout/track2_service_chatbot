"""tracking/trace.py — one JSONL line per turn, PII never raw.

Workplan §10's fields, masking formats deck-fixed: mobile `9*** 4567`,
email `w***@example.com`, plate `S****K`, NRIC/card `[removed]`. The name
stays raw — that is the deck's own example row. The message is masked at
construction, so no code path can log the raw turn by forgetting to.

(Deviation §11: package is `tracking/`, not the deck's `logging/` — a
package named `logging` shadows stdlib logging and breaks sklearn.)
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from core import config
from guards import redact


def mask_field(name: str, value: str) -> str:
    if value == "":
        return ""
    if name == "mobile":
        return redact.mask_mobile(value)
    if name == "email":
        return redact.mask_email(value)
    if name in ("vehicle_plate",):
        return redact.mask_plate(value)
    return value


class TraceRecorder:
    """Append-only JSONL. `path` overridable so run_acceptance can audit
    a separate file instead of the live log."""

    def __init__(self, path=config.TRACE_PATH):
        self.path = path
        self.turn = 0
        self.started_at = datetime.now().isoformat(timespec="seconds")

    def record(self, *, session, text, route, state_before, state_after,
               reply, faq_section=None, retrieval_top_score=None,
               extracted_field="", validation="", attempts_at_state=0,
               tokens_in=0, tokens_out=0, model_calls=0,
               latency_ms=0.0, blocked_reason="") -> dict:
        self.turn += 1
        masked_values = {}
        if session is not None:
            for field_name, value in session.filled.items():
                masked = mask_field(field_name, value)
                if masked != value:
                    masked_values[field_name] = masked
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "turn": self.turn,
            "message_masked": redact.mask_for_trace(text),
            "route": route,
            "state_before": state_before,
            "state_after": state_after,
            "faq_section_cited": faq_section,
            "retrieval_top_score": retrieval_top_score,
            "extracted_field": extracted_field,
            "validation": validation,
            "attempts_at_state": attempts_at_state,
            "model_calls_this_turn": model_calls,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": round(latency_ms, 1),
            "blocked_reason": blocked_reason,
            "masked_values": masked_values,
            "outcome": session.outcome if session is not None else "",
            # reply_preview is masked too: the confirm/booked replies quote
            # plate, mobile and email verbatim — an earlier draft logged
            # them raw, which the acceptance suite's PII checks caught.
            "reply_preview": redact.mask_for_trace(reply)[:120],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record
