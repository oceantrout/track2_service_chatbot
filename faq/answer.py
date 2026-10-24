"""faq/answer.py — grounded composition, default deterministic.

Deck: the model only rephrases the *winning section*, never the whole KB
(one chunk in, one answer out — nothing to blend, no hallucination surface
to widen). The default composer here is pure code: it quotes the section's
most relevant lines verbatim. A model may replace the rewording step
through core/gateway.py (max one call), and its output is still checked
with citation.grounded before it ships; the Source: line is code's, always.
"""

from __future__ import annotations

import re

from faq import citation, search as faq_search

ABSTAIN_TEXT = "I don't have that information — I can pass you to our service desk."


class FaqEngine:
    def __init__(self, backend: str = "tfidf", llm=None):
        self.search = faq_search.build(backend)
        self.llm = llm  # optional rewording; None by default (deviation §11)

    def answer(self, question: str) -> dict:
        hit = self.search.hit(question)
        if hit is None:
            return {
                "answer": ABSTAIN_TEXT,
                "section": None,
                "score": None,
                "abstained": True,
            }
        score, section = hit
        if self.llm is not None:
            body = self._compose_with_model(question, section)
        else:
            body = self._compose_deterministically(question, section)
        if not citation.grounded(body, section):
            body = self._compose_deterministically(question, section)
        return {
            "answer": f"{body}\n{citation.cite(section)}",
            "section": section.title,
            "score": round(score, 4),
            "abstained": False,
        }

    # ---- composers ----------------------------------------------------------

    def _compose_deterministically(self, question: str, section) -> str:
        """Quote up to 3 lines of the winning section, best token overlap first."""
        q_tokens = set(_words(question))
        lines = [ln.strip("-• ").strip() for ln in section.text.splitlines() if ln.strip()]
        ranked = sorted(
            lines,
            key=lambda ln: (len(q_tokens & set(_words(ln))), -len(ln.split())),
            reverse=True,
        )
        if ranked and q_tokens & set(_words(ranked[0])):
            chosen = ranked[:3]
        else:
            chosen = lines[:2]
        # One quoted line per KB line (never a stitched cross-section
        # sentence — that is what citation.grounded checks per line).
        return "\n".join(chosen)

    def _compose_with_model(self, question: str, section) -> str:
        prompt = (
            "Answer ONLY from this section of the workshop's FAQ. Quote it "
            "closely; add nothing. Before answering, silently correct any "
            "spelling errors or typos in the question so it matches the "
            "section's wording; do not mention the correction in your "
            "answer.\n\nSection: " + section.title + "\n"
            + section.text + "\n\nQuestion: " + question
        )
        return self.llm.complete(prompt).strip()


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'%\-]*", text.lower()) if w not in STOPWORDS]

STOPWORDS = {"the", "a", "an", "and", "or", "to", "of", "is", "are", "for", "on", "in", "at", "you", "your", "do", "does", "can", "i", "we", "it", "be", "what", "how", "much"}
