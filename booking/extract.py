"""booking/extract.py — free text to candidate value, three states only.

Deck slide 8: the model may map "basic servicing" → SVC-BASIC and resolve
"next Tuesday" → 2026-10-27 — and **code checks every candidate**. In this
build the default extractors are deterministic (deviation §11 of the
workplan); the model path is available through `core/gateway.py` when a
flag asks for it, and even then its output is only ever a candidate.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from core import config

# Longest keyword wins: "basic servicing" before "basic".
SERVICE_KEYWORDS = [
    ("SVC-BASIC", ["basic servicing", "basic service", "basic servicing package", "basic"]),
    ("SVC-FULL", ["full servicing", "full service", "full servicing package", "full"]),
    ("SVC-EV", ["ev and hybrid health check", "ev health check", "ev health", "ev check", "ev", "hybrid", "electric"]),
    ("TYR-ROT", ["tyre rotation and balancing", "tire rotation and balancing", "tyre rotation", "tire rotation", "wheel rotation", "rotation", "tyre balance", "tire balance", "balancing", "tyre", "tire"]),
    ("AC-SVC", ["aircon servicing", "aircon service", "air conditioning service", "aircon", "air con", "air conditioning", "ac servicing"]),
    ("BRK-PAD", ["brake pad replacement", "brake pads", "brake pad", "brake"]),
    ("DET-INT", ["interior detailing", "interior detail", "interior cleaning", "interior", "cabin"]),
    ("DET-EXT", ["exterior detailing", "exterior detail", "car polish", "polish", "ceramic coating", "paint correction", "exterior", "detailing"]),
]

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "wednes": 2, "wed": 2, "thursday": 3, "thurs": 3,
    "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def extract_service(text: str) -> str | None:
    """Candidate service_code from free text; None if nothing matches.

    Every keyword targets a code that exists in services.csv by
    construction; the state machine re-checks with the CSV anyway.
    """
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for code, keywords in SERVICE_KEYWORDS:
        for keyword in keywords:
            if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                if best is None or len(keyword) > best[0]:
                    best = (len(keyword), code)
    return best[1] if best else None


def extract_date(text: str, today: date = config.TODAY) -> date | None:
    """Resolve the deck's date language against the fixed TODAY."""
    lowered = text.lower().strip()

    iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", lowered)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    if re.search(r"\btoday\b", lowered):
        return today
    if re.search(r"\btomorrow\b", lowered):
        return today + timedelta(days=1)

    # "29 Oct", "2 Nov", "3 Nov 2026", "Oct 29"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b", lowered)
    if m:
        day, month = int(m.group(1)), MONTHS[m.group(2)]
        year = today.year
        y = re.search(r"\b(20\d{2})\b", lowered)
        if y:
            year = int(y.group(1))
        return _roll_forward(date(year, month, day), today)

    m = re.search(r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\s+(\d{1,2})\b", lowered)
    if m:
        day, month = int(m.group(2)), MONTHS[m.group(1)]
        return _roll_forward(date(today.year, month, day), today)

    # "next Tuesday", "next tues", "this Friday"
    m = re.search(r"\b(?:next|this|coming)\s+(monday|mon|tuesday|tues|tue|wednesday|wednes|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|sunday|sun)\b", lowered)
    if m:
        target = WEEKDAYS[m.group(1)]
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        # "next Tuesday" from Saturday 24 Oct -> 27 Oct (the deck's rule).
        return today + timedelta(days=delta)

    # bare weekday: "Tuesday?" as an answer to "which date"
    m = re.search(r"\b(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thu|friday|fri|saturday|sat)\b", lowered)
    if m and _looks_like_bare_weekday(lowered):
        target = WEEKDAYS[m.group(1)]
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        return today + timedelta(days=delta)

    return None


def _looks_like_bare_weekday(lowered: str) -> bool:
    """Accept 'Tuesday', 'Tue.' or 'on Saturday?' — not a sentence about tyres."""
    stripped = re.sub(r"[^a-z ]", "", lowered).strip()
    words = stripped.split()
    return len(words) <= 3 and any(w in WEEKDAYS for w in words)


def _roll_forward(day: date, today: date) -> date:
    """A named date already past this year means the customer means next."""
    if day < today - timedelta(days=7):
        try:
            return day.replace(year=day.year + 1)
        except ValueError:
            return day
    return day


TIME_BUCKETS = ("morning", "afternoon", "as early as possible", "earliest")


def extract_time(text: str) -> tuple[str, str] | None:
    """(kind, value): ('clock', '13:00') or ('bucket', 'afternoon')."""
    lowered = text.lower().strip()

    bucket = re.search(r"\b(morning|afternoon|as early as possible|earliest|anytime)\b", lowered)
    if bucket:
        word = bucket.group(1)
        if word == "anytime":
            word = "as early as possible"
        return ("bucket", word)

    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lowered)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3) == "pm":
            hour += 12
        minute = int(m.group(2) or 0)
        return ("clock", f"{hour:02d}:{minute:02d}")

    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", lowered)
    if m:
        return ("clock", f"{int(m.group(1)):02d}:{m.group(2)}")

    return None


_MONTH_NAMES = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def pretty_date(day: date) -> str:
    """'Tue 27 Oct 2026' — portable, never strftime('%-d') (no Windows)."""
    return f"{_DAY_NAMES[day.weekday()]} {day.day} {_MONTH_NAMES[day.month - 1]} {day.year}"
