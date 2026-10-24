"""Offline self-checks for the GraphRAG demo.

These run without an API key and without touching the network. They cover
the parts where a silent bug would poison everything downstream: section
boundaries, unit provenance and unit identity.

    python -m graphrag_demo.cli selftest
"""

from __future__ import annotations

from . import config
from .corpus import load_corpus


class CheckFailed(Exception):
    """Raised when a self-check does not hold."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def _test_sections(corpus: dict) -> None:
    sections = corpus["sections"]
    _check(len(sections) >= 12, f"expected >= 12 sections, got {len(sections)}")
    ids = [s["section_id"] for s in sections]
    _check(len(ids) == len(set(ids)), "duplicate section ids")

    titles = {s["title"] for s in sections}
    for expected in ("Opening hours", "Servicing packages", "Payment methods"):
        _check(expected in titles, f"missing expected section: {expected}")

    for section in sections:
        _check(
            section["start_line"] < section["end_line"],
            f"section {section['section_id']} has an empty line range",
        )


def _test_units(corpus: dict) -> None:
    units = corpus["units"]
    _check(len(units) > 0, "no text units produced")

    unit_ids = [u["unit_id"] for u in units]
    _check(len(unit_ids) == len(set(unit_ids)), "duplicate unit ids")

    section_ids = {s["section_id"] for s in corpus["sections"]}
    for unit in units:
        _check(unit["section_id"] in section_ids, f"orphan unit {unit['unit_id']}")
        _check(unit["text"].strip() != "", f"empty unit {unit['unit_id']}")
        _check(
            unit["start_line"] <= unit["end_line"],
            f"unit {unit['unit_id']} has a backwards line range",
        )


def _test_provenance(corpus: dict) -> None:
    """Every unit's text must really come from the lines it points at."""
    raw = config.FAQ_KB_PATH.read_text(encoding="utf-8").splitlines()
    section_by_id = {s["section_id"]: s for s in corpus["sections"]}

    checked = 0
    for unit in corpus["units"]:
        section = section_by_id[unit["section_id"]]
        _check(
            section["start_line"] <= unit["start_line"] <= unit["end_line"] <= section["end_line"],
            f"unit {unit['unit_id']} points outside its section",
        )
        if unit["kind"] == "table_row":
            continue  # table rows are reformatted, checked separately
        source = " ".join(raw[unit["start_line"] - 1 : unit["end_line"]])
        # Compare on a normalised whitespace basis.
        normalise = lambda s: " ".join(s.split()).lower()
        head = normalise(unit["text"])[:60]
        _check(
            head in normalise(source),
            f"unit {unit['unit_id']} text not found at its source lines",
        )
        checked += 1

    _check(checked > 0, "no provenance-checkable units found")


def _test_table_rows(corpus: dict) -> None:
    """A table row unit must carry its column names."""
    rows = [u for u in corpus["units"] if u["kind"] == "table_row"]
    _check(len(rows) > 0, "no table rows extracted")

    priced = [r for r in rows if "128" in r["text"]]
    _check(bool(priced), "expected the SGD 128 servicing package in the table rows")
    for row in priced:
        _check(
            "price" in row["text"].lower(),
            f"table row lost its column header: {row['text'][:60]!r}",
        )


def _test_list_items(corpus: dict) -> None:
    items = [u for u in corpus["units"] if u["kind"] == "list_item"]
    _check(len(items) > 0, "no list items extracted")
    for item in items:
        _check(
            not item["text"].lstrip().startswith(("-", "*", "+")),
            f"list bullet marker survived into the unit text: {item['text'][:40]!r}",
        )


def run_selftest() -> int:
    corpus = load_corpus()
    tests = [
        ("sections", _test_sections),
        ("units", _test_units),
        ("provenance", _test_provenance),
        ("table rows", _test_table_rows),
        ("list items", _test_list_items),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn(corpus)
        except CheckFailed as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
        else:
            print(f"ok    {name}")
    print()
    print(
        f"{len(corpus['sections'])} sections, {len(corpus['units'])} units, "
        f"{len(tests) - failures}/{len(tests)} checks passed"
    )
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_selftest())
