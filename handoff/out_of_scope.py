"""handoff/out_of_scope.py — the polite wall, canned, zero model calls.

C12 (nasi lemak) and C11 turn 1 (a blocked injection lands here too —
the guardrail runs before the router, and the trace records the route as
out_of_scope, which is honest: the message is outside this bot's scope).
"""

from __future__ import annotations

OUT_OF_SCOPE_TEXT = (
    "I can only help with this workshop's services, prices and bookings. "
    "Ask me about servicing packages, what to bring, payment, or book a "
    "slot — and if you need anything else, our service desk at 6123 4500 "
    "can help."
)


def reply() -> str:
    return OUT_OF_SCOPE_TEXT
