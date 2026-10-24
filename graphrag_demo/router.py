"""G7 - Router.

Decides how a question should be answered:

``local``
    The question names a specific thing and asks for a specific fact about
    it ("how much does a maintenance cost"). Answer from that thing's
    neighbourhood.

``global``
    The question asks for a sweep across the whole knowledge base ("what
    are all your fees?", "summarise your policies"). Answer from the
    community summaries - the pre-computed whole-corpus index.

``hybrid``
    Everything else. A global primer for the themes, then a local
    drill-down for the specifics.

The decision is made in code from explicit markers, and the reason is
returned with it so the front-end can show the working. No model call:
routing must be predictable and free.
"""

from __future__ import annotations

from .query import (  # markers live with abstention in query.py (one definition)
    GLOBAL_MARKERS,
    LOCAL_MARKERS,
    WORKSHOP_MARKERS,
    QueryEngine,
    detect_intent,
)


class Router:
    """Pick the retrieval mode for a question."""

    def __init__(self, engine: QueryEngine | None = None):
        self.engine = engine or QueryEngine()

    def route(self, question: str) -> dict:
        """Return the chosen mode and the reasons behind it."""
        reasons: list[str] = []
        intent = detect_intent(question)
        entities = self.engine.score_entities(question)

        has_global = bool(GLOBAL_MARKERS.search(question))
        has_local = bool(LOCAL_MARKERS.search(question))
        has_workshop = bool(WORKSHOP_MARKERS.search(question))
        has_entity = bool(entities) and entities[0]["score"] >= 0.8

        if has_entity:
            reasons.append(
                f"named entity matched: '{entities[0]['name']}' "
                f"(via {entities[0]['matched_kind']} '{entities[0]['matched']}')"
            )
        else:
            reasons.append("no entity or alias matched the question")

        if has_global:
            reasons.append("question asks for a whole-corpus sweep")
            mode = "global"
        elif has_entity and has_local:
            reasons.append(f"question asks for one specific fact (intent: {intent})")
            mode = "local"
        elif has_entity and intent != "general":
            reasons.append(f"question asks for one specific fact (intent: {intent})")
            mode = "local"
        elif has_workshop:
            reasons.append("question is about the workshop's own policy")
            mode = "hybrid"
        else:
            reasons.append("no clear marker - use the global primer plus a local drill-down")
            mode = "hybrid"

        confidence = 0.5
        if has_global:
            confidence = 0.8
        elif has_entity and has_local:
            confidence = 0.85
        elif has_entity:
            confidence = 0.7

        return {
            "question": question,
            "mode": mode,
            "intent": intent,
            "confidence": confidence,
            "reasons": reasons,
            "entities": entities[:5],
            "markers": {
                "global": has_global,
                "local": has_local,
                "workshop": has_workshop,
                "entity": has_entity,
            },
        }


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    router = Router()
    for question in sys.argv[1:]:
        decision = router.route(question)
        print(f"{question}\n  -> {decision['mode']}  ({decision['confidence']})")
        for reason in decision["reasons"]:
            print(f"     {reason}")
