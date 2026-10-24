"""G6a - Citations.

Every answer the demo gives must be traceable to a line in the knowledge
base. A citation is only emitted when the quoted text has been **verified**
against the file it claims to come from.

The verification is done in code, against the actual file on disk, not
against a model assertion. If the text cannot be found at the claimed
lines, the citation is marked unverified and the answer drops it.
"""

from __future__ import annotations

import re

from . import config


def normalise(text: str) -> str:
    """Collapse whitespace and unify the dashes and quotes the KB mixes."""
    text = text.replace("–", "-").replace("—", "-").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def verify_quote(quote: str, source_text: str) -> bool:
    """Is this quote really in this source?

    Tolerant of the ways a quote can drift from its source: collapsed
    whitespace, unified dashes and quotes, and one character clipped at
    either end (models truncate). Not tolerant of different words.
    """
    needle = normalise(quote)
    haystack = normalise(source_text)
    if not needle:
        return False
    if needle in haystack:
        return True
    if len(needle) > 40:
        # One character clipped at either end.
        if needle[1:] in haystack or needle[:-1] in haystack:
            return True
    return False


def source_lines(path, start: int, end: int) -> str:
    """The text of the file between two line numbers (1-indexed, inclusive)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    start = max(start - 1, 0)
    end = min(end, len(lines))
    return "\n".join(lines[start:end])


def build_citation(evidence: dict, kb_path=None) -> dict:
    """Verify one piece of evidence and turn it into a citation."""
    kb_path = kb_path or config.FAQ_KB_PATH
    excerpt = source_lines(kb_path, evidence.get("start_line", 0), evidence.get("end_line", 0))
    verified = verify_quote(evidence.get("text", ""), excerpt)

    citation = {
        "section_title": evidence.get("section_title", ""),
        "section_id": evidence.get("section_id", ""),
        "unit_id": evidence.get("unit_id", ""),
        "start_line": evidence.get("start_line", 0),
        "end_line": evidence.get("end_line", 0),
        "quote": evidence.get("text", ""),
        "verified": verified,
        "label": format_citation(evidence),
    }
    return citation


def format_citation(evidence: dict) -> str:
    """The human-readable citation line."""
    title = evidence.get("section_title") or "Knowledge base"
    start, end = evidence.get("start_line", 0), evidence.get("end_line", 0)
    if start and end and start != end:
        return f"Source: {title} (lines {start}-{end})"
    if start:
        return f"Source: {title} (line {start})"
    return f"Source: {title}"


def build_citations(evidence_items: list[dict], kb_path=None) -> list[dict]:
    """Verify a list of evidence items and return their citations."""
    citations = []
    for item in evidence_items:
        citation = build_citation(item, kb_path)
        if citation["verified"]:
            citations.append(citation)
    return citations
