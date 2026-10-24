# GraphRAG demo — workplan & module tracker

**Status:** modules G0–G10 built, automated checks green · **Owner:** Claude Code · **Started:** 21 Oct 2026 · **Integrated:** shipped as the chatbot's `graphrag` FAQ backend (sidebar toggle, 24 Oct) · **Next:** nothing pending

This file is the tracker for the GraphRAG demo. It is the single place that
says what each module is, who owns it, whether it works, and how to test
it. Update it as modules land.

---

## 1. What this is, relative to the PPT

The deck in `docs/Kallang_Track2_Workplan_Architecture_Workflow.pptx` is the
**judged** chatbot: TF-IDF retrieval, a ten-state booking machine, a
four-branch router. That architecture is not being changed.

This workplan is a **parallel experiment** that keeps the deck's design
doctrine but replaces the FAQ retrieval layer with GraphRAG, to answer two
questions the deck cannot:

1. **Does a graph beat TF-IDF on the synonym trap?** Test C03 asks "how
   much does a maintenance cost" and "do you do car polish". Neither word
   appears anywhere in `faq_kb.md`.
2. **Can it answer global questions?** "What are all your fees?", "summarise
   your policies" — the shape of question retrieval is worst at.

If the experiment wins, `faq/search.py` gains one interface and swaps
implementation. If it loses, the deck stands and this is the evidence
that we checked.

**Outcome (24 Oct): it won, and it is in.** `faq/search.py:GraphRagSearch`
is the chatbot's sidebar *graphrag* backend: same interface, the TF-IDF/
BM25 layer kept as an abstention backstop, and the graph's retrieval mode
comes from this demo's own rules-based router (§G7).

### Inherited doctrine (from the deck, slide 3)

| Doctrine | How this demo obeys it |
| --- | --- |
| Model for language, code for rules | The model extracts and summarises. Code decides what is true. |
| Every capability has exactly one owner | The module table in §3 names an owner per module. |
| The model may extract; code may only decide and write | Aliases, entity merges, thresholds and abstention are all code. |
| Guardrail before any model call | Applies to the answer call too, not just the router. |
| Trace every turn, PII masked | One trace record per turn, with tokens and latency. |
| Cost meter: tokens, cost, seconds | Every model call is metered and shown in the front-end. |
| τ threshold, below which we abstain | Test C14 depends on it. Same rule, same place — with one code-decided exception (§G5: a sweep-certified question may pass on community breadth, never on a weak claim). |

---

## 2. Workflow

The demo reuses the deck's **Workflow A** (slide 5) — six steps, same
order, so a trace record from the demo looks like a trace record from the
chatbot.

```
user question
     |
 1  INGEST      strip NRIC / card / passport numbers      code
     |
 2  GUARDRAIL   block injection, discount demands, card asks  code
     |
 3  ROUTE       decide: local / global / hybrid / refuse     code
     |            (the sweep markers live in query.py, shared
     |             with step 4's abstention — one definition)
 4  RETRIEVE    local: entity neighbourhood -> claims
                global: matched communities -> their claims
                        (ranked only for sweep-certified
                        questions; selection always runs)
                hybrid: local ranking + global selection
                                                          code
     |           abstain gate: best claim ≥ τ, OR — for a
     |           sweep — the matched communities genuinely
     |           spread (≥3 communities over ≥4 sections)
 5  COMPOSE     grounded answer + "Source: <section(s)>"     model
     |
 6  TRACE       route, evidence, citations, tokens, seconds    code
```

---

## 3. Module map

Each module is one file, one owner, one artifact, one CLI command. Status
is updated as work lands.

| ID | Module | File | Layer (deck slide 4) | Owner | In → Out | Status |
| --- | --- | --- | --- | --- | --- | --- |
| **G0** | Config + endpoint doctor | `config.py`, `doctor.py` | cross-cutting | code | paths, model id, τ → environment report | **done** |
| **G1** | Corpus | `corpus.py` | data | code | `faq_kb.md` → 12 sections + 87 text units | **done** |
| **G2** | Extraction | `extract.py` (model), `extract_rules.py` (rules) | intelligence | model / code | text units → entities, aliases, claims, relationships | **done** (rules verified, model path blocked) |
| **G3** | Graph + entity resolution | `graph.py` | deterministic core | code | entities → merged node/edge graph | **done** |
| **G4** | Communities | `community.py` | intelligence + core | code + model | graph → communities + summaries | **done** |
| **G5** | Retrieval | `query.py` | deterministic core | code | question → ranked evidence | **done** |
| **G6** | Grounded answer + citation | `answer.py`, `citation.py` | intelligence | model | question + evidence → answer + citations | **done** |
| **G7** | Route selection | `router.py` | intelligence | code + model | question → local / global / hybrid | **done** |
| **G8** | Evaluation | `evaluate.py` | cross-cutting | code | GraphRAG vs TF-IDF, on the test set | **done** |
| **G9** | Orchestrator, trace, metrics | `core/` | orchestration | code | one question → reply + trace record | **done** |
| **G10** | Front-end | `app.py` | presentation | code | Streamlit, one tab per module | **done** |

### Module contracts

**G1 — corpus.** Splits `faq_kb.md` at its 12 `##` headings, then into
atomic units: a paragraph, one list item, or one table row. Units keep a
line range into the source file, so a citation can be checked against
exact lines. Table rows carry their column names inline.

**G2 — extraction.** One model call per section. Produces entities
(canonical name, type, **aliases**, summary), claims (subject, predicate,
object, verbatim quote, source unit) and relationships. The alias list is
the point: it must contain the words customers use that *do not* appear in
the text. Claims are grounded in code — the quote must be found in the
cited unit or the claim is flagged `grounded: false` and cannot produce a
citation.

`extract_rules.py` is a deterministic twin producing the identical
artifact format: no API calls, no cost, reproducible. It is the baseline
the model engine is measured against.

**G3 — graph.** Nodes are entities, edges are relationships. Entity
resolution merges names and aliases with a union-find, so the same service
mentioned in four sections becomes one node. Alias edges are what let
"maintenance" reach "Basic servicing package".

**G4 — communities.** Leiden/Louvain partition of the graph, then one
summary per community. The summaries are the artefact that answers global
questions: a pre-computed index of the whole corpus.

**G5 — retrieval.** Three modes.

| Mode | How | Serves |
| --- | --- | --- |
| local | match the question against entity names *and aliases*, take the neighbourhood's grounded claims | "how much is a basic servicing?" |
| global | score every community summary (entity spelling hits + token overlap), take the grounded sample claims of the matched communities | "summarise your fees" |
| hybrid | local ranking; community selection still runs (G8 scores coverage on it) but its claims join the ranking only when the question is sweep-certified | both |

Two rules decide *whether a retrieval may ship*, both in code (rewritten
24 Oct — see the log; global mode previously produced no evidence at all):

1. **Per-claim gate.** Best ranked claim ≥ τ (`ABSTAIN_THRESHOLD` 0.35).
   This is what C14 leans on: "What's the COE price this month?" sits at
   0.333 inside the price community — under τ, and with no sweep marker
   in the question it abstains.
2. **Sweep exception.** A question *certified* as a whole-corpus sweep —
   routed `global`, or matching the sweep markers — may instead pass on
   **breadth**: ≥3 matched communities spanning ≥4 distinct sections.
   "Summarise all your policies" matches many communities weakly (~0.2
   each); no single claim clears τ, and demanding one would make the
   graph's headline capability answerable only by accident. Breadth is a
   different confidence signal, so it is decided by a different rule —
   and it only ever applies when the marker rule says so, which is why
   rule 1 keeps its teeth on C14.

Community claims outrank local evidence only inside the sweep exception.
Ungated they hijack specific questions: the What-to-bring community's
summary ("when you *drop off your car*…") scores 0.75 on "where can I
park when I drop off my car?" and beat the legitimate parking entity at
0.592 — a breadth measure masquerading as a pinpoint hit.

**G6 — answer.** The model sees only the retrieved evidence, never the
whole KB, so it cannot invent a price. Below τ the demo says it does not
know and cites nothing (test C14). Every answer ends with its source
section — or, for a sweep answer, with every section the sweep touched.
The chatbot side (§G5 wiring) merges up to five swept sections into one
citable Section, so each quoted line is still verbatim KB and
`citation.grounded` holds per line.

**G7 — route.** Decides local / global / hybrid from explicit marker
rules + entity hits, with the reasons returned for the front-end. No
model call: routing must be predictable and free. The marker regexes
live in `query.py`, not here, because the abstention gate reads the
same definitions — router and gate can never disagree about what a
sweep is (24 Oct).

**G8 — evaluation.** Runs the FAQ questions lifted from
`test_set/conversations.jsonl` (C02, C03, C11, C14) plus a set of
global questions, through both GraphRAG and the deck's TF-IDF baseline,
and reports section-hit accuracy, abstention correctness and cost.

**G9 — orchestrator.** `handle_turn(question) → reply + trace record`,
following Workflow A. Trace records mask PII.

**G10 — front-end.** Streamlit. One tab per module, so each can be
exercised and inspected on its own, plus a **Playground** tab that runs
the full six-step workflow end to end.

### G10 — the front-end, as part of the master plan

The front-end is not a wrapper added at the end: it is the presentation
layer of the deck's five-layer architecture (slide 4), and the test
surface for every module. It is in the plan because *every module is
judged by what you can see of it in the front-end*.

| Sidebar page | Module | What you can test there |
| --- | --- | --- |
| Overview | — | artifact readiness, graph stats, the two judged traps |
| G1 Ingest | corpus | sections, unit kinds, line-range provenance per unit |
| G2 Extract | extraction | entities + aliases + claims per section, engine used |
| G3 Graph | graph | **alias lookup** (type `maintenance` or `polish`, see which node it resolves to), nodes table, edge kinds |
| G4 Communities | communities | each community's members, claims, summary; deeper levels toggle |
| G5 Retrieve | retrieval | any question, forced mode; entity score vs lexical score shown separately; abstain flag vs τ |
| G6 Answer | answer + citation | grounded answer, each quote re-verified against `faq_kb.md`; optional model checkbox (spends tokens) |
| G9 Workflow A | orchestrator | full turn from the nine judged presets (C02–C14 + PII), six steps timed, masked trace record shown |
| G8 Evaluate | evaluation | hit@1 / abstention / global coverage vs TF-IDF, per-question rows |

Launch:

```bash
python -m graphrag_demo.cli app              # opens on http://localhost:8600
python -m graphrag_demo.cli app --headless   # server only
# or directly:
streamlit run graphrag_demo/app.py
```

Rebuild artifacts from the CLI (the tabs read them; each tab says what is
missing). Smoke test for the front-end itself:

```bash
PYTHONPATH=. python graphrag_demo/_t_app.py   # 12 checks, all pages + buttons
```

Everything runs offline by default — the rules engine, extractive
summaries and deterministic composer need no key and cost nothing. The
model is only used when a tab checkbox or CLI flag explicitly asks for
it, and the token cost is printed when spent.

---

## 4. The two tests this demo is judged on

| Test | What it needs | Which module provides it |
| --- | --- | --- |
| **C03** synonym trap — "maintenance", "polish" | a path from a customer word to the right entity | G2 aliases → G3 alias edges → G5 alias matching |
| **C14** abstention — "Do you handle insurance claims?" | a calibrated threshold, and the will to say "I don't know" | G5 scoring + G6 τ gate |

The deck's own answer to C03 is a hand-written synonym expansion map
(slide 7). That map is implemented in the baseline for comparison. The
GraphRAG claim is that an **extracted alias list generalises** to words
nobody wrote down — which is a testable difference, not a slogan.

### Result (G8, re-run 24 Oct after the global-mode fix; retrieval only, no model call)

| Metric | TF-IDF baseline | GraphRAG |
| --- | --- | --- |
| Section hit@1 (9 judged questions) | 44% | **78%** |
| Section hit@3 | 67% | 78% |
| Abstention correct (C14 + false positives) | 78% | **100%** |
| Sections covered on global questions | 3.7 | **6.3** |
| Latency per question | 2.4 ms | 8.4 ms |

Read the coverage line with its history: the 21 Oct table said **7.7**,
and that number was inflated by the very bug the fix exposed — global
mode dropped `sample_claims`, produced zero evidence, and the coverage
metric had been counting the *selected* communities (a list that never
reached any answer). The 6.3 above counts sections the ranked
**evidence actually reaches**. Lower number, stronger claim: it is now
backed by citable lines instead of an unreachable selection list, and it
is still 1.7× the baseline.

Both C03 questions: the baseline's **top hit is the wrong section**
(*Warranty* for "maintenance", *What to bring* for "car polish");
GraphRAG hits both hit@1 via the alias gazetteer. Both C14 questions:
baseline retrieves a confident-looking wrong chunk every time (it has no
abstain signal); GraphRAG abstains with no citations. Latency is
milliseconds either way because the graph and the community summaries
are built at index time, not query time. The honest caveat: the corpus
is 12 sections of synthetic text, so this is a mechanism demonstration,
not a benchmark.

---

## 5. Environment notes

* The Anthropic SDK here points at a **MiniMax proxy**
  (`ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic`,
  `ANTHROPIC_MODEL=MiniMax-M3.1-Flash`). The proxy accepts
  `output_config.format` and **silently ignores it**, so `llm.py` probes
  the endpoint once and falls back to schema-in-prompt + local validation
  + one repair call.
* `python -m graphrag_demo.cli doctor` reports what the endpoint really
  supports, which packages are present, and which artifacts exist.
* `networkx` / `graspologic` are not installed; G3 and G4 use pure-python
  implementations so nothing needs installing to run the pipeline.
* All data in this track is synthetic (README, "Data provenance").

---

## 6. Log

| Date | Module | What happened |
| --- | --- | --- |
| 21 Oct | G0 | `config.py`, `doctor.py`, `schema.py`, `llm.py`. Endpoint probe found the MiniMax proxy and the ignored `output_config.format`. |
| 21 Oct | G1 | `corpus.py`. 12 sections, 87 units. `selftest.py` verifies provenance — every unit's text really is at the lines it claims. 5/5 green. |
| 21 Oct | G2 | `extract.py` (model) + `extract_rules.py` (rules). Rules engine verified: 40 entities, 137 claims, 38 relationships, alias mapping confirmed (`maintenance` → Basic servicing package, `polish` → Exterior detailing). The model engine is written but has not been run — the sandbox classifier blocks sending the KB to the external endpoint. |
| 21 Oct | G3 | `graph.py`. Union-find entity resolution: 50 occurrences → 33 nodes, 17 merged, 43 edges, 137 grounded claims. Section topics kept as hubs, never merged into content entities on an alias alone (the "Warranty" bug). |
| 21 Oct | G4 | `community.py`. Pure-python Louvain with multi-level aggregation; 13 level-0 communities map 1:1 onto the KB's themes. Every community carries claims and a summary (extractive by default, model on `--llm`). |
| 21 Oct | G5 | `query.py`. Local / global / hybrid retrieval; entity score kept separate from the TF-IDF lexical score so G8 can attribute the difference. Intent rules (price / duration / schedule / …) promote the right fact kind — "how much does a maintenance cost" now leads with the price claim. |
| 21 Oct | G6 | `answer.py` + `citation.py`. Every citation's quote re-read from `faq_kb.md` and verified in code before display; unverified quotes are dropped. Deterministic quote-composer by default, model phrasing opt-in. |
| 21 Oct | G7 | `router.py`. Marker rules (global sweep / local fact / workshop policy) + entity hits choose the mode; reasons returned and shown in the front-end. No model call. |
| 21 Oct | G8 | `evaluate.py`. TF-IDF baseline vs GraphRAG on the 9 judged questions + 3 global questions: hit@1 44% → 78%, abstention 78% → 100%, global coverage 3.7 → 7.7 sections. Written to `artifacts/m6_eval.json`. |
| 21 Oct | G9 | `core/` — guardrail (injection / handoff / out-of-scope / PII regex masking), Workflow A orchestrator, JSONL trace with masked messages and per-step timing. 9/9 smoke checks green incl. C11 injection, C13 handoff, C14 abstain, and an NRIC that never reaches the trace. CLI: `workflow "…"`. |
| 21 Oct | G10 | `app.py` — Streamlit front-end, one page per module (tab table in §3), nine judged presets in the Workflow A tab, per-step latency and token cost shown. `cli app` launches it; `_t_app.py` smoke-tests all pages (12/12 green). |
| 24 Oct | G5/G7/G8 + chatbot adapter | **Global mode was dead, and the chatbot never asked for it.** Live test: "Summarise all your policies and rules for customers" got the TF-IDF backstop's price chunk even with the graphrag toggle on. Cause 1: `_global_evidence` rebuilt each scored community and dropped `sample_claims` — the exact field its own evidence loop read two lines below — so global returned 0 evidence, abstained on every sweep question, and G8's coverage metric hid it by counting the (empty) community *selection* instead of evidence. Cause 2: the chatbot adapter hardcoded `mode="local"`, so even the fixed path never ran from :8601. Fixed: `sample_claims` carried through (+grounded-claim guard); abstention became sweep-aware (breadth rule §G5, per-claim τ still gates C14 — COE sits at 0.333 vs 0.35 and a first ungated attempt proved word-overlap can outrank real entity hits, so community claims rank only for sweep-certified questions); marker regexes moved into `query.py` shared with `router.py`; `GraphRagSearch.hit` now asks in the routed mode and merges a global hit's sections (≤5) into one citable Section. Verification: `_t_graphrag_backend` **19/19** (graph hit@1 7/7 — up from 6/7 with BM25 covering), selftest 5/5, `evaluate` regenerated (78%/100% unchanged, coverage honestly 6.33, latency 12.7→8.4 ms), both servers restarted per the orphan-kill protocol and curl-verified. |

### Decisions

| Decision | Why |
| --- | --- |
| Provider-agnostic LLM layer, endpoint probed at runtime | The configured endpoint ignores `output_config.format`. Assuming it works would silently produce unparseable output. |
| Rules engine ships alongside the model engine | Keeps the demo runnable at zero cost, and gives the model engine something to be measured against. |
| Demo lives in `graphrag_demo/`, outside the judged chatbot | The deck's architecture is what gets judged. This is evidence for it, not a replacement. |
| Claims are grounded by code, not trusted from the model | A claim whose quote is not in its cited unit must not be able to produce a citation. |
| The front-end (G10) belongs in the master plan, not bolted on | It is the presentation layer of the deck's own five-layer architecture (slide 4) and the test surface for every module: a module is judged by what you can exercise of it in the UI. |
| Everything offline by default, model explicitly opt-in | Rules engine, extractive summaries, deterministic composer cost nothing; each tab that spends tokens says so and prints the meter. |
| Guardrail covers handoff, not just injection | C13 (damage complaint → human) must never be answered from the FAQ; the guardrail routes it out before retrieval. |
| Sweep-certified questions answer on community **breadth**; everything else on the per-claim τ | breadth and pinpoint relevance are different confidence signals; "summarise all your policies" matches every community at ~0.2 and nothing clears τ, while C14 keeps its teeth because COE/insurance match no sweep marker and stay under the gate. |
| Community claims join the ranking only inside the sweep exception | summary token-overlap is a breadth measure; ungated it outranks legitimate local entity evidence on specific questions (the parking → What-to-bring flip caught by regression, 24 Oct). |
| Sweep markers live in `query.py`, imported by `router.py` | the router and the abstention gate must never drift apart on "is this a sweep?" — one definition, two readers. |
| Chatbot global hits merge ≤5 swept sections into one citable `Section` | keeps the chatbot's one-Section answer contract while the graph's whole point (the sweep) shows through: each quoted line is still verbatim from one KB line, `citation.grounded` holds per line, and the Source lists every section the sweep touched. |
