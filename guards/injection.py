"""guards/injection.py — the code blocklist that runs before the router.

Deck, workflow A step 2: "Code blocklist: injection, 'ignore previous
instructions', discount demands, card asks." The guardrail runs **before**
the router, so an injection attempt never reaches a model and can never be
persuaded to grant a discount.

The blocked turn is routed as ``out_of_scope`` in the trace — that is the
label the test set expects for C11, and it is honest: the message is out of
this bot's scope by definition.
"""

from __future__ import annotations

import re

PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "instruction override",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}"
            r"\b(previous|prior|earlier|above|all|your)\b.{0,20}"
            r"\b(instruction|prompt|rule|direction|system)\b",
            re.I,
        ),
    ),
    (
        "persona takeover",
        re.compile(
            r"\byou\s+are\s+now\b|\bact\s+as\b|\bpretend\s+to\s+be\b|"
            r"\badmin\s+mode\b|\bdeveloper\s+mode\b|\bjailbreak\b",
            re.I,
        ),
    ),
    (
        "discount demand",
        re.compile(
            r"\b\d{1,3}\s?%\s*(off|discount)\b|"
            r"\b(give|get|apply|grant|offer)\b.{0,24}\bdiscount\b|"
            r"\bdiscount\b.{0,24}\b(apply|give|grant|must)\b|"
            r"\bwaive\b.{0,20}\b(fee|charge|price)\b",
            re.I,
        ),
    ),
    (
        "card request",
        re.compile(
            r"\b(tell|show|repeat|give|print)\b.{0,24}\b(card|payment\s+card|"
            r"cvv|pin)\b|\bcard\s+number\b",
            re.I,
        ),
    ),
    (
        "prompt extraction",
        re.compile(
            r"\b(reveal|show|print|repeat|output)\b.{0,24}"
            r"\b(system\s+prompt|instructions|prompt)\b",
            re.I,
        ),
    ),
]

REFUSAL = (
    "I can only help with this workshop's services, prices and bookings — "
    "and I can't change prices or my own rules. If you need a price "
    "adjustment, please speak to our service desk."
)


def check(text: str) -> tuple[bool, str]:
    """(blocked, reason). Blocked messages never reach the router."""
    for name, pattern in PATTERNS:
        if pattern.search(text):
            return True, name
    return False, ""
