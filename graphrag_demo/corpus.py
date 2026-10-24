"""M1 - Corpus loader.

Turns `data/faq_kb.md` into two things:

  * sections   - one per `##` heading, with the line range it occupies.
  * text units - the atomic, citable pieces of text that everything
                  downstream works with.

A "text unit" is deliberately small and atomic: a paragraph, a single list
item, or a single table row (with its header row carried along for
context). Small units mean a retrieved claim points at exactly the words
that support it, which is what makes citation in M6 honest.

Every unit keeps a line range into the source file so any answer can be
traced back to the exact lines of `faq_kb.md`.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from . import config

# A heading like "## Services and prices"
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


@dataclass
class Section:
    """One `##` section of the knowledge base."""

    section_id: str
    title: str
    heading: str
    start_line: int
    end_line: int
    unit_ids: list[str] = field(default_factory=list)


@dataclass
class TextUnit:
    """An atomic, citable piece of the knowledge base."""

    unit_id: str
    section_id: str
    section_title: str
    kind: str  # "para" | "list_item" | "table_row"
    text: str
    start_line: int
    end_line: int

    def to_dict(self) -> dict:
        return asdict(self)


def _slug(title: str) -> str:
    """`Services and prices` -> `services-and-prices`."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "section"


def _split_sentences(text: str) -> list[str]:
    """Split a paragraph on sentence-ish boundaries, keeping punctuation."""
    # Guard against decimals (SGD 20) and abbreviations by requiring the
    # period to be followed by whitespace + a capital or end of string.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text)
    return [p.strip() for p in parts if p.strip()]


def _chunk_paragraph(text: str, max_chars: int) -> list[str]:
    """Break a long paragraph into sentence groups under `max_chars`."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in _split_sentences(text):
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def _iter_blocks(lines: list[tuple[int, str]]) -> Iterable[tuple[str, int, int, object]]:
    """Yield `(kind, start_line, end_line, payload)` for each logical block.

    Blocks are the raw material: a table is one block, a bullet list is one
    block, a paragraph is one block. They get refined into units later.

    The payload is a string for `para` and `list`, and a list of
    `(line_no, raw_line)` pairs for `table` so each table row can cite its
    own line.
    """
    i = 0
    n = len(lines)
    while i < n:
        line_no, raw = lines[i]
        stripped = raw.strip()

        if not stripped:
            i += 1
            continue

        if TABLE_ROW_RE.match(raw):
            start = line_no
            table_lines: list[tuple[int, str]] = []
            while i < n and TABLE_ROW_RE.match(lines[i][1]):
                table_lines.append(lines[i])
                i += 1
            yield ("table", start, lines[i - 1][0], table_lines)
            continue

        if LIST_RE.match(raw):
            start = line_no
            items = []
            while i < n and LIST_RE.match(lines[i][1]):
                items.append(lines[i][1])
                i += 1
            yield ("list", start, lines[i - 1][0], "\n".join(items))
            continue

        # Paragraph: consecutive non-blank, non-table, non-list lines.
        start = line_no
        para_lines = []
        while i < n:
            _, para_raw = lines[i]
            if (
                not para_raw.strip()
                or TABLE_ROW_RE.match(para_raw)
                or LIST_RE.match(para_raw)
            ):
                break
            para_lines.append(para_raw.strip())
            i += 1
        yield ("para", start, lines[i - 1][0], " ".join(para_lines))


def _table_rows(block_lines: list[tuple[int, str]]) -> list[tuple[str, str, int]]:
    """Split a markdown table into `(row_text, header, line_no)` triples.

    The header row is repeated into every row so a unit like
    "Basic servicing package | 128 | 1 hour" still says what the columns
    mean when it is retrieved on its own.
    """
    rows = [
        (line_no, line.strip())
        for line_no, line in block_lines
        if line.strip() and not TABLE_SEP_RE.match(line)
    ]
    if not rows:
        return []

    header = rows[0][1]
    body = rows[1:]
    if not body:
        return [("", header, rows[0][0])]

    header_cells = [c.strip() for c in header.strip("|").split("|")]
    out: list[tuple[str, str, int]] = []
    for line_no, row in body:
        cells = [c.strip() for c in row.strip("|").split("|")]
        pairs = [
            f"{h}: {c}"
            for h, c in zip(header_cells, cells)
            if h and c and h.lower() != "c"
        ]
        readable = "; ".join(pairs) if pairs else row
        out.append((readable, header, line_no))
    return out


def parse_knowledge_base(path: Path | None = None) -> dict:
    """Parse the FAQ knowledge base into sections and text units.

    Returns a dict with `sections` and `units`, both JSON-serialisable.
    """
    path = path or config.FAQ_KB_PATH
    raw_lines = path.read_text(encoding="utf-8").splitlines()

    # --- 1. carve the file into sections -----------------------------
    headings: list[tuple[int, str]] = []
    for idx, line in enumerate(raw_lines, start=1):
        match = HEADING_RE.match(line)
        if match:
            headings.append((idx, match.group(1)))

    if not headings:
        raise ValueError(f"No '## ' section headings found in {path}")

    sections: list[Section] = []
    for order, (line_no, title) in enumerate(headings):
        start = line_no
        end = headings[order + 1][0] - 1 if order + 1 < len(headings) else len(raw_lines)
        sections.append(
            Section(
                section_id=_slug(title),
                title=title,
                heading=f"## {title}",
                start_line=start,
                end_line=end,
            )
        )

    # --- 2. turn each section into text units ------------------------
    units: list[TextUnit] = []
    for section in sections:
        body = [
            (n, raw_lines[n - 1])
            for n in range(section.start_line + 1, section.end_line + 1)
        ]
        unit_no = 0
        for kind, block_start, block_end, payload in _iter_blocks(body):
            if kind == "table":
                for row_text, header, row_line in _table_rows(payload):
                    if not row_text:
                        continue
                    unit_no += 1
                    text = f"{header}\n{row_text}"
                    units.append(
                        TextUnit(
                            unit_id=f"{section.section_id}::u{unit_no:03d}",
                            section_id=section.section_id,
                            section_title=section.title,
                            kind="table_row",
                            text=text,
                            start_line=row_line,
                            end_line=row_line,
                        )
                    )
            elif kind == "list":
                for offset, item_line in enumerate(payload.splitlines()):
                    match = LIST_RE.match(item_line)
                    if not match:
                        continue
                    body_text = match.group(1).strip()
                    if not body_text:
                        continue
                    unit_no += 1
                    units.append(
                        TextUnit(
                            unit_id=f"{section.section_id}::u{unit_no:03d}",
                            section_id=section.section_id,
                            section_title=section.title,
                            kind="list_item",
                            text=body_text,
                            start_line=block_start + offset,
                            end_line=block_start + offset,
                        )
                    )
            else:  # paragraph
                for chunk in _chunk_paragraph(payload, config.MAX_UNIT_CHARS):
                    unit_no += 1
                    units.append(
                        TextUnit(
                            unit_id=f"{section.section_id}::u{unit_no:03d}",
                            section_id=section.section_id,
                            section_title=section.title,
                            kind="para",
                            text=chunk,
                            start_line=block_start,
                            end_line=block_end,
                        )
                    )

        section.unit_ids = [u.unit_id for u in units if u.section_id == section.section_id]

    return {
        "source_path": str(path),
        "sections": [asdict(s) for s in sections],
        "units": [u.to_dict() for u in units],
    }


def write_corpus(path: Path | None = None) -> dict:
    """Parse the KB and write the M1 artifact."""
    corpus = parse_knowledge_base(path)
    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    config.CORPUS_PATH.write_text(
        json.dumps(corpus, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return corpus


def load_corpus() -> dict:
    """Load the M1 artifact, parsing the KB first if it does not exist."""
    if not config.CORPUS_PATH.exists():
        return write_corpus()
    return json.loads(config.CORPUS_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    result = write_corpus()
    print(f"sections: {len(result['sections'])}")
    print(f"units:    {len(result['units'])}")
