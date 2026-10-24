"""run_acceptance.py — replay all 14 judged conversations, field for field.

The definition of done, executable:

  * every turn's expected_route and expected_state (from the trace),
  * expected_outcome per conversation,
  * expected_booking compared field-for-field against bookings.csv,
  * negative guarantees: cancelled/no_consent wrote nothing, and
    must_not_appear_in_bookings_or_logs strings occur nowhere — neither
    the data file nor this conversation's trace lines,
  * per-conversation metrics (turns, model calls, tokens, latency), which
    is the deck's "model for language, code for rules" receipt: the rules
    path should report 0 calls.

bookings.csv is reset to its header before every conversation and restored
afterwards, so the suite is repeatable and leaves the repo clean.

Run:  PYTHONPATH=. python run_acceptance.py
"""

from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from booking.state_machine import BookingMachine, Session
from core import config
from core.orchestrator import Orchestrator
from faq.answer import FaqEngine
from tracking.trace import TraceRecorder

BOOKINGS_HEADER = ("booking_ref,created_at,service_code,vehicle_plate,"
                   "appointment_date,appointment_time,name,mobile,email,"
                   "consent_given_at\n")


def reset_bookings() -> None:
    config.BOOKINGS_PATH.write_text(BOOKINGS_HEADER, encoding="utf-8")


def read_booking_rows() -> list[dict]:
    with config.BOOKINGS_PATH.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def observed_outcome(session: Session, results) -> str:
    if session.outcome:
        return session.outcome
    # A blocked injection anywhere in the conversation makes the judged
    # outcome "refused" (C11), even when the customer then asks a real
    # question and gets an answer.
    if any(r.trace_record.get("blocked_reason") for r in results):
        return "refused"
    last = results[-1] if results else None
    if last is None:
        return ""
    if last.route == "out_of_scope":
        return "out_of_scope"
    if last.route == "handoff":
        return "handoff"
    if last.route == "faq":
        return "answered"
    return ""


def main() -> int:
    rows = [json.loads(l) for l in
            Path("test_set/conversations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    machine = BookingMachine()          # shared (read-only service table)
    faq_engine = FaqEngine(backend="tfidf")

    total_failures = 0
    metrics_rows = []
    report = {"pass": 0, "fail": 0, "conversations": []}
    t_suite = time.perf_counter()

    for conv in rows:
        reset_bookings()
        session = Session()
        trace_path = config.LOGS_DIR / f"acceptance_trace_{conv['id']}.jsonl"
        if trace_path.exists():
            trace_path.unlink()
        recorder = TraceRecorder(path=trace_path)
        orch = Orchestrator(machine=machine, faq_engine=faq_engine, recorder=recorder)

        failures: list[str] = []
        results = []
        t0 = time.perf_counter()
        for i, turn in enumerate(conv["turns"], start=1):
            res = orch.handle_turn(session, turn["user"])
            results.append(res)
            if res.route != turn["expected_route"]:
                failures.append(
                    f"turn {i} route: {res.route!r} != {turn['expected_route']!r} "
                    f"({turn['user'][:40]!r} -> {res.reply[:60]!r})")
            if res.state_after != turn["expected_state"]:
                failures.append(
                    f"turn {i} state: {res.state_after!r} != {turn['expected_state']!r} "
                    f"({turn['user'][:40]!r})")
            if res.verify_flags:
                failures.append(f"turn {i} verify: {res.verify_flags}")
            if "expected_section" in turn:
                want = turn["expected_section"]
                if res.faq_section != want:
                    failures.append(f"turn {i} section: {res.faq_section!r} != {want!r}")
        latency = (time.perf_counter() - t0) * 1000

        outcome = observed_outcome(session, results)
        if outcome != conv.get("expected_outcome", ""):
            failures.append(f"outcome: {outcome!r} != {conv['expected_outcome']!r}")

        db_rows = read_booking_rows()
        expected_booking = conv.get("expected_booking")
        if expected_booking:
            if len(db_rows) != 1:
                failures.append(f"expected exactly 1 row, got {len(db_rows)}")
            else:
                for field, want in expected_booking.items():
                    got = db_rows[0].get(field)
                    if got is not None and got != want:
                        failures.append(f"booking {field}: {got!r} != {want!r}")
        else:
            if db_rows and outcome in ("cancelled", "no_consent", "refused",
                                       "out_of_scope", "handoff", "answered"):
                failures.append(f"nothing should have been written; {len(db_rows)} rows present")

        forbidden = conv.get("must_not_appear_in_bookings_or_logs", [])
        if expected_booking:
            # The deck's masking formats are also a negative guarantee:
            # raw mobile/email/plate values must not appear in the trace.
            trace_text = trace_path.read_text(encoding="utf-8") if trace_path.exists() else ""
            for field in ("mobile", "email", "vehicle_plate"):
                value = expected_booking.get(field)
                # NOTE: these go to `failures`, not `forbidden` — `forbidden`
                # is a list of needles to search for; appending a message
                # there (as an earlier draft did) silently checks nothing.
                if value and value in trace_text:
                    failures.append(f"raw {field} {value!r} found in trace")
            digits = "".join(c for c in expected_booking.get("mobile", "") if c.isdigit())[-8:]
            if len(digits) == 8:
                # customers type mobiles spaced ("9123 4567"); that raw form
                # must not survive masking either — both forms are checked
                for form in (digits, f"{digits[:4]} {digits[4:]}"):
                    if form in trace_text:
                        failures.append(f"raw mobile form {form!r} found in trace")
                masked_mobile = f"{digits[0]}*** {digits[4:]}"
                if masked_mobile not in trace_text:
                    failures.append(f"masked mobile {masked_mobile!r} not found in trace (spec format)")
        if forbidden:
            haystack = config.BOOKINGS_PATH.read_text(encoding="utf-8")
            if trace_path.exists():
                haystack += trace_path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in haystack:
                    failures.append(f"forbidden string {needle!r} appears in bookings or logs")

        model_calls = sum(r.model_calls for r in results)
        total_failures += len(failures)
        status = "PASS" if not failures else "FAIL"
        report["conversations"].append({
            "id": conv["id"], "status": status, "turns": len(conv["turns"]),
            "outcome": outcome, "failures": failures,
            "model_calls": model_calls, "latency_ms": round(latency, 1),
        })
        metrics_rows.append((conv["id"], status, len(conv["turns"]), outcome,
                             model_calls, round(latency / len(conv["turns"]), 1), failures))

    reset_bookings()  # leave the repo clean for the judges

    width = 76
    print("=" * width)
    print(f"{'conv':4s} {'st':4s} {'turns':5s} {'outcome':13s} {'calls':5s} {'ms/turn':8s}")
    print("-" * width)
    for cid, st, n, outcome, calls, ms, failures in metrics_rows:
        print(f"{cid:4s} {st:4s} {n:<5d} {outcome:13s} {calls:<5d} {ms:<8.1f}")
        for f in failures:
            print(f"       ! {f}")
    print("-" * width)
    passed = sum(1 for r in metrics_rows if r[1] == "PASS")
    print(f"{passed}/{len(metrics_rows)} conversations PASS, "
          f"{total_failures} assertion failures, "
          f"{time.perf_counter() - t_suite:.1f}s suite wall-clock")
    report["pass"], report["fail"] = passed, len(metrics_rows) - passed
    config.LOGS_DIR.mkdir(exist_ok=True)
    (config.PROJECT_ROOT / "artifacts").mkdir(exist_ok=True)
    Path("artifacts/acceptance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if total_failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
