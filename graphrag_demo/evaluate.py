"""G8 - Evaluation.

Compares the GraphRAG pipeline against the TF-IDF baseline the deck
proposes, on the judged questions from `test_set/conversations.jsonl`.

The baseline is deliberately the *fair* version of itself: TF-IDF over the
knowledge base's own chunks, each chunk carrying its section title. No
synonym expansion - the deck is explicit that plain TF-IDF with no query
expansion is what fails C03, and inflating the baseline would flatter the
result.

Two question classes:

* **targeted** - one question, one expected section. Scored on section
  hit@1 / hit@3 and on whether the system abstained when it should not.
* **global** - a question about the whole knowledge base. Scored on how
  many distinct sections the answer covers. A single retrieved chunk can
  cover one; a community summary can cover many. This is the case vector
  RAG cannot answer at all.

The evaluation runs on retrieval only. No model call, no cost, no
network - so the numbers are reproducible and the comparison is about
retrieval, not about how well a model words things.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from . import config
from .corpus import load_corpus
from .query import QueryEngine, TfidfIndex
from .router import Router

# Questions that only a whole-corpus index can answer.
GLOBAL_QUESTIONS = [
    {
        "question": "What are all the fees and charges at the workshop?",
        "expect": "a sweep across every fee, not one paragraph",
    },
    {
        "question": "Summarise all your policies and rules for customers.",
        "expect": "a sweep across every policy, not one paragraph",
    },
    {
        "question": "List every service you offer and what each one costs.",
        "expect": "a sweep across the price list, not one paragraph",
    },
]


def load_judged_questions() -> list[dict]:
    """The retrieval questions from the test set, with their expected section."""
    path = config.PROJECT_ROOT / "test_set" / "conversations.jsonl"
    judged = []
    if not path.exists():
        return judged

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        conversation = json.loads(line)
        for turn in conversation.get("turns", []):
            expected = turn.get("expected_section")
            if expected is None and turn.get("expected_route") != "faq":
                continue
            judged.append(
                {
                    "id": conversation["id"],
                    "title": conversation["title"],
                    "question": turn["user"],
                    "expected_section": expected,
                    "expect_abstain": expected is None,
                }
            )
    return judged


class TfidfBaseline:
    """The deck's baseline: TF-IDF over the knowledge base's chunks."""

    def __init__(self) -> None:
        corpus = load_corpus()
        self.units = corpus["units"]
        # Each chunk carries its section title - the standard way to chunk
        # a document, and the strongest honest form of the baseline.
        self.texts = [
            f"{unit['section_title']}\n{unit['text']}" for unit in self.units
        ]
        self.index = TfidfIndex(self.texts)

    def retrieve(self, question: str, top_k: int = 5) -> list[dict]:
        scores = self.index.scores(question)
        ranked = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_k]
        return [
            {
                "section_title": self.units[i]["section_title"],
                "section_id": self.units[i]["section_id"],
                "text": self.units[i]["text"],
                "score": round(scores[i], 4),
            }
            for i in ranked
        ]


class GraphRagSystem:
    """Router + retrieval, with the answer layer left out (retrieval only)."""

    def __init__(self) -> None:
        self.engine = QueryEngine()
        self.router = Router(self.engine)

    def retrieve(self, question: str, top_k: int = 8) -> dict:
        decision = self.router.route(question)
        result = self.engine.query(question, mode=decision["mode"], top_k=top_k)
        sections = []
        for item in result["evidence"]:
            if item["section_title"] and item["section_title"] not in sections:
                sections.append(item["section_title"])
        community_sections = []
        for community in result["communities"]:
            for section in community.get("sections", []):
                if section not in community_sections:
                    community_sections.append(section)
        return {
            "mode": decision["mode"],
            "intent": result["intent"],
            "abstain": result["abstain"],
            "score": result["best_score"],
            "sections": sections,
            "community_sections": community_sections,
            "evidence": result["evidence"],
            "communities": result["communities"],
        }


def evaluate(questions=None, global_questions=None) -> dict:
    """Run the comparison and write the artifact."""
    judged = questions if questions is not None else load_judged_questions()
    globals_ = global_questions if global_questions is not None else GLOBAL_QUESTIONS

    baseline = TfidfBaseline()
    system = GraphRagSystem()

    rows = []
    baseline_hit1 = baseline_hit3 = 0
    graph_hit1 = graph_hit3 = 0
    baseline_abstain_ok = graph_abstain_ok = 0
    baseline_ms = graph_ms = 0.0

    for item in judged:
        question = item["question"]
        expected = item["expected_section"]

        start = time.perf_counter()
        baseline_result = baseline.retrieve(question)
        baseline_ms += (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        graph_result = system.retrieve(question)
        graph_ms += (time.perf_counter() - start) * 1000

        baseline_sections = []
        for chunk in baseline_result:
            if chunk["section_title"] not in baseline_sections:
                baseline_sections.append(chunk["section_title"])

        # The baseline has no confidence signal of its own, so it never
        # abstains - it always returns its best chunk. That is exactly the
        # failure mode C14 catches.
        baseline_abstained = False
        graph_abstained = graph_result["abstain"]

        b_hit1 = expected is not None and expected in baseline_sections[:1]
        b_hit3 = expected is not None and expected in baseline_sections[:3]
        g_hit1 = expected is not None and expected in graph_result["sections"][:1]
        g_hit3 = expected is not None and expected in graph_result["sections"][:3]

        baseline_hit1 += b_hit1
        baseline_hit3 += b_hit3
        graph_hit1 += g_hit1
        graph_hit3 += g_hit3

        # Abstention correctness: must abstain on the unanswerable
        # questions, must NOT abstain on the answerable ones.
        b_abstain_ok = (not item["expect_abstain"]) and not baseline_abstained
        g_abstain_ok = graph_abstained == item["expect_abstain"]
        baseline_abstain_ok += b_abstain_ok
        graph_abstain_ok += g_abstain_ok

        rows.append(
            {
                "id": item["id"],
                "title": item["title"],
                "question": question,
                "expected_section": expected,
                "expect_abstain": item["expect_abstain"],
                "baseline": {
                    "sections": baseline_sections[:3],
                    "hit1": b_hit1,
                    "hit3": b_hit3,
                    "abstained": baseline_abstained,
                    "abstain_correct": b_abstain_ok,
                    "score": baseline_result[0]["score"] if baseline_result else 0.0,
                },
                "graphrag": {
                    "mode": graph_result["mode"],
                    "intent": graph_result["intent"],
                    "sections": graph_result["sections"][:3],
                    "hit1": g_hit1,
                    "hit3": g_hit3,
                    "abstained": graph_abstained,
                    "abstain_correct": g_abstain_ok,
                    "score": graph_result["score"],
                },
            }
        )

    total = max(len(rows), 1)

    # -- the global questions ------------------------------------------
    global_rows = []
    baseline_coverage = 0
    graph_coverage = 0

    for item in globals_:
        question = item["question"]
        baseline_result = baseline.retrieve(question, top_k=8)
        graph_result = system.retrieve(question, top_k=8)

        b_sections = []
        for chunk in baseline_result:
            if chunk["section_title"] not in b_sections:
                b_sections.append(chunk["section_title"])
        g_sections = graph_result["sections"] or graph_result["community_sections"]

        baseline_coverage += len(b_sections)
        graph_coverage += len(g_sections)

        global_rows.append(
            {
                "question": question,
                "expect": item["expect"],
                "baseline_sections": b_sections,
                "baseline_coverage": len(b_sections),
                "graphrag_mode": graph_result["mode"],
                "graphrag_sections": g_sections,
                "graphrag_coverage": len(g_sections),
                "graphrag_communities": [
                    c["community_id"] for c in graph_result["communities"][:5]
                ],
            }
        )

    n_globals = max(len(global_rows), 1)

    summary = {
        "questions": len(rows),
        "baseline_hit1": round(baseline_hit1 / total, 4),
        "baseline_hit3": round(baseline_hit3 / total, 4),
        "graphrag_hit1": round(graph_hit1 / total, 4),
        "graphrag_hit3": round(graph_hit3 / total, 4),
        "baseline_abstain_correct": round(baseline_abstain_ok / total, 4),
        "graphrag_abstain_correct": round(graph_abstain_ok / total, 4),
        "baseline_avg_ms": round(baseline_ms / total, 2),
        "graphrag_avg_ms": round(graph_ms / total, 2),
        "global_questions": len(global_rows),
        "baseline_global_coverage": round(baseline_coverage / n_globals, 2),
        "graphrag_global_coverage": round(graph_coverage / n_globals, 2),
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "rows": rows,
        "global_rows": global_rows,
    }

    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    config.EVAL_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return payload


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = evaluate()
    summary = result["summary"]
    print("SUMMARY")
    for key, value in summary.items():
        print(f"  {key:<32} {value}")
    print()
    for row in result["rows"]:
        mark = "PASS" if row["graphrag"]["hit1"] else ("abst" if row["expect_abstain"] else "FAIL")
        print(f"  [{mark:>4}] {row['id']} {row['question'][:52]}")
        print(f"          expected: {row['expected_section']}")
        print(f"          baseline: {row['baseline']['sections'][:2]}")
        print(f"          graphrag: {row['graphrag']['sections'][:2]}  mode={row['graphrag']['mode']}")
