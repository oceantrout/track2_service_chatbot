"""router/router.py — four intents, five disambiguation rules, model optional.

Workplan §4 (deck workflow A step 3 + architecture slide 4). The rules
run first because they are the product: a model path (core/gateway.py)
only ever *suggests* a Route through route_schema.validate_model_json, and
low confidence falls back to `faq` — never to silence, never to a write.

The five rules, in order:
  1. Complaints always win → handoff (even mid-booking, state preserved).
  2. Off-topic content markers → out_of_scope (nasi lemak stays out of
     scope even when it mentions "the workshop" — C12).
  3. A question beats a value: mid-booking, "do you accept PayNow?" goes
     to the FAQ path and the booking state does not move (C04).
  4. A bare value while a booking is active belongs to the booking.
  5. Anything else → faq when idle; booking only on intent markers.
"""

from __future__ import annotations

import re

from booking.extract import extract_service
from guards import typo
from router.route_schema import Route

HANDOFF_RE = re.compile(
    r"\b(complaint|compensation|manager|supervisor|boss|duty manager|"
    r"speak to (?:a |the )?(?:human|person|someone|staff|representative)|"
    r"talk to (?:a |the )?(?:human|person|manager)|"
    r"lawyer|police|negligence|demand refund|useless service)\b",
    re.I,
)

OUT_OF_SCOPE_RE = re.compile(
    r"\b(nasi lemak|food|restaurant|hawker|cook|recipe|weather|rain|forecast|"
    r"movie|netflix|spotify|song|crypto|bitcoin|forex|stock market|betting|"
    r"lottery|toto|visa|immigration|passport renew|homework|exam|essay|"
    r"love life|relationship|horoscope)\b",
    re.I,
)

BOOKING_INTENT_RE = re.compile(
    r"\b(book|booking|appointment|schedule|reschedule my visit|make a slot)\b",
    re.I,
)

# Same words as booking/state_machine.BOOKING_SPEECH_RE (kept local so the
# router imports nothing from the booking package): booking speech that
# beats the question shape mid-booking.
BOOKING_SPEECH_RE = re.compile(r"\b(book|booking|appointment)\b", re.I)

# Retry/restart phrases — mirrors booking/state_machine.RETRY_RE plus the
# restart words. "try again" is about THE BOOKING; the KB has no article on
# the customer's failed attempts, so the FAQ could only abstain (live-UI
# found exactly that after a 3-strike handoff).
RETRY_RE = re.compile(
    r"\b(try(?: (?:that|this|it))? again|retry|start over|start again"
    r"|begin again|restart|one more (?:try|time|go)|another (?:try|go|round)"
    r"|give it another go)\b",
    re.I,
)

# Rule 2d (live-UI): a question ABOUT THE OPEN BOOKING'S NEXT STEP —
# "which day?", "what dates?", "which time?", "next week is fine?" — is not
# KB content: it is the clerk's own question coming back. The booking machine
# answers it with its actual free slots (state_machine step-echo handling).
# The (?!...open|hours...) tails keep genuine opening-hours questions
# ("what days are you open?") on the FAQ path.
STEP_ECHO_RE = re.compile(
    r"\b(?:which|what)\b[^.?!]*\b(?:day|days|date|dates|slot|slots)\b(?![^.?!]*\b(?:open|hours)\b)"
    r"|\bwhich\s+time\b(?![^.?!]*\b(?:open|hours)\b)"
    r"|\bnext\s+week\b",
    re.I,
)

# Go-back-to-step-one (live-UI error4.png): "no. I want to change from the
# begining", "can we go back to the beginning?". About THE BOOKING — never
# KB content, and its question shape must not send it to the FAQ. Same
# words as booking/state_machine.GO_BACK_RE (kept local so the router
# imports nothing from the booking package).
GO_BACK_RE = re.compile(
    r"\bfrom\s+the\s+(?:beginning|start|top)\b"
    r"|\b(?:go\s+)?back\s+to\s+(?:the\s+)?(?:beginning|start|top|first)\b"
    r"|\bback\s+to\s+(?:the\s+)?(?:first\s+step|step\s+one|step\s+1)\b"
    r"|\bback\s+to\s+square\s+one\b"
    r"|\bfrom\s+scratch\b"
    r"|\bstart\s+again\s+from\s+the\s+beginning\b",
    re.I,
)

# Change-service request (error6.png): "can I change service" carries NO
# booking-intent word and is question-shaped — Rule 3 used to hand it to the
# FAQ, which dumped the whole pricing section (the KB's markdown table
# header rendered as "random text with a service header") while the booking
# sat untouched. This is a booking command: pick another service. The
# (?!date|time|...) tail keeps genuine policy questions on the FAQ path —
# "can I change the service date later?" IS knowledge-base content.
CHANGE_SERVICE_RE = re.compile(
    r"\b(?:change|switch|swap)\b[^.?!]*\bservice\b(?!s?\s*(?:date|time|slot|centre|center|advisor))"
    r"|\b(?:another|different|other|new)\s+services?\b"
    r"|\bback\s+to\s+the\s+services?\b",
    re.I,
)

QUESTION_RE = re.compile(
    r"^[^\n?]*\?|"
    r"^\s*(what|when|where|which|how|why|who|do|does|did|is|are|can|could|"
    r"will|would|may|any|there?s|by the way)\b",
    re.I,
)

# Spell-fix vocabulary for guards/typo.py, DERIVED from the rule patterns
# above so it can never drift from what the router actually matches. The
# text handed to route() has already been redacted by the orchestrator, so
# no PII reaches here in raw form.
#
# COMMON_NEIGHBOURS is the safety belt: real words sitting one edit from a
# keyword. They make an ambiguous typo uncorrectable instead of silently
# re-routing it — took/book, agent/again, polite/police, woman/human,
# sweater/weather. min_len=5: four-letter words are where typos and
# domain terms collide; short inputs stay untouched.
COMMON_NEIGHBOURS = {"took", "look", "lock", "dock", "loss", "toss", "agent",
                     "polite", "woman", "sweater"}
INTENT_VOCAB = {
    w
    for rx in (HANDOFF_RE, OUT_OF_SCOPE_RE, BOOKING_INTENT_RE,
               BOOKING_SPEECH_RE, RETRY_RE, STEP_ECHO_RE, GO_BACK_RE,
               QUESTION_RE)
    for w in re.findall(r"[a-z]{3,}", rx.pattern)
} | COMMON_NEIGHBOURS


def route(text: str, booking_active: bool, model=None) -> Route:
    """Deterministic rules; `model` (a core/gateway.Gateway) is opt-in.

    The model never sees PII beyond what the caller passed — the
    orchestrator hands it the *redacted* text only.

    Typos are fixed BEFORE the rules and before any model call, against
    INTENT_VOCAB only ("can i make a boooking" still starts the flow);
    the booking machine still receives the customer's ORIGINAL text, so
    names, plates and emails arrive byte-for-byte.
    """
    stripped = typo.correct(text.strip(), INTENT_VOCAB, min_len=5)

    # Rule 1 — complaints always win.
    if HANDOFF_RE.search(stripped):
        return Route("handoff", 1.0, "complaint/request for a person")
    # Rule 2 — off-topic content, whatever else it mentions.
    if OUT_OF_SCOPE_RE.search(stripped):
        return Route("out_of_scope", 1.0, "topic outside this workshop")

    if booking_active:
        # Rule 2b (UI-found): booking speech beats the question SHAPE.
        # "Can I make a booking?" mid-flow is about the booking — sending it
        # to the FAQ abstained. KB questions without intent words ("do you
        # accept PayNow?") still go to the FAQ (C04).
        if BOOKING_SPEECH_RE.search(stripped):
            return Route("booking", 0.95, "booking speech while a booking is open")
        # Rule 2c (UI-found): retry/restart requests are booking speech too —
        # even in question shape ("can I try again?").
        if RETRY_RE.search(stripped):
            return Route("booking", 0.95, "retry/restart while a booking is open")
        # Rule 2d (live-UI): a question about the booking's next step belongs
        # to the booking — it asks what is FREE, which only the machine knows.
        if STEP_ECHO_RE.search(stripped):
            return Route("booking", 0.92, "question about the open booking step")
        # Rule 2e (error4.png): "go back to the beginning" — even in question
        # shape ("can we go back to the beginning?") — is a booking command,
        # never KB content. The machine resets to step 1 and re-lists.
        if GO_BACK_RE.search(stripped):
            return Route("booking", 0.93, "go back to the start of the booking")
        # Rule 2f (error6.png): "can I change service" — a change-service
        # request with no booking word in it. It is about THE BOOKING (pick
        # another service), not KB pricing content; the machine resets to the
        # service step and re-lists. Gated on booking_active so KB questions
        # ("can I change the service date later?") keep their FAQ answer via
        # the (?!date|time|...) tail above.
        if CHANGE_SERVICE_RE.search(stripped):
            return Route("booking", 0.9, "change the booked service / pick another")
        # Rule 3 — a question beats a value; the booking waits.
        if QUESTION_RE.search(stripped):
            return Route("faq", 0.9, "question mid-booking (question beats value)")
        # Rule 4 — otherwise it answers the current state.
        return Route("booking", 0.95, "value for the open booking")

    # Rule 5 — idle: intent markers, else FAQ (the safe destination).
    if BOOKING_INTENT_RE.search(stripped):
        return Route("booking", 0.9, "booking intent words")

    # Rule 5a (UI-found): a retry after the booking CLOSED (handoff/cancel/
    # done) starts a fresh one — the machine's own retry branch handles it.
    if RETRY_RE.search(stripped):
        return Route("booking", 0.9, "retry / start-again request")

    # Rule 5a2 (error4.png): same for "go back to the beginning" with no
    # booking open — the machine's GO_BACK branch resets and lists.
    if GO_BACK_RE.search(stripped):
        return Route("booking", 0.88, "start again from the beginning")

    # Rule 5a3 (error6.png): "can I change service" with nothing open starts
    # the booking at step 1 (listing services) rather than dumping the
    # pricing KB section — the customer is asking to book, not asking policy.
    if CHANGE_SERVICE_RE.search(stripped):
        return Route("booking", 0.86, "change / pick a service request")

    # Rule 5b — a plain statement naming one of OUR services starts the
    # flow without the word "book": "I'd like to bring my car in for
    # detailing". Questions about a service ("how much is detailing?")
    # stay on the FAQ path — question shape is checked first. The rehearsal
    # (U2) caught that intent markers alone miss every other phrasing.
    if not QUESTION_RE.search(stripped) and extract_service(stripped):
        return Route("booking", 0.85, "service named while idle (not a question)")

    if model is not None:
        suggested = model.route_json(stripped)
        if suggested is not None and suggested.confidence >= 0.5:
            return suggested
        if suggested is not None:
            # Low confidence: deck rule — fall back to faq, never invent.
            return Route("faq", 1.0 - suggested.confidence, "low-confidence fallback to faq")

    if QUESTION_RE.search(stripped):
        return Route("faq", 0.8, "question")
    return Route("faq", 0.5, "default to faq when uncertain")
