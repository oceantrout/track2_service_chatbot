"""One-shot deck edit: clone the Technical Architecture deck's FAQ-path
slide into a GraphRAG-upgrade slide (new slide 8), rewrite every text box,
and renumber footer pages. Run from track2_service_chatbot/. Backup: the
_backup_AI_Technical_Architecture_before_graphrag_24Oct.pptx in docs/.
"""
import copy

from pptx import Presentation

PATH = "docs/Kallang_Track2_AI_Technical_Architecture.pptx"


def norm(t):
    return " ".join(t.split())


def set_text(shape, new):
    """Replace a shape's text keeping the first run's formatting."""
    tf = shape.text_frame
    para0 = tf.paragraphs[0]
    if not para0.runs:
        para0.text = new
        return
    first = para0.runs[0]
    for r in list(para0.runs)[1:]:
        r._r.getparent().remove(r._r)
    first.text = new
    for p in list(tf.paragraphs)[1:]:
        p._p.getparent().remove(p._p)


def set_lines(shape, lines):
    """Multi-paragraph replace: paragraph 0 is the formatting template."""
    set_text(shape, lines[0])
    tf = shape.text_frame
    template = tf.paragraphs[0]._p
    anchor = template
    for line in lines[1:]:
        el = copy.deepcopy(template)
        anchor.addnext(el)
        anchor = el
    for p, line in zip(tf.paragraphs, lines):
        runs = p.runs
        if runs:
            for r in list(runs)[1:]:
                r._r.getparent().remove(r._r)
            runs[0].text = line
        else:
            p.text = line


WORKED = [
    "“Summarise all your policies and rules for customers?”",
    "→ the router certifies a sweep: markers yes, entity no",
    "→ 8 community claims span 7 sections of the KB",
    "→ the answer quotes warranty, invoicing, servicing lines",
    "→ ends “Source: Warranty; Payment methods; …”",
]

REPL = {
    "The FAQ path — retrieve, answer, cite":
        "The FAQ path — the GraphRAG upgrade (sidebar toggle)",
    "The model never answers from memory. It answers from one section of the knowledge base, or it says it does not know.":
        "GraphRAG answers the sweep questions retrieval cannot — code-decided modes, abstention kept, TF-IDF backstop.",
    "Chunk index": "Router",
    "Score + expand": "Retrieve",
    "the customer's message": "“summarise all your policies”",
    "faq_kb.md → 12 sections, split at the ## headings": "code markers decide the mode — no model call",
    "TF-IDF cosine, then the synonym map": "entity neighbourhood, or community claims",
    "is the best score above τ?": "per-claim τ — or sweep breadth",
    "the winning section, verbatim": "grounded claims, verbatim units",
    "one call, grounded in that context": "one call over graph evidence",
    "“Source: <section>”": "“Source: <section(s)>” — a sweep lists all",
    "Below τ → no answer. Say “I don't have that information” and offer the service desk. No citation is invented.":
        "Below τ and no sweep marker → the BM25 backstop answers; both fail → no answer, no citation (C14).",
    "▪  Synonyms — the KB says servicing, the customer says maintenance\n"
    "▪  Threshold — an unmatched question must fall through to the honest “I don't know”\n"
    "▪  Citation — the section name is appended by code, so it always matches the text that was retrieved":
        "▪  Sweep vs pinpoint — breadth clears the gate only when the marker rule certifies a sweep\n"
        "▪  Abstention kept — COE/insurance match no marker and stay under τ\n"
        "▪  Measured, not claimed — hit@1 78%, abstention 100%, global coverage 6.3 vs 3.7 sections",
    "“How much does a maintenance cost?”\n→ the synonym map rewrites maintenance to servicing\n→ the best section becomes Servicing packages\n→ the answer gives SGD 128 / SGD 268 from that section\n→ the reply ends “Source: Servicing packages”":
        "\n".join(WORKED),
    "THE GROUNDED PROMPT": "THE ABSTENTION GATE",
    "“Answer only from the section below. If the section does not contain the answer, say so. Do not use any other knowledge. Section: <retrieved text>”":
        "A specific question ships only when its best claim ≥ τ (0.35). A sweep-certified question may ship on spread instead: ≥3 matched communities over ≥4 distinct sections — never both on a weak claim.",
}

# keys are matched on the normed (whitespace-collapsed) text; values keep
# their explicit newlines as paragraph boundaries
NORMED = {norm(k): v for k, v in REPL.items()}

prs = Presentation(PATH)
src = prs.slides[6]
layout = src.slide_layout
dst = prs.slides.add_slide(layout)
for sh in list(dst.shapes):  # drop any placeholders the layout injected
    sh._element.getparent().remove(sh._element)
for sh in src.shapes:        # clone every shape (pure text/vector, no rels)
    dst.shapes._spTree.append(copy.deepcopy(sh._element))

# move the new slide from the end to right after the old FAQ-path slide
lst = prs.slides._sldIdLst
moving = list(lst)[-1]
lst.remove(moving)
lst.insert(7, moving)

done = set()
for sh in dst.shapes:
    if not sh.has_text_frame:
        continue
    key = norm(sh.text_frame.text)
    if key in NORMED:
        lines = NORMED[key].split("\n")
        if len(lines) > 1:
            set_lines(sh, lines)
        else:
            set_text(sh, lines[0])
        done.add(key)

missing = set(NORMED) - done
print("replaced:", len(done), "of", len(NORMED))
for m in missing:
    print("  MISSING:", m[:70])

# footers show ordinal-1 == 0-based index; the tail slides shifted by one
for idx in range(7, len(prs.slides._sldIdLst)):
    for sh in prs.slides[idx].shapes:
        if sh.has_text_frame and sh.text_frame.text.strip().isdigit():
            want = str(idx)
            if sh.text_frame.text.strip() != want:
                print(f"  slide {idx+1}: footer {sh.text_frame.text.strip()} -> {want}")
                set_text(sh, want)

prs.save(PATH)
print("saved", PATH, "| slides:", len(prs.slides._sldIdLst))
