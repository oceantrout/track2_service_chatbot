"""Core configuration for the Track 2 chatbot.

Every constant the deck calls "cross-cutting" lives here: the fixed
TODAY, the booking window, the model id, the τ gate, the attempt cap and
the call ceiling. Risk "Today drifts" is killed by this file: nothing in
the build may call `datetime.now()`.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

# graphrag: this file -> core/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

FAQ_KB_PATH = DATA_DIR / "faq_kb.md"
SERVICES_PATH = DATA_DIR / "services.csv"
AVAILABILITY_PATH = DATA_DIR / "availability.csv"
BOOKINGS_PATH = DATA_DIR / "bookings.csv"
TRACE_PATH = LOGS_DIR / "trace.jsonl"

# The deck's fixed clock. "next Tuesday" must always mean 27 Oct.
TODAY = date(2026, 10, 24)
NOW = datetime(2026, 10, 24, 10, 0, 0)  # used for created_at / consent_at

BOOKING_WINDOW = (date(2026, 10, 26), date(2026, 11, 7))

MODEL_ID = "claude-opus-5-5"
MAX_MODEL_CALLS_PER_TURN = 2
MAX_ATTEMPTS_PER_STATE = 3

# τ gate for the FAQ path: below this, "I don't have that information".
# BM25 units (scores observed 5.9-20.4 on judged hits; C14's abstentions are
# caught upstream by faq.search.unknown_unmapped, τ is the backstop).
FAQ_TAU = 3.0

# Cost meter (README "Then add" 7). List-price ESTIMATE constants so the
# trace page can show money: $4 per 1M input tokens, $20 per 1M output —
# the same numbers graphrag_demo/llm.py's format_cost prints. An estimate,
# not a bill; on the deterministic path the model is never called, so the
# meter reads $0.00 by construction.
USD_PER_MTOK_IN = 4.0
USD_PER_MTOK_OUT = 20.0

STATES = [
    "service",
    "vehicle_plate",
    "date",
    "time_slot",
    "name",
    "mobile",
    "email",
    "consent",
    "confirm",
    "done",
]

BAY_TYPES = ("general", "ev", "detailing")
