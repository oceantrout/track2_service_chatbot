"""booking/persistence.py — the only write in the system, behind four gates.

Deck, workflow A step 7 (Verify) + architecture slide's invariant: "the
model may extract; only code decides, validates and writes." Before a row
is appended, ALL FOUR gates must pass, in this order:

    1. consent_given_at is set (nothing is saved without explicit consent);
    2. the slot is re-checked live (it may have been taken during the chat);
    3. every field is re-validated with the regexes (not trusted from the
       session — defence against any bug upstream);
    4. append exactly one row, then nothing else happens.

Reference format is the spec's ``KAC-YYMMDD-NNN`` where NNN is the count of
existing bookings for that appointment date + 1. The repo CSV keeps only a
header; the seeded row in the test set is what the acceptor expects.
"""

from __future__ import annotations

import csv

from core import config


def next_reference(appointment_date: str) -> str:
    """KAC-261027-001 for the first booking on 2026-10-27."""
    yyymmdd = appointment_date[2:].replace("-", "")
    count = 0
    if config.BOOKINGS_PATH.exists():
        with config.BOOKINGS_PATH.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row["appointment_date"] == appointment_date:
                    count += 1
    return f"KAC-{yyymmdd}-{count + 1:03d}"


FIELD_ORDER = [
    "booking_ref",
    "created_at",
    "service_code",
    "vehicle_plate",
    "appointment_date",
    "appointment_time",
    "name",
    "mobile",
    "email",
    "consent_given_at",
]


class WriteRejected(Exception):
    """Raised by a gate; the caller reports handoff, never a partial row."""


def save_booking(session, services) -> str:
    """Run the four gates; return the booking reference.

    ``session`` is the booking.Session; ``services`` the availability map.
    Raises WriteRejected with a reason if any gate fails.
    """
    from booking import availability, validate

    # Gate 1 — consent.
    if not session.consent_given_at:
        raise WriteRejected("consent not given")

    service = services.get(session.filled.get("service"))
    if service is None:
        raise WriteRejected("unknown service code")

    # Gate 2 — slot re-checked live against availability AND saved rows.
    date_iso = session.filled["date"]
    time = session.filled["time_slot"]
    if not availability.is_free(date_iso, time, service.bay_type):
        raise WriteRejected(f"slot {date_iso} {time} is no longer free")

    # Gate 3 — re-validate every field with the regexes, from the stored
    # values themselves (already normalised, so this re-derives identity).
    ok, res = validate.valid_plate(session.filled["vehicle_plate"])
    if not ok:
        raise WriteRejected(f"plate failed revalidation: {res}")
    ok, res = validate.valid_mobile(session.filled["mobile"])
    if not ok:
        raise WriteRejected(f"mobile failed revalidation: {res}")
    ok, res = validate.valid_email(session.filled["email"])
    if not ok:
        raise WriteRejected(f"email failed revalidation: {res}")
    ok, res = validate.valid_name(session.filled["name"])
    if not ok:
        raise WriteRejected(f"name failed revalidation: {res}")

    # Gate 4 — append exactly one row.
    ref = next_reference(date_iso)
    row = {
        "booking_ref": ref,
        "created_at": config.NOW.isoformat(timespec="seconds"),
        "service_code": service.code,
        "vehicle_plate": session.filled["vehicle_plate"],
        "appointment_date": date_iso,
        "appointment_time": time,
        "name": session.filled["name"],
        "mobile": session.filled["mobile"],
        "email": session.filled["email"],
        "consent_given_at": session.consent_given_at,
    }
    header_present = config.BOOKINGS_PATH.exists() and (
        config.BOOKINGS_PATH.open(encoding="utf-8").readline().strip() != ""
    )
    with config.BOOKINGS_PATH.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELD_ORDER)
        if not header_present:
            writer.writeheader()
        writer.writerow(row)
    return ref
