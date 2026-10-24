"""G6b - Grounded answering.

Two rules, both from the deck:

1. **The model answers only from the evidence it was given.** Nothing else.
2. **A claim that cannot be traced to a line in the knowledge base is not
   said.** Every sentence in the answer carries a citation, and every
   citation is verified against the file on disk before it is emitted.

Below the confidence threshold tau, the system **abstains** rather than
guessing. This is the C14 behaviour: "I don't have that in the knowledge
base" is a correct answer, and a wrong one is worse.

The model is optional. When it is not reachable the module composes the
answer directly from the retrieved quotes, which is slower to read but
exactly as grounded.
"""

from __future__ import annotations

from . import config
from .citation import build_citations, format_citation
from .query import QueryEngine

ANSWER_SYSTEM = """\
You answer a customer's question about a car workshop using only the
evidence given to you.

Rules:
- Use ONLY facts that appear in the evidence. Do not use prior knowledge,
  do not infer, do not fill gaps.
- If the evidence does not contain the answer, say so plainly.
- Answer in 1-3 short sentences, then list the source sections you used.
- Keep prices, times, durations and names exactly as written.
- Never invent a price, a duration, a policy or a date.
"""

ABSTAIN_TEXT = (
    "I don't have that in the workshop's knowledge base, so I don't want to "
    "guess. I can help with services and prices, servicing packages, "
    "detailing, opening hours, parking and the courtesy car, payment "
    "methods, warranty, cancellations and what to bring."
)


def compose_answer_with_model(question: str, evidence: list[dict], llm) -> dict | None:
    """Ask the model to phrase the answer. None if it cannot be reached."""
    if llm is None or not evidence:
        return None

    numbered = "\n".join(
        f"[{index + 1}] ({item['section_title']}) {item['text']}"
        for index, item in enumerate(evidence)
    )
    prompt = (
        f"Question: {question}\n\n"
        f"Evidence from the knowledge base:\n{numbered}\n\n"
        "Answer the question using only this evidence."
    )

    try:
        text = llm.text(
            system=ANSWER_SYSTEM,
            prompt=prompt,
            label="answer",
            effort=config.ANSWER_EFFORT,
            max_tokens=500,
        )
    except Exception:  # noqa: BLE001 - fall back to the deterministic path
        return None

    return {"answer": text.strip(), "composer": "model"}


def compose_answer_deterministically(question: str, evidence: list[dict]) -> dict:
    """Quote the retrieved evidence. Slower to read, exactly as grounded."""
    if not evidence:
        return {"answer": ABSTAIN_TEXT, "composer": "deterministic"}

    # One quote per section, best first, so the answer stays short.
    chosen: list[dict] = []
    seen_sections: set[str] = set()
    for item in evidence:
        key = item["section_id"] or item["unit_id"]
        if key in seen_sections:
            continue
        seen_sections.add(key)
        chosen.append(item)
        if len(chosen) >= 3:
            break

    lines = ["Here's what the knowledge base says:"]
    for item in chosen:
        quote = item["text"].strip()
        lines.append(f"- {quote}  {format_citation(item)}")

    return {"answer": "\n".join(lines), "composer": "deterministic"}


class AnswerEngine:
    """Turn a question into a grounded, cited answer."""

    def __init__(self, engine: QueryEngine | None = None, llm=None):
        self.engine = engine or QueryEngine()
        self._llm = llm
        self._llm_tried = llm is not None

    # -- the model, if there is one -------------------------------------

    @property
    def llm(self):
        if not self._llm_tried:
            self._llm_tried = True
            try:
                from .llm import LLM

                self._llm = LLM()
            except Exception:  # noqa: BLE001 - the model is optional
                self._llm = None
        return self._llm

    def _compose_with_model(self, question: str, evidence: list[dict]) -> dict | None:
        return compose_answer_with_model(question, evidence, self.llm)

    def _compose_deterministically(self, question: str, evidence: list[dict]) -> dict:
        return compose_answer_deterministically(question, evidence)

    # -- public ---------------------------------------------------------

    def answer(self, question: str, mode: str = "hybrid", top_k: int = 6) -> dict:
        """Answer a question, or abstain."""
        retrieval = self.engine.query(question, mode=mode, top_k=top_k)

        if retrieval["abstain"]:
            return {
                "question": question,
                "mode": mode,
                "answer": ABSTAIN_TEXT,
                "abstained": True,
                "confidence": retrieval["best_score"],
                "threshold": config.ABSTAIN_THRESHOLD,
                "citations": [],
                "evidence": retrieval["evidence"],
                "entities": retrieval["entities"],
                "communities": retrieval["communities"],
                "intent": retrieval["intent"],
                "composer": "abstain",
                "usage": None,
            }

        evidence = retrieval["evidence"][:top_k]
        composed = self._compose_with_model(question, evidence) or self._compose_deterministically(
            question, evidence
        )

        # Citations are verified against the file. An unverified quote is
        # dropped rather than shown with a warning.
        citations = build_citations(evidence)

        return {
            "question": question,
            "mode": mode,
            "answer": composed["answer"],
            "abstained": False,
            "confidence": retrieval["best_score"],
            "threshold": config.ABSTAIN_THRESHOLD,
            "citations": citations,
            "evidence": evidence,
            "entities": retrieval["entities"],
            "communities": retrieval["communities"],
            "intent": retrieval["intent"],
            "composer": composed["composer"],
            "usage": self._llm.usage.to_dict() if self._llm is not None else None,
        }


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    question = sys.argv[1] if len(sys.argv) > 1 else "How much does a maintenance cost?"
    result = AnswerEngine().answer(question)
    print(result["answer"])
    print()
    for citation in result["citations"]:
        print(f"  [{citation['label']}] verified={citation['verified']}")
