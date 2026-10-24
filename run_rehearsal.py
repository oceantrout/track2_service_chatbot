"""run_rehearsal.py — the DoD's "three unseen conversations rehearsed".

Not part of the judged set: fresh wordings through the same orchestrator,
covering the three things the README says the judges improvise — changing
an answer, giving bad values repeatedly, and offering data the bot must
not keep (card number). Prints the transcript, asserts the behaviour,
restores bookings.csv to its header.

Run:  PYTHONPATH=. python run_rehearsal.py
"""

from __future__ import annotations

import csv
import json
import sys
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

FAILS: list[str] = []


def check(name: str, cond: bool):
    if not cond:
        FAILS.append(name)
        print(f"  FAIL {name}")


def rows() -> list[dict]:
    with config.BOOKINGS_PATH.open(encoding="utf-8", newline="") as h:
        return list(csv.DictReader(h))


def reset_bookings() -> None:
    config.BOOKINGS_PATH.write_text(BOOKINGS_HEADER, encoding="utf-8")


def play(orch: Orchestrator, session: Session, cid: str, turns: list[str]):
    print(f"\n=== {cid} ===")
    replies = []
    for t in turns:
        r = orch.handle_turn(session, t)
        replies.append(r)
        print(f"  C: {t}\n  B: {r.reply[:170]}\n     [{r.route} → {r.state_after}]")
    return replies


def main() -> int:
    config.BOOKINGS_PATH.write_text(BOOKINGS_HEADER, encoding="utf-8")
    machine = BookingMachine()
    faq = FaqEngine(backend="tfidf")

    # U1 — unseen wording, change-the-date command mid-flow
    trace1 = config.LOGS_DIR / "rehearsal_trace_U1.jsonl"
    for p in (trace1,):
        if p.exists():
            p.unlink()
    s1 = Session()
    o1 = Orchestrator(machine=machine, faq_engine=faq, recorder=TraceRecorder(path=trace1))
    r = play(o1, s1, "U1 · change an answer", [
        "Can I make an appointment for an aircon service?",
        "SHX9021L",
        "next Monday",
        "actually change the date to next Wednesday",   # fully booked 28 Oct → must SAY why
        "change the date to 30 October",
        "2pm",
        "Siti Aisyah binti Omar",
        "+65 8712 3456",
        "siti.aisyah@example.com",
        "yes I agree",
        "yes",
    ])
    check("U1 booked", s1.outcome == "booked")
    check("U1 change rejected names the reason", "fully booked" in r[3].reply.lower())
    check("U1 change to booked date did not move state", r[3].state_after == "time_slot")
    check("U1 final date 30 Oct", s1.booking_ref == "KAC-261030-001")
    check("U1 mobile normalised", rows() and rows()[0]["mobile"] == "87123456")

    # U2 — bad values three times → handoff, nothing written
    reset_bookings()
    trace2 = config.LOGS_DIR / "rehearsal_trace_U2.jsonl"
    if trace2.exists():
        trace2.unlink()
    s2 = Session()
    o2 = Orchestrator(machine=machine, faq_engine=faq, recorder=TraceRecorder(path=trace2))
    play(o2, s2, "U2 · three bad plates", [
        "I'd like to bring my car in for detailing",
        "ABC",
        "9999",
        "banana",
    ])
    check("U2 handoff", s2.outcome == "handoff")
    check("U2 nothing written", len(rows()) == 0)

    # U3 — offers a card number; it must be removed and never stored/logged
    reset_bookings()
    trace3 = config.LOGS_DIR / "rehearsal_trace_U3.jsonl"
    if trace3.exists():
        trace3.unlink()
    s3 = Session()
    o3 = Orchestrator(machine=machine, faq_engine=faq, recorder=TraceRecorder(path=trace3))
    r3 = play(o3, s3, "U3 · volunteered card number", [
        "I want to book a car polish",
        "SDD7788P",
        "3 Nov 2026",
        "14:00",
        "Rahman bin Ali",
        "my card number is 4111 1111 1111 1111, mobile 90001111",
        "rahman.a@example.com",
        "yes",
        "yes",
    ])
    check("U3 booked despite card", s3.outcome == "booked")
    check("U3 card never stored", rows() and "4111" not in json.dumps(rows()[-1]))
    all_logs = trace3.read_text(encoding="utf-8") if trace3.exists() else ""
    check("U3 card never logged", "4111 1111" not in all_logs and "41111111" not in all_logs)
    check("U3 mobile kept", rows() and rows()[-1]["mobile"] == "90001111")
    check("U3 told it isn't needed", any("removed" in x.reply.lower() and "privacy" in x.reply.lower() for x in r3))

    config.BOOKINGS_PATH.write_text(BOOKINGS_HEADER, encoding="utf-8")
    print("\n" + ("REHEARSAL: 3/3 clean" if not FAILS else f"REHEARSAL FAILURES: {FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
