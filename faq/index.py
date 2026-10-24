"""faq/index.py — chunk the KB by `##` section; one chunk = one citable unit.

Twelve sections, so the index is tiny and can be rebuilt in memory on every
process start; no persisted artefact needed at hackathon scale. Each chunk
keeps its exact line range so a citation can point at real lines in the
file (the same trick graphrag_demo/citation.py verified).
"""

from __future__ import annotations

from dataclasses import dataclass

from core import config


@dataclass(frozen=True)
class Section:
    title: str
    text: str          # body without the heading line
    start_line: int    # 1-based, inclusive
    end_line: int      # 1-based, inclusive


def load_sections(path=config.FAQ_KB_PATH) -> list[Section]:
    lines = path.read_text(encoding="utf-8").splitlines()
    sections: list[Section] = []
    current_title = ""
    start = 0
    body: list[str] = []
    preamble_done = False

    def flush(end_line: int) -> None:
        if current_title:
            sections.append(
                Section(current_title, "\n".join(body).strip(), start + 1, end_line)
            )

    for i, line in enumerate(lines):
        if line.startswith("## "):
            flush(i)
            current_title = line[3:].strip()
            start = i
            body = []
            preamble_done = True
        elif line.startswith("# "):
            continue
        elif preamble_done:
            body.append(line)
    flush(len(lines))
    return sections
