"""G9a - The guardrail.

Two jobs, both done in code:

1. **Refuse what should never reach the router.** Prompt injection, and
   requests for things the workshop does not do. The guardrail runs
   *before* the router, so an injected instruction never reaches the
   retrieval layer, let alone the model.

2. **Mask PII.** The knowledge base is public, but a customer's message
   may contain an NRIC, a FIN, a passport number, a card number, a phone
   number or a vehicle plate. The rules in the README are not negotiable:
   ask for consent before saving anything, never store or log those
   numbers, validate them with a regex rather than sending them to a
   model, and mask them in the trace.

The patterns below are validated against the synthetic test data in
`test_set/conversations.jsonl` and in the demo's own PII test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- PII patterns ------------------------------------------------------
# Order matters: the more specific patterns run first, so a passport
# number is not mistaken for an NRIC.

PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("PASSPORT", re.compile(r"\b[EFG]\d{7}[A-Z]\b")),
    ("NRIC", re.compile(r"\b[STFGM]\d{7}[A-Z]\b")),
    ("FIN", re.compile(r"\b[TR]\d{7}[A-Z]\b")),
    ("CARD", re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("MOBILE", re.compile(r"(?:\+65[\s-]?)?\b[89]\d{3}[\s-]?\d{4}\b")),
    ("PLATE", re.compile(r"\b[A-Z]{2,3}\d{3,4}[A-Z]\b")),
]

# --- Injection and out-of-scope patterns -------------------------------

INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("instruction override", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}"
        r"\b(previous|prior|earlier|above|all|your)\b.{0,20}"
        r"\b(instruction|prompt|rule|direction)s?\b", re.I)),
    ("persona takeover", re.compile(
        r"\byou\s+are\s+now\b|\bact\s+as\b|\bpretend\s+to\s+be\b|"
        r"\badmin\s+mode\b|\bdeveloper\s+mode\b|\bjailbreak\b|\bDAN\b", re.I)),
    ("secret exfiltration", re.compile(
        r"\b(reveal|show|print|repeat|output)\b.{0,30}"
        r"\b(system\s+prompt|instructions|rules|configuration)\b", re.I)),
    ("policy bypass", re.compile(
        r"\b(discount|discounted|free|waive|waiver|free of charge)\b.{0,30}"
        r"\b(apply|give|offer|grant)\b|"
        r"\b\d{1,3}\s?%\s*(off|discount)\b|"
        r"\b(give|get|grant|offer)\b.{0,20}\b\d{1,3}\s?%\s*off\b|"
        r"\bwithout\s+(paying|payment|charge)\b|"
        r"\b(ignore|skip|bypass)\b.{0,20}\b(charge|price|fee|payment)\b", re.I)),
]

# Requests that are simply not this business.
OUT_OF_SCOPE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("food recommendation", re.compile(
        r"\b(recommend|suggest|where can i (eat|find)|best)\b.{0,30}"
        r"\b(restaurant|food|eat|meal|nasi lemak|mcdonald|kfc|starbucks|cafe)\b", re.I)),
    ("medical or legal advice", re.compile(
        r"\b(diagnos\w+|symptom|prescription|medical|symptom)\b", re.I)),
    ("immigration", re.compile(r"\b(visa|passport application|immigration|work permit)\b", re.I)),
    ("weather", re.compile(r"\b(weather|forecast|raining|rain today)\b", re.I)),
    ("sports and news", re.compile(r"\b(football|match result|score|news|election|stock price)\b", re.I)),
]

# A complaint about work done is a handoff to a human, not an FAQ.
HANDOFF_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("damage complaint", re.compile(
        r"\b(scratch|dent|damag\w+|broken|cracked|leak\w*|smell|soiled|"
        r"lost|missing)\b.{0,60}\b(bumper|car|panel|paint|wheel|seat|interior|"
        r"my car|service)\b|\bmy car\b.{0,40}\b(scratch|damag\w+|broken)\b", re.I)),
    ("wants a human", re.compile(
        r"\b(speak to|talk to|see)\b.{0,20}\b(a |the )?(manager|human|person|supervisor|staff)\b|"
        r"\bhuman\b.{0,20}\b(agent|help)\b|\breal person\b", re.I)),
    ("formal complaint", re.compile(r"\b(complaint|complain|unacceptable|disappointed|refund)\b", re.I)),
]


@dataclass
class Guardrail:
    """Decide what happens to a message before it reaches the router."""

    enable_injection: bool = True
    enable_scope: bool = True
    enable_handoff: bool = True
    findings: list[dict] = field(default_factory=list)

    def check(self, text: str) -> dict:
        """Classify a message.

        Returns a decision: ``faq`` (carry on), ``out_of_scope`` (polite
        refusal), ``handoff`` (a human), or ``blocked`` (an injection
        attempt - refused, and named as such).
        """
        self.findings = []

        if self.enable_injection:
            for name, pattern in INJECTION_PATTERNS:
                match = pattern.search(text)
                if match:
                    self.findings.append(
                        {
                            "kind": "injection",
                            "name": name,
                            "matched": match.group(0)[:80],
                        }
                    )
                    return {
                        "decision": "blocked",
                        "reason": f"prompt injection detected ({name})",
                        "findings": list(self.findings),
                    }

        if self.enable_handoff:
            for name, pattern in HANDOFF_PATTERNS:
                match = pattern.search(text)
                if match:
                    self.findings.append(
                        {
                            "kind": "handoff",
                            "name": name,
                            "matched": match.group(0)[:80],
                        }
                    )
                    return {
                        "decision": "handoff",
                        "reason": f"customer needs a human ({name})",
                        "findings": list(self.findings),
                    }

        if self.enable_scope:
            for name, pattern in OUT_OF_SCOPE_PATTERNS:
                match = pattern.search(text)
                if match:
                    self.findings.append(
                        {
                            "kind": "out_of_scope",
                            "name": name,
                            "matched": match.group(0)[:80],
                        }
                    )
                    return {
                        "decision": "out_of_scope",
                        "reason": f"not a workshop question ({name})",
                        "findings": list(self.findings),
                    }

        return {"decision": "faq", "reason": "no guardrail finding", "findings": []}

    @staticmethod
    def detect_pii(text: str) -> list[dict]:
        """Find PII in a message, without reading it to a model."""
        found = []
        for kind, pattern in PII_PATTERNS:
            for match in pattern.finditer(text):
                found.append({"kind": kind, "value": match.group(0)})
        return found


def mask_pii(text: str) -> str:
    """Replace every PII value with a type tag.

    The value never appears in the trace, the log, or the model prompt.
    """
    masked = text
    for kind, pattern in PII_PATTERNS:
        masked = pattern.sub(f"<{kind}>", masked)
    return masked
