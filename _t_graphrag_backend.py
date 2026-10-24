"""Sanity check: the optional GraphRAG FAQ backend (P2 swap, app toggle).

Run:  PYTHONPATH=. python _t_graphrag_backend.py

The judged suite always runs the lexical baseline; this proves the
*other* backend is actually wired — every FAQ question in the test set
goes through FaqEngine(backend="graphrag") end to end (retrieve → answer
→ cite) and lands on a real section, and that the deck's abstain case
still abstains. Also prints the graphrag-vs-baseline hit@1 comparison so
the G8 story can be verified on the current (re-calibrated BM25) baseline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from faq.answer import FaqEngine
from faq.index import load_sections

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name}")


def main() -> int:
    titles = {s.title for s in load_sections()}
    rows = [json.loads(l) for l in
            Path("test_set/conversations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    faq_turns = [(c["id"], t) for c in rows for t in c["turns"] if "expected_section" in t]
    print(f"{len(faq_turns)} judged FAQ turns; graphrag engine building...", flush=True)

    graph = FaqEngine(backend="graphrag")
    base = FaqEngine(backend="tfidf")

    g_hits = b_hits = 0
    for cid, turn in faq_turns:
        q = turn["user"]
        want = turn["expected_section"]          # None on judged ABSTAIN turns
        gres = graph.answer(q)
        gsec = gres["section"] if not gres["abstained"] else None
        bres = base.answer(q)
        if want is None:
            # the deck's no-answer turn: both backends must abstain
            check(f"{cid} graphrag abstains (no-answer turn)", gres["abstained"])
            check(f"{cid} baseline abstains (no-answer turn)", bres["abstained"])
            continue
        if gsec is not None:
            g_hits += (gsec == want)
            check(f"{cid} graphrag section is a real title ({q[:30]!r})", gsec in titles)
            check(f"{cid} graphrag cites it", gres["answer"].endswith(f"Source: {gsec}"))
        else:
            print(f"  note: {cid} graphrag abstained on {q[:50]!r} (want {want!r})")
        if not bres["abstained"]:
            b_hits += (bres["section"] == want)
        print(f"{cid:4s} {q[:46]:46s} want={want[:28]:28s} "
              f"graph={str(gsec)[:28]:28s} base={str(bres['section'])[:28]}")

    answerable = [t for _, t in faq_turns if t["expected_section"] is not None]
    print(f"\ngraphrag hit@1: {g_hits}/{len(answerable)} "
          f"· current BM25 baseline hit@1: {b_hits}/{len(answerable)} "
          f"(+{len(faq_turns) - len(answerable)} judged abstain turns)")
    check("graphrag wiring lands the judged sections", g_hits == len(answerable))
    print(f"\n_t_graphrag_backend: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
