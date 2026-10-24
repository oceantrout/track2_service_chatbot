"""guards/redact.py — personal data never leaves the machine.

Deck slide 9: NRIC / FIN / passport / card numbers are **removed before the
model sees the message**; plate / mobile / email are not redacted from the
message (they are booking values that code validates) but are **never sent
to a model** and are masked in every trace line.

The bot must also *tell* the customer an offered number is not needed
(C10), so redaction returns what was removed, not just clean text.
"""

from __future__ import annotations

import re

# Order matters: the more specific shape first, so a passport is never
# treated as an NRIC.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("passport", re.compile(r"\b[EFG]\d{7}[A-Z]\b")),
    ("nric", re.compile(r"\b[STFGM]\d{7}[A-Z]\b")),
    ("fin", re.compile(r"\b[TR]\d{7}[A-Z]\b")),
    ("card", re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b|\b\d{13,19}\b")),
]

# The phrases that let us cut a whole clause, not just the number:
# "…, my NRIC is S1234567D" should leave no dangling "my NRIC is". The
# number itself is removed first (exact word-bounded patterns), so this
# only has to eat the sentence remnant.
CLAUSE_RE = re.compile(
    r"[,;.!]*\s*\b(?:my|his|her|the|your)?\s*"
    r"(?:nric|fin|passport|identity\s+card|id\s+card|card\s+number|credit\s+card|debit\s+card)\s*"
    r"(?:number|no\.?|#)?\s*(?:is|are|:|=)?\s*",
    re.IGNORECASE,
)

WORD = {"nric": "NRIC", "fin": "FIN", "passport": "passport", "card": "card number"}


def redact(text: str) -> tuple[str, list[str]]:
    """Strip never-collect identifiers. Returns (clean_text, removed_kinds)."""
    removed: list[str] = []
    clean = text
    for kind, pattern in PATTERNS:
        if pattern.search(clean):
            removed.append(kind)
    if removed:
        # Remove the exact numbers first (word-bounded), then the phrase
        # that was pointing at them, then tidy the punctuation.
        for kind, pattern in PATTERNS:
            clean = pattern.sub("", clean)
        clean = CLAUSE_RE.sub("", clean)
        clean = re.sub(r"\s{2,}", " ", clean).strip().strip(",;.- ").strip()
        clean = clean or "(removed)"
    return clean, removed


# --- trace masking (deck slide 9 table, formats are fixed) ----------------

def mask_mobile(value: str) -> str:
    # Deck-fixed format: first digit, *** , last four — 91234567 → 9*** 4567.
    digits = re.sub(r"\D", "", value)[-8:]
    return f"{digits[0]}*** {digits[4:]}" if len(digits) == 8 else "[masked]"


def mask_email(value: str) -> str:
    if "@" not in value:
        return "[masked]"
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def mask_plate(value: str) -> str:
    if len(value) < 2:
        return "[masked]"
    return f"{value[0]}****{value[-1]}"


def mask_for_trace(value: str) -> str:
    """Best-effort masking of any PII-shaped run inside free text.

    Order matters: never-collect numbers (gone), then the 16-digit card,
    then email, mobile, plate. `SBA1234K` cannot match the NRIC pattern
    (it starts letter-digit-letter…, NRIC needs seven digits after S) and
    `S1234567D` cannot match the plate pattern (no letter between S and
    the digits) — the two shapes are disjoint by construction.
    """
    out = value
    for kind, pattern in PATTERNS:
        out = pattern.sub("[removed]", out)
    out = EMAIL_TEXT_RE.sub(lambda m: mask_email(m.group(0)), out)
    out = MOBILE_TEXT_RE.sub(lambda m: mask_mobile(m.group(0)), out)
    out = PLATE_TEXT_RE.sub(lambda m: mask_plate(m.group(0)), out)
    return out


EMAIL_TEXT_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# 8 digits: [89] + 3 + optional separator + 4. An earlier draft here had
# \d{2} (seven digits) and matched NO real mobile — the front-end smoke
# test caught that as a live masking gap. Keep the trailing/leading
# digit-and-letter guards: they are what stops a plate's digits
# ("SBA1234K" — preceded by 'A') from being treated as a mobile.
MOBILE_TEXT_RE = re.compile(r"(?<![\dA-Z])\+?6?5?[- ]?[89]\d{3}[- ]?\d{4}(?![\dA-Z])")
PLATE_TEXT_RE = re.compile(r"\bS[A-Z]{1,2}\d{1,4}[A-Z]\b")
