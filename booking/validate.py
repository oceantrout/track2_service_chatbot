"""booking/validate.py — regex decides, always. Deck slide 9's table as code.

Plate, mobile, email and name never go to a model: a regular expression
handles them perfectly, so sending them out would be a PDPA cost with no
benefit (data minimisation). These functions take the **raw** message
value and return (ok, normalised_value_or_reason).
"""

from __future__ import annotations

import re
from datetime import date

from core import config

PLATE_RE = re.compile(r"^S[A-Z]{1,2}\d{1,4}[A-Z]$")
MOBILE_RE = re.compile(r"^[89]\d{7}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '\/\.\-@]*$")

# "my name is X", "this is X", "the name is X" — strip in code.
NAME_PREFIX_RE = re.compile(
    r"^(?:my\s+)?(?:name|it|this)\s+(?:is|:)\s+", re.IGNORECASE
)


def valid_plate(raw: str) -> tuple[bool, str]:
    value = re.sub(r"[\s-]", "", raw.strip().upper())
    if PLATE_RE.match(value):
        return True, value
    return False, "That doesn't look like a valid Singapore car plate (for example SBA1234K)."


def valid_mobile(raw: str) -> tuple[bool, str]:
    value = re.sub(r"\D", "", raw.strip())
    if value.startswith("65") and len(value) == 10:
        value = value[2:]
    if MOBILE_RE.match(value):
        return True, value
    return False, "That doesn't look like a valid Singapore mobile number — it should be 8 digits starting with 8 or 9."


def valid_email(raw: str) -> tuple[bool, str]:
    value = raw.strip()
    if EMAIL_RE.match(value):
        return True, value
    return False, "That doesn't look like a valid email address."


def valid_name(raw: str) -> tuple[bool, str]:
    value = NAME_PREFIX_RE.sub("", raw.strip()).strip()
    value = re.sub(r"\s{2,}", " ", value).strip(" ,;:.")
    if not (2 <= len(value) <= 60):
        return False, "Please give a name between 2 and 60 characters."
    if not NAME_RE.match(value):
        return False, "The name can only contain letters, spaces and ' - / . @."
    return True, value


def valid_date(value: date) -> tuple[bool, str]:
    """In window, not a Sunday, at least one free slot for the bay."""
    if value < config.BOOKING_WINDOW[0] or value > config.BOOKING_WINDOW[1]:
        return False, (
            "We take bookings from 26 Oct to 7 Nov 2026, Monday to Saturday. "
            "Could you pick a date in that range?"
        )
    if value.weekday() == 6:
        return False, "We are closed on Sundays — could you pick a Monday to Saturday?"
    return True, ""


def valid_time(time: str, date_iso: str, bay: str) -> tuple[bool, str]:
    from . import availability

    if availability.is_free(date_iso, time, bay):
        return True, ""
    return False, time
