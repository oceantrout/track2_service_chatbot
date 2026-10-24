# GraphRAG demo — module-by-module walkthrough

A standalone GraphRAG implementation built against the Kallang Auto Care
knowledge base (`data/faq_kb.md`). It sits **alongside** the mandated
TF-IDF chatbot in `README.md` — and since 24 Oct also *inside* it, as the
sidebar **graphrag** FAQ backend wired through `faq/search.py` (default
stays TF-IDF; the judged architecture is otherwise untouched). Its job is
to answer two questions:

1. Does a graph beat TF-IDF on this corpus? (The C03 synonym trap.)
2. Can it answer the *global* questions that retrieval cannot? (Themes
   across all 12 sections.)

## Why this corpus

`faq_kb.md` has 12 sections, each a coherent theme:

```
opening-hours  location-and-parking  services-and-prices  servicing-packages
ev-and-hybrid-servicing  warranty  what-to-bring  payment-methods
cancellation-and-no-show-fees  courtesy-car  rescheduling  data-and-privacy-notice
```

That is exactly the shape community detection is good at: the graph will
fall out into roughly one community per theme, and the community
summaries become a pre-computed index of *the whole corpus* — the thing
naive vector RAG cannot build.

And the synonym trap from test **C03** is a real trap here: the words
"maintenance" and "polish" appear **nowhere** in the KB.

```
$ grep -ic "maintenance" data/faq_kb.md   -> 0
$ grep -ic "polish"      data/faq_kb.md     -> 0
```

TF-IDF has no lexical signal at all. A graph with *alias edges*
("maintenance" -> "Basic servicing package") does.

## Layout

```
graphrag_demo/
  config.py      paths, model settings, thresholds
  corpus.py      M1  markdown -> sections -> atomic text units
  extract.py     M2  LLM entity / alias / claim extraction
  graph.py       M3  graph construction + entity resolution
  community.py   M4  community detection + summaries
  query.py       M5  local / global / hybrid retrieval
  answer.py      M6  grounded answer generation with citations
  evaluate.py    M6  GraphRAG vs TF-IDF benchmark
  selftest.py    offline checks
  artifacts/     one JSON artifact per module
```

Each module writes one artifact and is independently re-runnable, so you
can test them one at a time.

## Status

| Module | Needs API key | State |
| --- | --- | --- |
| M1 corpus | no | **done** |
| M2 extraction | no (rules twin; model engine would) | **done** — rules verified; model engine written but endpoint blocked (see `doctor`) |
| M3 graph | no | **done** |
| M4 communities | no (extractive summaries; `--llm` for model summaries) | **done** |
| M5 query | no | **done** — local / global / hybrid; global gated by sweep certification (GRAPHRAG_WORKPLAN §G5) |
| M6 answer + eval | no (deterministic composer; model opt-in) | **done** — G8 receipt `artifacts/m6_eval.json`, re-run 24 Oct |

Also built since this doc was first written: `router.py` (G7, rules-only
mode choice), `core/` (G9, Workflow A orchestrator + masked trace),
`app.py` (G10, Streamlit front-end on :8600), `cli.py`, `citation.py`,
`extract_rules.py`. Module contracts and the change log live in
[GRAPHRAG_WORKPLAN.md](GRAPHRAG_WORKPLAN.md).

---

## M1 — Corpus

Parses `faq_kb.md` into **sections** and **text units**.

A text unit is deliberately atomic: a paragraph, a single list item, or a
single table row. Small units mean a retrieved claim points at exactly the
words that support it, which is what makes citation honest in M6. Every
unit keeps a line range into `faq_kb.md`, so any answer can be traced back
to exact source lines.

Table rows keep their column names inline, so a unit retrieved on its own
still says what the columns mean:

```
[services-and-prices::u002] (table_row, lines 33-33)
  | Service | Price (SGD) | Typical time |
Service: Basic servicing package; Price (SGD): 128; Typical time: 1 hour
```

### Test it

```bash
cd track2_service_chatbot
python -m graphrag_demo.cli ingest --verbose
python -m graphrag_demo.cli sections
python -m graphrag_demo.cli units --section payment-methods
python -m graphrag_demo.cli units --kind table_row
python -m graphrag_demo.cli selftest
```

Current output: **12 sections, 87 text units**, 5/5 self-checks green.

The self-checks are deliberately paranoid, because a silent bug here would
poison everything downstream:

- section ids unique, expected sections present
- unit ids unique, no orphan units, no empty units
- **every unit's text is actually present at the lines it claims** — this
  is the provenance check, and it is the one that catches a bad line-offset
  bug before it reaches a citation
- table rows keep their header; list bullets are stripped

### Artifact

`graphrag_demo/artifacts/m1_corpus.json`
