"""Unit test: booking state machine, deterministic path only.

Run:  PYTHONPATH=. python _t_booking.py   (from the track2 root)

Each scenario ends by asserting the saved row (or nothing saved), the same
way run_acceptance.py will, but without the router in the loop yet.
"""

from __future__ import annotations

import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from booking import availability, extract, persistence, state_machine
from booking.state_machine import BookingMachine, Session
from core import config
from guards import redact

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name}\n  got : {got!r}\n  want: {want!r}")


def reset_bookings():
    with config.BOOKINGS_PATH.open("w", encoding="utf-8", newline="") as fh:
        fh.write("booking_ref,created_at,service_code,vehicle_plate,"
                 "appointment_date,appointment_time,name,mobile,email,"
                 "consent_given_at\n")


def read_rows():
    import csv
    with config.BOOKINGS_PATH.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


m = BookingMachine()

# ---- extract.py unit checks ------------------------------------------------

check("service basic", extract.extract_service("I want to book a basic servicing"), "SVC-BASIC")
check("service full", extract.extract_service("full servicing please"), "SVC-FULL")
check("service ev", extract.extract_service("EV health check for my hybrid"), "SVC-EV")
check("service tyre", extract.extract_service("tyre rotation"), "TYR-ROT")
check("service aircon", extract.extract_service("book an aircon servicing"), "AC-SVC")
check("service brake", extract.extract_service("brake pad replacement (front)"), "BRK-PAD")
check("service det-int", extract.extract_service("interior detailing"), "DET-INT")
check("service det-ext", extract.extract_service("exterior detailing"), "DET-EXT")
check("next Tuesday", extract.extract_date("next Tuesday"), date(2026, 10, 27))
check("29 Oct", extract.extract_date("29 Oct"), date(2026, 10, 29))
check("3 Nov", extract.extract_date("Actually change the date to 3 Nov"), date(2026, 11, 3))
check("tomorrow", extract.extract_date("tomorrow"), date(2026, 10, 25))
check("iso", extract.extract_date("2026-10-30"), date(2026, 10, 30))
check("time 10am", extract.extract_time("10am"), ("clock", "10:00"))
check("time 2pm", extract.extract_time("2pm"), ("clock", "14:00"))
check("time 14:00", extract.extract_time("14:00"), ("clock", "14:00"))
check("time afternoon", extract.extract_time("afternoon"), ("bucket", "afternoon"))
check("time early", extract.extract_time("as early as possible"), ("bucket", "as early as possible"))
check("pretty", extract.pretty_date(date(2026, 10, 27)), "Tue 27 Oct 2026")

# ---- validate.py unit checks -----------------------------------------------

from booking import validate
check("plate normalise", validate.valid_plate("SBA 1234 K"), (True, "SBA1234K"))
check("plate bad", validate.valid_plate("1234 5678")[0], False)
check("mobile bad", validate.valid_mobile("1234 5678")[0], False)
check("mobile +65", validate.valid_mobile("+65 8765 4321"), (True, "87654321"))
check("email ok", validate.valid_email("weiming.tan@example.com")[0], True)
check("name strip", validate.valid_name("my name is Tan Wei Ming"), (True, "Tan Wei Ming"))
check("name trailing comma", validate.valid_name("Rahman bin Ali,"), (True, "Rahman bin Ali"))
check("date sunday", validate.valid_date(date(2026, 11, 1))[0], False)
check("date out", validate.valid_date(date(2026, 11, 9))[0], False)
check("date in", validate.valid_date(date(2026, 10, 27))[0], True)

# ---- availability checks -----------------------------------------------------

check("28 Oct general full", availability.has_any_free("2026-10-28", "general"), False)
check("30 Oct 09:00 taken", availability.is_free("2026-10-30", "09:00", "general"), False)
check("29 Oct 13:00 free", availability.is_free("2026-10-29", "13:00", "general"), True)
check("29 Oct 12:00 blocked", availability.is_free("2026-10-29", "12:00", "general"), False)

# ---- full walk: C01-shaped sequence ------------------------------------------

reset_bookings()
s = Session()
steps = [
    ("I'd like to book a basic servicing", "vehicle_plate"),
    ("SBA 1234 K", "date"),
    ("next Tuesday", "time_slot"),
    ("10am", "name"),
    ("Tan Wei Ming", "mobile"),
    ("9123 4567", "email"),
    ("weiming.tan@example.com", "consent"),
    ("yes", "confirm"),
    ("yes", "done"),
]
state = "service"
for text, want_state in steps:
    r = m.handle(s, text)
    state = r.state_after
r1 = read_rows()
check("C01 state", state, "done")
check("C01 outcome", s.outcome, "booked")
check("C01 rows", len(r1), 1)
row = r1[0]
check("C01 ref", row["booking_ref"], "KAC-261027-001")
check("C01 fields", (row["service_code"], row["vehicle_plate"], row["appointment_date"],
                     row["appointment_time"], row["name"], row["mobile"], row["email"]),
      ("SVC-BASIC", "SBA1234K", "2026-10-27", "10:00", "Tan Wei Ming", "91234567", "weiming.tan@example.com"))

# second booking same date -> sequence 002
r2 = m.handle(s, "I want another booking")  # idle session restarts
check("ref sequence 002", persistence.next_reference("2026-10-27"), "KAC-261027-002")

# ---- C06-shaped: full day + taken slot + attempts -----------------------------

reset_bookings()
s = Session()
r = m.handle(s, "full servicing")
r = m.handle(s, "SGX11B")
r = m.handle(s, "28 Oct")
check("C06 28Oct rejected", (s.state, r.validation), ("date", "fail"))
r = m.handle(s, "30 Oct")
check("C06 30Oct accepted", s.state, "time_slot")
r = m.handle(s, "9am")
check("C06 9am taken", (s.state, r.validation), ("time_slot", "fail"))
check("C06 attempts", s.attempts.get("time_slot"), 1)
r = m.handle(s, "11am")
check("C06 11am ok", s.state, "name")
r = m.handle(s, "Lim Bo Kay")
r = m.handle(s, "98765432")
r = m.handle(s, "bk@example.com")
r = m.handle(s, "yes")
check("C06 consent->confirm", s.state, "confirm")
r = m.handle(s, "yes")
check("C06 booked", s.outcome, "booked")
check("C06 ref", read_rows()[0]["booking_ref"], "KAC-261030-001")

# ---- three failures -> handoff --------------------------------------------------

reset_bookings()
s = Session()
m.handle(s, "tyre rotation")
m.handle(s, "not a plate")
m.handle(s, "still wrong")
r = m.handle(s, "garbage again")
check("3rd fail -> handoff", (s.outcome, s.state), ("handoff", "idle"))
check("handoff mentions desk", "service desk" in r.reply, True)

# ---- change command: date clears time_slot (C07 shape) ---------------------------

reset_bookings()
s = Session()
m.handle(s, "interior detailing")
m.handle(s, "SBB99X")
m.handle(s, "27 Oct")
m.handle(s, "9am")  # the only detailing slot free on 27 Oct
m.handle(s, "Ahmad Salleh")
m.handle(s, "92223333")
m.handle(s, "ahmad@example.com")
check("C07 at consent gate", s.state, "consent")
r = m.handle(s, "Actually change the date to 3 Nov")
check("C07 change clears time", s.state, "time_slot")
check("C07 date set", s.filled["date"], "2026-11-03")
r = m.handle(s, "2pm")
check("C07 2pm ok", s.state, "confirm" if s.consent_given_at else "consent")

# ---- step echoes: "next week is fine" / "which day?" / "which time?" --------
# Live-UI feature request: the clerk answers availability questions from the
# booking step with real free slots — and they never charge an attempt.

reset_bookings()
s = Session()
m.handle(s, "aircon servicing")
m.handle(s, "SHX9021L")
check("U-echo at date step", s.state, "date")
r = m.handle(s, "next week is fine")
check("next week answered with free days", "26 Oct" in r.reply and "Which day" in r.reply, True)
check("echo charges no attempt", s.attempts.get("date", 0), 0)
r = m.handle(s, "which day next week?")
check("echo repeatable", "26 Oct" in r.reply, True)
check("still no attempt", s.attempts.get("date", 0), 0)
r = m.handle(s, "next Monday")
check("date set after echo", (s.filled["date"], s.state), ("2026-10-26", "time_slot"))
r = m.handle(s, "which time?")
check("time echo lists real slots", "09:00" in r.reply and "suits" in r.reply, True)
check("time echo charges no attempt", s.attempts.get("time_slot", 0), 0)
r = m.handle(s, "2pm")
check("time set after echo", (s.filled["time_slot"], s.state), ("14:00", "name"))

# ---- consent "no" -> nothing saved -----------------------------------------------

reset_bookings()
s = Session()
for t in ["exterior detailing", "SXX12T", "28 Oct", "10am", "Wei Li", "90000001", "wl@example.com"]:
    m.handle(s, t)
r = m.handle(s, "no thanks")
check("no_consent outcome", s.outcome, "no_consent")
check("no_consent idle", s.state, "idle")
check("nothing written", read_rows(), [])

# ---- cancel ------------------------------------------------------------------------

reset_bookings()
s = Session()
m.handle(s, "aircon servicing")
r = m.handle(s, "cancel that")
check("cancelled", (s.outcome, s.state), ("cancelled", "idle"))

# ---- C10 name with offered NRIC (redact first, then machine) -------------------------

reset_bookings()
clean, kinds = redact.redact("Rahman bin Ali, my NRIC is S1234567D")
check("nric removed", "S1234567D" in clean, False)
check("nric kind detected", "nric" in kinds, True)
s = Session()
for t in ["EV health check", "SVE1E", "3 Nov", "9am", clean, "90001111", "rahman@example.com"]:
    r = m.handle(s, t)
check("C10 state after name turn", s.state, "consent")
check("C10 name", s.filled.get("name"), "Rahman bin Ali")

# ---- afternoon bucket: earliest free from 12:00 ---------------------------------------

reset_bookings()
s = Session()
m.handle(s, "basic servicing")
m.handle(s, "SPB123L")
m.handle(s, "29 Oct")  # 12:00 not free, 13:00 free
m.handle(s, "afternoon")
check("bucket afternoon", s.filled.get("time_slot"), "13:00")

# ---- retry phrases (live UI: "try again" dead-ended in a FAQ abstain) ----------

from router import router as router_mod

check("retry routes booking when idle", router_mod.route("try again", False).intent, "booking")
check("retry question routes booking when active",
      router_mod.route("can I try again?", True).intent, "booking")
check("restart question routes booking when active",
      router_mod.route("start over?", True).intent, "booking")
check("PayNow question still faq (no retry words)",
      router_mod.route("do you accept PayNow?", True).intent, "faq")

# spell-fix at the router (guards/typo.py, live-UI feature request): a
# typoed intent word still routes — and collision words must NOT be
# re-routed (took/book is why COMMON_NEIGHBOURS exists).
check("boooking typo starts booking",
      router_mod.route("can i make a boooking", False).intent, "booking")
check("appoinment typo starts booking",
      router_mod.route("i want an appoinment", False).intent, "booking")
check("humaan typo reaches handoff",
      router_mod.route("my tyre burst, i want to speak to a humaan", False).intent,
      "handoff")
check("retry typo 'agian' still booking",
      router_mod.route("try agian", False).intent, "booking")
check("took is not book (no re-route)",
      router_mod.route("i took my car yesterday", False).intent, "faq")
check("well is not will-flip (no re-route)",
      router_mod.route("that works well thanks", False).intent, "faq")
check("step echo routes to booking",
      router_mod.route("Which day next week?", True).intent, "booking")
check("idle question not echoed to booking",
      router_mod.route("what dates do you have?", False).intent, "faq")
check("open-days question stays faq (not echo)",
      router_mod.route("what days are you open?", True).intent, "faq")

reset_bookings()
s = Session()
m.handle(s, "tyre rotation")
m.handle(s, "bad plate")
check("attempt charged before retry", s.attempts.get("vehicle_plate", 0), 1)
r = m.handle(s, "try again")
check("retry clears this step's attempts", s.attempts.get("vehicle_plate", 0), 0)
check("retry stays on the same step", s.state, "vehicle_plate")
check("retry re-asks the step", "plate" in r.reply, True)
check("retry is not an attempt", r.validation, "ok")

# post-handoff: "try again" must start a FRESH booking (orchestrator does
# machine.start() first on route=booking — mirrored here)
reset_bookings()
s = Session()
m.handle(s, "tyre rotation")
for t in ["x", "y", "z"]:
    m.handle(s, t)
check("handoff state reached", (s.outcome, s.state), ("handoff", "idle"))
m.start(s)
r = m.handle(s, "try again")
check("retry after handoff reopens booking", (s.active, s.state, s.outcome),
      (True, "service", ""))
check("retry-after-handoff offers services", "We offer" in r.reply, True)

# a named service alongside a retry isn't swallowed by the retry branch —
# at the service step it flows as a normal service value
reset_bookings()
s = Session()
m.handle(s, "try again, exterior detailing")
check("retry+service names the service", s.filled.get("service"), "DET-EXT")

# ---- go back to the beginning (error4.png) ---------------------------------
# "no. I want to change from the begining" at the email step must restart
# at step 1 WITH an acknowledgement — it was charged as a bad email value.

reset_bookings()
s = Session()
for t in ["basic servicing", "SBA1234K", "29 Oct", "1pm", "Tan Wei Ming", "9123 4567"]:
    m.handle(s, t)
check("G at email step", s.state, "email")
r = m.handle(s, "no. I want to change from the begining")
check("go-back acknowledges (deck wording)",
      r.reply.startswith("Sure — let's go back to the beginning and select a service"), True)
check("go-back re-lists services", "We offer" in r.reply, True)
check("go-back resets to step 1", (s.state, s.filled, s.attempts), ("service", {}, {}))
check("go-back is a command, not a value", r.validation, "command")
r = m.handle(s, "tyre rotation")
check("fresh pick after go-back", (s.filled.get("service"), s.state), ("TYR-ROT", "vehicle_plate"))
r = m.handle(s, "can we go back to the beginning?")
check("go-back repeatable in question shape", r.reply.startswith("Sure — let's go back"), True)
check("go-back clears mid-plate", (s.state, s.filled), ("service", {}))

# bare service change with no new name (the error4 sibling: "to change
# another service") — back to step 1, no attempt charged
s = Session()
for t in ["full servicing", "SGX11B", "29 Oct"]:
    m.handle(s, t)
r = m.handle(s, "I want to change the service")
check("bare service change -> step 1", (s.state, "pick the service again" in r.reply), ("service", True))
check("bare service change charges no attempt", s.attempts.get("service", 0), 0)
r = m.handle(s, "change the service to exterior detailing")
check("named service change still flows", s.filled.get("service"), "DET-EXT")

# router: go-back utterances belong to the booking, in any shape
check("go-back question routes booking (active)",
      router_mod.route("can we go back to the beginning?", True).intent, "booking")
check("go-back typo routes booking (active)",
      router_mod.route("go back to the begining", True).intent, "booking")
check("go-back after close restarts booking (idle)",
      router_mod.route("start from the beginning", False).intent, "booking")
check("plain mid-booking question still faq",
      router_mod.route("my brakes feel soft, is that bad?", True).intent, "faq")

# error4 follow-up (live UI): mid-flow "can I book a service" must come back
# to step 1 WITH the canned priced list — no attempt, no model path. Booking
# speech without the service noun ("Can I make a booking?") stays "where are
# we?" and re-asks the current step untouched.
s = Session()
for t in ["basic servicing", "SBA1234K", "29 Oct", "1pm", "Tan Wei Ming", "9123 4567"]:
    m.handle(s, t)
check("H at email step", s.state, "email")
r = m.handle(s, "can I book a service")
check("service-noun speech mid-flow -> step 1", s.state, "service")
check("priced list returned (canned)", "SGD 128" in r.reply and "Which service" in r.reply, True)
check("service-noun speech clears data, charges nothing", (s.filled, s.attempts), ({}, {}))
r = m.handle(s, "tyre rotation")
check("H fresh pick after list flows", (s.filled.get("service"), s.state), ("TYR-ROT", "vehicle_plate"))
r = m.handle(s, "Can I make a booking?")
check("no-noun booking speech stays put", s.state, "vehicle_plate")
check("no-noun speech re-asks current step", "already booking" in r.reply, True)
check("router: service-noun speech active is booking",
      router_mod.route("can I book a service", True).intent, "booking")

# error6.png: "can I change service" carries NO booking word and is
# question-shaped — Rule 3 used to hand it to the FAQ, which dumped the raw
# pricing section ("| Service | Price (SGD) | …") and the state never moved.
check("change-service routes booking (active)",
      router_mod.route("can I change service", True).intent, "booking")
check("change-service routes booking (idle)",
      router_mod.route("can I change service", False).intent, "booking")
check("service-DATE policy question still faq (active)",
      router_mod.route("can I change the service date later?", True).intent, "faq")

reset_bookings()
s = Session()
m.handle(s, "EV health check")
m.handle(s, "SGX11B")
check("E6 at date step", s.state, "date")
r = m.handle(s, "can I change service")
check("E6 canned priced list returned",
      ("pick the service again" in r.reply and "SGD 128" in r.reply
       and "Which service" in r.reply), True)
check("E6 state moved to service", (s.state, s.filled), ("service", {}))
check("E6 no attempts charged", s.attempts, {})
r = m.handle(s, "basic servicing")
check("E6 new service taken", (s.filled.get("service"), s.state), ("SVC-BASIC", "vehicle_plate"))

print(f"\n_t_booking: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
