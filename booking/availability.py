"""booking/availability.py — a slot is a fact, not an opinion.

Deck slide 10, the rule verbatim:

    free(date, time, bay_type) = availability[date,time,bay] == 1
                                 AND not booked(date, time, bay)

The bay type comes from the **service**, never from the customer. A saved
booking takes the slot: `booked()` reads bookings.csv live, so a booking
saved earlier in the same session is already consumed.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass

from core import config


@dataclass(frozen=True)
class Service:
    code: str
    name: str
    duration_min: int
    price_sgd: int
    bay_type: str
    notes: str


def load_services() -> dict[str, Service]:
    services: dict[str, Service] = {}
    with config.SERVICES_PATH.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            services[row["service_code"]] = Service(
                code=row["service_code"],
                name=row["name"],
                duration_min=int(row["duration_min"]),
                price_sgd=int(row["price_sgd"]),
                bay_type=row["bay_type"],
                notes=row["notes"],
            )
    return services


def _free_from_availability(date_iso: str, bay: str) -> list[str]:
    times = []
    with config.AVAILABILITY_PATH.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["date"] == date_iso and row["bay_type"] == bay and row["available"] == "1":
                times.append(row["time"])
    return sorted(times)


def booked_times(date_iso: str, bay: str) -> set[str]:
    """Times already consumed by saved bookings for that date + bay."""
    services = load_services()
    taken: set[str] = set()
    if not config.BOOKINGS_PATH.exists():
        return taken
    with config.BOOKINGS_PATH.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["appointment_date"] != date_iso:
                continue
            service = services.get(row["service_code"])
            if service and service.bay_type == bay:
                taken.add(row["appointment_time"])
    return taken


def free_slots(date_iso: str, bay: str) -> list[str]:
    """Slots offered to customers: free in the CSV and not booked."""
    return [t for t in _free_from_availability(date_iso, bay) if t not in booked_times(date_iso, bay)]


def is_free(date_iso: str, time: str, bay: str) -> bool:
    return time in free_slots(date_iso, bay)


def has_any_free(date_iso: str, bay: str) -> bool:
    return bool(free_slots(date_iso, bay))


def alternative_dates(bay: str, exclude: str) -> list[str]:
    """Dates in the window with at least one free slot, soonest first."""
    out: list[str] = []
    day = config.BOOKING_WINDOW[0]
    last = config.BOOKING_WINDOW[1]
    while day <= last:
        iso = day.isoformat()
        if iso != exclude and day.weekday() != 6 and has_any_free(iso, bay):
            out.append(iso)
        day = day.fromordinal(day.toordinal() + 1)
    return out
