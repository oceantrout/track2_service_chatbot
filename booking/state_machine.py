"""booking/state_machine.py — the ten states, in code, exactly as specified.

Spec: `service → vehicle_plate → date → time_slot → name → mobile → email
→ consent → confirm → done`, `idle` outside. The robustness rule is the
spec's own: **the current state is the first field still empty**, so
clearing or changing an earlier field automatically sends the flow back.

Three global rules:
  * 3 failed attempts at any state → stop the booking, outcome `handoff`;
  * commands work at any step (cancel / start over / change-a-field);
  * nothing is written until consent, and writing goes through
    persistence.save_booking's four gates — never from here directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core import config
from booking import availability, extract, persistence, validate
from guards import typo

FIELDS = ["service", "vehicle_plate", "date", "time_slot", "name", "mobile", "email"]

CONSSENT_TEXT = (
    "Kallang Auto Care will use your name, mobile number, email and plate "
    "number only to manage this booking, send reminders and keep your "
    "service record (kept 2 years after your last visit). Do you agree? "
    "(yes / no)"
)

YES_RE = re.compile(r"^\s*(yes|y|yeah|yep|yup|confirm|correct|ok|okay|go ahead|please do)\b", re.I)
NO_RE = re.compile(r"^\s*(no|n|nope|nah|don'?t|do not|not now|no thanks)\b", re.I)

CANCEL_RE = re.compile(r"\b(cancel|stop|never mind|nevermind|forget it)\b", re.I)
RESTART_RE = re.compile(r"\b(start over|restart|begin again)\b", re.I)
# "try again" / "retry" — a fresh slate for the CURRENT step, or a brand-new
# booking after the last one ended (handoff/cancel). Live UI found that
# customers reach for exactly this after a 3-strike handoff, and without a
# rule it fell to the FAQ — which has nothing about their attempt and
# abstained.
RETRY_RE = re.compile(
    r"\b(try(?: (?:that|this|it))? again|retry|one more (?:try|time|go)"
    r"|another (?:try|go|round)|give it another go)\b",
    re.I,
)
# Go-back-to-step-one (live-UI error4.png): mid-flow the customer wants out
# of the current step ENTIRELY — "no. I want to change from the begining".
# Nothing matched: the sentence has a change VERB but no field word, so
# _try_change declined and the machine charged it as a bad email value.
# A human clerk hears "start again at the beginning" and re-offers the
# service list; that is the rule below. Typo "begining" folds via
# DETECT_VOCAB — "beginning" is a literal word in this pattern, so the
# vocab derives it and can never drift from it.
GO_BACK_RE = re.compile(
    r"\bfrom\s+the\s+(?:beginning|start|top)\b"
    r"|\b(?:go\s+)?back\s+to\s+(?:the\s+)?(?:beginning|start|top|first)\b"
    r"|\bback\s+to\s+(?:the\s+)?(?:first\s+step|step\s+one|step\s+1)\b"
    r"|\bback\s+to\s+square\s+one\b"
    r"|\bfrom\s+scratch\b"
    r"|\bstart\s+again\s+from\s+the\s+beginning\b",
    re.I,
)
# Step-echo questions — the customer asks WHAT IS AVAILABLE rather than
# giving a value ("next week is fine", "which day?"). A human clerk answers
# those with the open days/times; they are not failed values and must not
# charge an attempt (live-UI: "next week is fine" burned 3 date attempts and
# the customer gave up). "what time are you open?" deliberately stays OUT
# (opening hours → FAQ): the echo needs the day/date/slot noun, or "which".
WEEK_REF_RE = re.compile(
    r"\b(next|this|coming)\s+week\b|\bnext\s+(?:couple|few)\s+days?\b"
    r"|(?:early|later)\s+next\s+week\b",
    re.I,
)
DATE_ECHO_RE = re.compile(r"\b(?:which|what|any)\b[^.?]*\b(?:day|days|date|dates)\b", re.I)
TIME_ECHO_RE = re.compile(r"\bwhich\s+time\b|\b(?:which|what|any)\b[^.?]*\b(?:slot|slots|times)\b", re.I)
# Booking speech — intent words regardless of question shape ("Can I make a
# booking?"). Mid-flow this asks WHERE the booking is, it is not a value for
# the current step; at the service step it means "which service do you want?"
BOOKING_SPEECH_RE = re.compile(r"\b(book|booking|appointment)\b", re.I)
MAKE_START_RE = re.compile(r"\b(make|start|begin|book)\b", re.I)

CHANGE_FIELD_WORDS = {
    "service": r"\b(service|job)\b",
    "vehicle_plate": r"\b(plate|number plate|reg)\b",
    "date": r"\b(date|day)\b",
    "time_slot": r"\b(time|slot|timing)\b",
    "name": r"\bname\b",
    "mobile": r"\b(mobile|phone|number|handsphone)\b",
    "email": r"\bemail\b",
}

CHANGE_VERB_RE = re.compile(r"\b(change|update|correct|actually|make it|instead)\b", re.I)

# Spell-fix vocabulary for guards/typo.py — the machine's own copy of the
# router's INTENT_VOCAB trick (router/router.py): DERIVED from the command/
# speech patterns so it can never drift from what the machine matches.
# Doctrine stays intact in both directions:
#   * the corrected text is used for REGEX DETECTION only — extract_*/
#     validate_* and the consent/confirm yes-no gates always see the
#     customer's raw text, so values are byte-for-byte and agreement is
#     never auto-corrected;
#   * COMMAND_NEIGHBOURS is the safety belt: real words one edit from a
#     command word (chance/change, agent/again, took/book). They make such
#     a typo uncorrectable instead of silently firing a command — but every
#     command word must ITSELF be in the vocab, or the belt rewrites the
#     correct word. U1 rehearsal's hard lesson: "change" was missing from
#     this list once and the "chance" neighbour silently ate the correctly
#     typed change-date command. Derive from the patterns; never hand-list
#     the verbs.
# min_len=5, same as the router: short words are where typos and real
# words collide ("date", "time", "name", "book", "stop" stay untouched —
# they are covered by their own exact tokens anyway).
COMMAND_NEIGHBOURS = {"chance", "agent", "took"}
DETECT_VOCAB = {
    w
    for rx in (CANCEL_RE, RESTART_RE, RETRY_RE, GO_BACK_RE, BOOKING_SPEECH_RE,
               MAKE_START_RE,
               CHANGE_VERB_RE, WEEK_REF_RE, DATE_ECHO_RE, TIME_ECHO_RE,
               re.compile("|".join(CHANGE_FIELD_WORDS.values()), re.I))
    for w in re.findall(r"[a-z]{3,}", rx.pattern)
} | COMMAND_NEIGHBOURS


def _detect(text: str) -> str:
    """Typo-corrected copy of `text` for command/speech regexes ONLY.

    Live UI found the gap: the router fixes "booooking" and routes to
    booking, but the machine saw the raw text, missed BOOKING_SPEECH_RE and
    charged it as a failed service name ("I couldn't match that to a
    service") instead of answering the booking request. Detection gets the
    same one-edit spellfix here; extraction never does.
    """
    return typo.correct(text, DETECT_VOCAB, min_len=5)


@dataclass
class Session:
    """One conversation's booking state. Lives in the app process only."""

    active: bool = False
    filled: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    consent_given_at: str = ""
    outcome: str = ""  # booked | cancelled | no_consent | handoff | ""
    booking_ref: str = ""

    @property
    def state(self) -> str:
        if self.outcome == "booked":
            return "done"
        if not self.active:
            return "idle"
        for f in FIELDS:
            if not self.filled.get(f):
                return f
        return "confirm" if self.consent_given_at else "consent"

    def reset(self) -> None:
        self.active = False
        self.filled = {}
        self.attempts = {}
        self.consent_given_at = ""
        self.booking_ref = ""


@dataclass
class StepResult:
    reply: str
    state_before: str
    state_after: str
    extracted_field: str = ""
    validation: str = ""  # ok | fail | command | terminal
    terminal: str = ""  # outcome when the booking ended this turn
    booking_ref: str = ""


class BookingMachine:
    def __init__(self):
        self.services = availability.load_services()

    # ---- entry points ----------------------------------------------------

    def start(self, session: Session) -> None:
        """Route=booking on an idle/done session starts the flow."""
        if not session.active:
            if session.outcome == "booked":
                session.filled = {}
                session.attempts = {}
                session.consent_given_at = ""
                session.booking_ref = ""
            session.outcome = ""
            session.active = True

    def handle(self, session: Session, text: str) -> StepResult:
        """One booking-path turn. `text` arrives already PII-redacted."""
        before = session.state
        detected = _detect(text)  # regex detection only — see _detect()

        # Commands first — they work at any step (spec table).
        if CANCEL_RE.search(detected) and not re.search(r"\bnot\b", detected, re.I):
            session.reset()
            session.outcome = "cancelled"
            return StepResult(
                "Booking cancelled — nothing was saved. Message me again if "
                "you change your mind.",
                before, session.state, validation="command", terminal="cancelled",
            )
        if RESTART_RE.search(detected):
            session.filled = {}
            session.attempts = {}
            session.consent_given_at = ""
            session.active = True
            session.outcome = ""
            return StepResult(
                self._prompt(session, text), before, session.state, validation="command"
            )
        # "go back to the beginning" (error4.png): acknowledge like a clerk,
        # wipe the booking, and re-open at step 1 with the service list.
        # Never an attempt — this IS the instruction the machine was asking
        # for. Placed before _try_change so "change … from the beginning"
        # cannot fall through to the current step's value handler.
        if GO_BACK_RE.search(detected):
            session.filled = {}
            session.attempts = {}
            session.consent_given_at = ""
            session.active = True
            session.outcome = ""
            return StepResult(
                "Sure — let's go back to the beginning and select a service. "
                + self._service_list(),
                before, session.state, validation="command",
            )
        changed = self._try_change(session, text, detected)
        if changed is not None:
            reply, field_name = changed
            return StepResult(
                reply, before, session.state, extracted_field=field_name, validation="command"
            )

        # Retry requests: wipe the current step's attempts and re-ask it — or,
        # when nothing is open (post-handoff/cancel), start a fresh booking.
        # Not charged as an attempt. Guarded by "no service named" so
        # "try again, exterior detailing" still flows as a service value.
        if RETRY_RE.search(detected) and extract.extract_service(text) is None:
            if not session.active:
                self.start(session)
                return StepResult(
                    "Sure — let's start the booking again. " + self._prompt(session, ""),
                    before, session.state, validation="ok",
                )
            session.attempts.pop(session.state, None)
            return StepResult(
                "Sure — let's try that again. " + self._prompt(session, ""),
                before, session.state, validation="ok",
            )

        # Booking speech with fields already collected ("Can I make a
        # booking?" mid-flow) is a "where are we?" — tell them the step and
        # re-ask. NOT charged as an attempt: they did the right kind of thing.
        if session.active and before not in ("service", "") \
                and BOOKING_SPEECH_RE.search(detected) and MAKE_START_RE.search(detected):
            # Live-UI clarification (error4 follow-up): booking speech that
            # names the generic SERVICE — "can I book a service" mid-flow —
            # is really "let me pick a (different) service": go back to step
            # 1 and answer with the canned priced list, no model, no attempt.
            # Without the noun ("Can I make a booking?") it stays "where are
            # we?" — that phrasing asks about the booking, not the services.
            if re.search(r"\b(?:service|services|job|jobs)\b", detected) \
                    and extract.extract_service(text) is None:
                session.filled = {}
                session.attempts = {}
                session.consent_given_at = ""
                session.active = True
                session.outcome = ""
                return StepResult(
                    "Sure — let's go back to the beginning and select a service. "
                    + self._service_list(),
                    before, session.state, validation="command",
                )
            return StepResult(
                "You're already booking with me — here's where we are: "
                + self._prompt(session, ""),
                before, session.state, validation="ok",
            )

        if not session.active:
            # A finished booking leaves the session `done`; a fresh booking
            # request starts a clean flow (the old row is already saved).
            if session.outcome == "booked":
                session.filled = {}
                session.attempts = {}
                session.consent_given_at = ""
                session.booking_ref = ""
            session.outcome = ""
            session.active = True

        state = session.state
        attempts_before = dict(session.attempts)
        handler = {
            "service": self._handle_service,
            "vehicle_plate": self._handle_plate,
            "date": self._handle_date,
            "time_slot": self._handle_time,
            "name": self._handle_name,
            "mobile": self._handle_mobile,
            "email": self._handle_email,
            "consent": self._handle_consent,
            "confirm": self._handle_confirm,
        }[state]
        reply = handler(session, text)
        after = session.state
        terminal = session.outcome if session.outcome in ("booked", "cancelled", "no_consent", "handoff") else ""
        validation = "fail" if session.attempts != attempts_before else "ok"
        return StepResult(
            reply, before, after,
            extracted_field=before if before in FIELDS else "",
            validation=validation, terminal=terminal,
            booking_ref=session.booking_ref,
        )

    def reask(self, session: Session) -> str:
        """Repeat the current question (used after a mid-booking FAQ turn)."""
        return self._prompt(session, "")

    # ---- per-state handlers ----------------------------------------------

    def _fail(self, session: Session, state: str, why: str) -> str:
        session.attempts[state] = session.attempts.get(state, 0) + 1
        if session.attempts[state] >= config.MAX_ATTEMPTS_PER_STATE:
            session.reset()
            session.outcome = "handoff"
            return (
                f"{why} I've tried three times, so it's best to leave this "
                "to a person — I'm passing you to our service desk. They can "
                "call you on 6123 4500 or you can WhatsApp us."
            )
        remaining = config.MAX_ATTEMPTS_PER_STATE - session.attempts[state]
        return f"{why} (Attempt {session.attempts[state]} of 3 — {remaining} left.)"

    def _handle_service(self, session: Session, text: str) -> str:
        code = extract.extract_service(text)
        if code is None or code not in self.services:
            # "Can I make a booking?" is booking speech, not a bad service
            # name — answer it and ask the real question. No attempt charged
            # (the UI test showed customers lead with the intent, then name
            # the service next turn). Speech check runs on the typo-
            # corrected DETECT copy ("booooking" still counts as speech);
            # the service value itself is always extracted from raw text.
            if BOOKING_SPEECH_RE.search(_detect(text)):
                return "Sure — happy to book that for you. " + self._service_list()
            return self._fail(
                session, "service",
                "I couldn't match that to a service. " + self._service_list()
            )
        # Changing service across bay types clears the chosen time (spec).
        old = session.filled.get("service")
        if old and self.services[old].bay_type != self.services[code].bay_type:
            session.filled["time_slot"] = ""
        session.filled["service"] = code
        return self._prompt(session, "")

    def _handle_plate(self, session: Session, text: str) -> str:
        ok, res = validate.valid_plate(text)
        if not ok:
            return self._fail(session, "vehicle_plate", res)
        session.filled["vehicle_plate"] = res
        return self._prompt(session, "")

    def _handle_date(self, session: Session, text: str) -> str:
        day = extract.extract_date(text)
        if day is None:
            # "next week is fine" / "which day?" — the clerk answers with the
            # open days; no attempt charged (see WEEK_REF_RE note).
            if WEEK_REF_RE.search(text) or DATE_ECHO_RE.search(text):
                return self._offer_days(text, session)
            return self._fail(
                session, "date",
                "I couldn't read a date in that. Which date? We take "
                "bookings Mon–Sat from 26 Oct to 7 Nov 2026."
            )
        ok, reason = validate.valid_date(day)
        if not ok:
            return self._fail(session, "date", reason)
        bay = self.services[session.filled["service"]].bay_type
        if not availability.has_any_free(day.isoformat(), bay):
            options = availability.alternative_dates(bay, exclude=day.isoformat())[:3]
            pretty = ", ".join(self._pretty_iso(d) for d in options)
            return self._fail(
                session, "date",
                f"We're fully booked on {self._pretty(day)} for that "
                f"service. I do have {bay} bay openings on {pretty}."
            )
        # Changing the date clears the time slot (spec dependency rule).
        if session.filled.get("time_slot"):
            session.filled["time_slot"] = ""
        session.filled["date"] = day.isoformat()
        return self._prompt(session, "")

    def _handle_time(self, session: Session, text: str) -> str:
        bay = self.services[session.filled["service"]].bay_type
        date_iso = session.filled["date"]
        slots = availability.free_slots(date_iso, bay)
        parsed = extract.extract_time(text)
        if parsed is None:
            # "which time?" / "any slots left?" — show the free times; no
            # attempt charged (step-echo rule, see WEEK_REF_RE note).
            if TIME_ECHO_RE.search(text):
                return (
                    f"On {self._pretty_iso(date_iso)} I'm free at: "
                    f"{', '.join(slots)}. Which time suits you?"
                )
            return self._fail(
                session, "time_slot",
                f"I couldn't read a time in that. Available times on "
                f"{self._pretty_iso(date_iso)}: {', '.join(slots)}."
            )
        kind, value = parsed
        if kind == "bucket":
            if value == "morning":
                pool = [t for t in slots if t < "12:00"]
            elif value == "afternoon":
                pool = [t for t in slots if t >= "12:00"]
            else:  # as early as possible / earliest
                pool = list(slots)
            if not pool:
                return self._fail(
                    session, "time_slot",
                    f"No {value} slots left on {self._pretty_iso(date_iso)}. "
                    f"Free times: {', '.join(slots)}."
                )
            value = pool[0]
        ok, reason = validate.valid_time(value, date_iso, bay)
        if not ok:
            if reason == value:  # taken/unavailable: show what IS free
                return self._fail(
                    session, "time_slot",
                    f"{value} on {self._pretty_iso(date_iso)} is already "
                    f"taken. Still free: {', '.join(slots)}."
                )
            return self._fail(session, "time_slot", reason)
        session.filled["time_slot"] = value
        return self._prompt(session, "")

    def _handle_name(self, session: Session, text: str) -> str:
        ok, res = validate.valid_name(text)
        if not ok:
            return self._fail(session, "name", res)
        session.filled["name"] = res
        return self._prompt(session, "")

    def _handle_mobile(self, session: Session, text: str) -> str:
        ok, res = validate.valid_mobile(text)
        if not ok:
            return self._fail(session, "mobile", res)
        session.filled["mobile"] = res
        return self._prompt(session, "")

    def _handle_email(self, session: Session, text: str) -> str:
        ok, res = validate.valid_email(text)
        if not ok:
            return self._fail(session, "email", res)
        session.filled["email"] = res
        return self._prompt(session, "")

    def _handle_consent(self, session: Session, text: str) -> str:
        if YES_RE.match(text):
            session.consent_given_at = config.NOW.isoformat(timespec="seconds")
            return self._prompt(session, "")
        if NO_RE.match(text):
            session.reset()
            session.outcome = "no_consent"
            return (
                "No problem — I haven't saved anything. If you change your "
                "mind, just start the booking again."
            )
        return self._fail(
            session, "consent",
            "Please answer yes or no — I can only save the booking with "
            "your agreement."
        )

    def _handle_confirm(self, session: Session, text: str) -> str:
        if YES_RE.match(text):
            try:
                ref = persistence.save_booking(session, self.services)
            except persistence.WriteRejected as exc:
                session.reset()
                session.outcome = "handoff"
                return (
                    f"I couldn't complete the booking ({exc}) — passing you "
                    "to our service desk to finish it safely."
                )
            session.booking_ref = ref
            session.outcome = "booked"
            session.active = False
            return (
                f"Booked! Reference {ref}: "
                f"{self.services[session.filled['service']].name} on "
                f"{self._pretty_iso(session.filled['date'])} at "
                f"{session.filled['time_slot']} for "
                f"{session.filled['vehicle_plate']}. A confirmation goes to "
                f"{session.filled['email']}. See you there!"
            )
        if NO_RE.match(text):
            return self._prompt(session, "")
        return self._fail(
            session, "confirm",
            "Shall I confirm, or tell me what to change (for example "
            "\"change the date to 3 Nov\")."
        )

    # ---- change command ---------------------------------------------------

    def _try_change(self, session: Session, text: str, detected: str | None = None) -> tuple[str, str] | None:
        """`detected` is the typo-corrected copy used for the change-word and
        field-word regexes; the field handlers still get the RAW text."""
        if not session.active:
            return None
        if detected is None:
            detected = _detect(text)
        if not CHANGE_VERB_RE.search(detected):
            return None
        target = None
        for field_name, pattern in CHANGE_FIELD_WORDS.items():
            if re.search(pattern, detected, re.I):
                target = field_name
                break
        if target is None:
            return None
        if target == "service" and extract.extract_service(text) is None:
            # "I want to change the service" / "to another service" with no
            # new name (the other half of error4.png): that is a restart at
            # step 1, not a bad service value — clear, acknowledge, re-list.
            session.filled = {}
            session.attempts = {}
            session.consent_given_at = ""
            session.active = True
            session.outcome = ""
            return ("Sure — let's pick the service again. " + self._service_list(),
                    "service")
        # Reuse the state handler for the field, then re-ask.
        state = session.state
        handlers = {
            "service": self._handle_service, "vehicle_plate": self._handle_plate,
            "date": self._handle_date, "time_slot": self._handle_time,
            "name": self._handle_name, "mobile": self._handle_mobile,
            "email": self._handle_email,
        }
        if target not in handlers:
            return None
        # Success is "the field's value actually moved" — never "state ==
        # target": for an already-filled field the state is the FIRST EMPTY
        # slot, so a rejected change (e.g. a fully-booked date) looked
        # identical to an accepted one and the reason was silently dropped.
        old_value = session.filled.get(target)
        reply = handlers[target](session, text)
        new_value = session.filled.get(target)
        if new_value and new_value != old_value:
            follow = self._prompt(session, "")
            return (follow, target)
        return (reply, target)

    # ---- prompts -----------------------------------------------------------

    def _offer_days(self, text: str, session: Session) -> str:
        """The clerk's answer to "next week?" / "which day?": the days that
        genuinely have a free slot in the chosen service's bay. No attempt
        charged — the customer asked a fair question."""
        from datetime import timedelta
        svc = self.services[session.filled["service"]]
        first, last = config.BOOKING_WINDOW
        if WEEK_REF_RE.search(text):
            # "next week" = the Mon–Sat of the week after TODAY (24 Oct = Sat
            # → the 26 Oct week). A human reads it the same way.
            monday = config.TODAY + timedelta(days=8 - config.TODAY.isoweekday())
            days = [monday + timedelta(days=i) for i in range(6)]
            scope = "Next week"
        else:
            days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
            scope = "In the booking window"
        open_days = [d for d in days
                     if first <= d <= last and d.weekday() < 6
                     and availability.has_any_free(d.isoformat(), svc.bay_type)]
        if not open_days:
            alts = availability.alternative_dates(svc.bay_type, exclude="")[:5]
            pretty = ", ".join(self._pretty_iso(a) for a in alts)
            return (f"{scope} is fully booked for {svc.name} — I do have "
                    f"openings on: {pretty}. Which suits you?")
        shown = open_days[:6]
        pretty = ", ".join(self._pretty_iso(d.isoformat()) for d in shown)
        more = ", …" if len(open_days) > len(shown) else ""
        return (f"Happy to find a day — {scope.lower()} I'm free for "
                f"{svc.name} on: {pretty}{more}. Which day suits you?")

    def _service_list(self) -> str:
        lines = [
            f"{s.name} — SGD {s.price_sgd} ({s.duration_min} min)"
            for s in self.services.values()
        ]
        return "We offer: " + "; ".join(lines) + ". Which service would you like?"

    def _pretty_iso(self, iso: str) -> str:
        from datetime import date as _d
        return extract.pretty_date(_d.fromisoformat(iso))

    def _pretty(self, day) -> str:
        return extract.pretty_date(day)

    def _prompt(self, session: Session, _text: str) -> str:
        state = session.state
        if state == "service":
            return "Which service would you like? " + self._service_list()
        if state == "vehicle_plate":
            return "What is your vehicle plate number? (for example SBA1234K)"
        if state == "date":
            return (
                "Which date? We take bookings Mon–Sat from 26 Oct to "
                "7 Nov 2026."
            )
        if state == "time_slot":
            bay = self.services[session.filled["service"]].bay_type
            slots = availability.free_slots(session.filled["date"], bay)
            return (
                f"Available times on {self._pretty_iso(session.filled['date'])}: "
                f"{', '.join(slots)}. Which time suits you?"
            )
        if state == "name":
            return "What name should the booking be under?"
        if state == "mobile":
            return "What is your mobile number? (8-digit Singapore number)"
        if state == "email":
            return "What email address should we send the confirmation to?"
        if state == "consent":
            return CONSSENT_TEXT
        if state == "confirm":
            s = self.services[session.filled["service"]]
            return (
                f"Please check: {s.name} (SGD {s.price_sgd}) for "
                f"{session.filled['vehicle_plate']} on "
                f"{self._pretty_iso(session.filled['date'])} at "
                f"{session.filled['time_slot']}. Name: {session.filled['name']}, "
                f"mobile: {session.filled['mobile']}, email: "
                f"{session.filled['email']}. Shall I confirm? (yes, or tell "
                "me what to change)"
            )
        return "Is there anything else I can help with?"
