"""G5 - Retrieval.

Three modes over the knowledge graph:

* **local** - match the question against entity names *and aliases*, take
  the neighbourhood of the matched entities, and rank their claims. This is
  where the C03 synonym trap is solved: "maintenance" is an alias of
  "Basic servicing package", so the question reaches the right facts even
  though the word never appears in the knowledge base.
* **global** - score every community and return the ones that match,
  each with its summary. This answers the questions retrieval is worst at:
  "what are all your fees?", "summarise your policies".
* **hybrid** - the global primer for themes, then the local drill-down
  for specifics.

Two scores are combined for every candidate:

``entity``
    How well the question matches an entity's name or aliases. Phrase
    containment scores highest; token overlap is the fallback.

``tfidf``
    Cosine similarity between the question and the candidate's own text,
    over TF-IDF vectors fitted on the knowledge base.

The lexical score is what the deck's baseline uses on its own. The entity
score is what the graph adds. Keeping them separate is what makes the
comparison in G8 honest.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from . import config
from .corpus import load_corpus
from .graph import load_graph, normalise

# Words that carry no retrieval signal.
STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "can", "could", "do",
    "does", "for", "from", "get", "have", "how", "i", "if", "in", "is", "it",
    "its", "many", "much", "my", "of", "on", "or", "our", "please", "so",
    "some", "tell", "that", "the", "their", "there", "they", "this", "to",
    "was", "we", "what", "when", "where", "which", "who", "will", "with",
    "would", "you", "your", "am", "im", "ive", "id", "dont", "doesnt",
    "me", "my", "need", "want", "like", "day", "time", "s",
}


def tokenise(text: str) -> list[str]:
    """Lowercase word tokens, minus stopwords."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------
#
# "How much does a maintenance cost" and "how long does a maintenance take"
# retrieve the *same* claims - the entity is right either way. What differs
# is the kind of fact being asked for. This is a rule, not a model call:
# the patterns below are checked in code and the matching claims are
# promoted. "Model for language, code for rules."

INTENT_RULES = [
    ("price", r"\bhow much\b|\bwhat(?:'s| is| are)?\b.*\b(cost|price|fee|charge|rate)s?\b|\b(cost|price|fee|charge)s?\b"),
    ("duration", r"\bhow long\b|\bhow many hours\b|\bduration\b|\btakes?\b"),
    ("schedule", r"\b(when|what time|opening|open|close|hours|weekend|holiday)s?\b"),
    ("location", r"\b(where|address|location|parking|mrt|block|find you)\b"),
    ("procedure", r"\bwhat.*(need|bring|require)|\b(bring|documents?|licence|key|documents)\b"),
    ("policy", r"\b(polic|guarantee|warranty|refund|allowed|eligible|can i|do you|covered)\w*\b"),
]

PRICE_RE = re.compile(r"SGD\s?\d+(?:\.\d+)?", re.IGNORECASE)
HOURS_RE = re.compile(r"\b\d+(?:\.\d+)?\s?-?\s*hour(?:s)?\b", re.IGNORECASE)
CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\s?(?:am|pm)\b", re.IGNORECASE)

CLAIM_INTENT_TAGS = {
    "price": lambda c: c.get("predicate") == "costs" or bool(PRICE_RE.search(c.get("text", ""))),
    "duration": lambda c: c.get("predicate") == "takes" or bool(HOURS_RE.search(c.get("text", ""))),
    "schedule": lambda c: bool(CLOCK_RE.search(c.get("text", ""))) or bool(
        re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", c.get("text", ""), re.I)
    ),
    "location": lambda c: bool(re.search(r"\b(block|mrt|road|street|avenue|ave)\b", c.get("text", ""), re.I)),
    "procedure": lambda c: bool(re.search(r"\b(bring|licence|key|invoice|id card|protection plan)\b", c.get("text", ""), re.I)),
    "policy": lambda c: bool(re.search(r"\b(not|cannot|must|require|guarantee|warranty|refund|within|covers)\b", c.get("text", ""), re.I)),
}


def detect_intent(question: str) -> str:
    """The kind of fact being asked for."""
    lowered = question.lower()
    for name, pattern in INTENT_RULES:
        if re.search(pattern, lowered):
            return name
    return "general"


# ---------------------------------------------------------------------------
# Sweep / drill-down markers
# ---------------------------------------------------------------------------
#
# These live here (not in router.py) because abstention needs them too: a
# question certified as a whole-corpus sweep may be answered off community
# breadth, while a question that merely contains a generic word ("price" in
# a COE query) must still clear the strict per-claim gate. Router imports
# them from here; there is one definition, no drift between the two users.

# Questions that want a sweep across the whole knowledge base.
GLOBAL_MARKERS = re.compile(
    r"\b(summar\w+|overview|all\s+(your|the|our)|list\s+all|everything|"
    r"in\s+general|overall|tell\s+me\s+about|what\s+are\s+your\s+(polic|rule|fee|charg)|"
    r"compare|differences?|explain\s+your|walk\s+me\s+through)\b",
    re.IGNORECASE,
)

# Questions that want one specific fact about one specific thing.
LOCAL_MARKERS = re.compile(
    r"\b(how\s+much|how\s+long|how\s+many|when|where|which|what\s+(is|are|was|were)\s+the|"
    r"can\s+i|do\s+you|is\s+there|are\s+there|my\s+car|my\s+vehicle)\b",
    re.IGNORECASE,
)

# A question about the workshop as a whole.
WORKSHOP_MARKERS = re.compile(
    r"\b(your\s+(polic|rule|fee|charg|service|opening|payment|process|procedure)|"
    r"you\s+(accept|offer|provide|do))\b",
    re.IGNORECASE,
)


class TfidfIndex:
    """A TF-IDF index over a set of documents."""

    def __init__(self, documents: list[str]):
        self.documents = documents
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity

            self.vectorizer = TfidfVectorizer(
                stop_words="english", ngram_range=(1, 2), sublinear_tf=True
            )
            self.matrix = self.vectorizer.fit_transform(documents)
            self._cosine = cosine_similarity
            self.available = True
        except ImportError:  # pragma: no cover - sklearn is installed here
            self.available = False
            self.matrix = None
            self.vectorizer = None

    def scores(self, query: str) -> list[float]:
        """Cosine similarity of `query` against every document."""
        if not self.available or not self.documents:
            return [0.0] * len(self.documents)
        vector = self.vectorizer.transform([query])
        return self._cosine(vector, self.matrix)[0].tolist()


class QueryEngine:
    """Retrieval over the graph, the communities and the corpus."""

    def __init__(self) -> None:
        self.graph = load_graph()
        self.corpus = load_corpus()
        self.communities = self._load_communities()

        self.unit_index = {
            unit["unit_id"]: unit for unit in self.corpus["units"]
        }
        self.section_index = {
            section["section_id"]: section for section in self.corpus["sections"]
        }

        self.claims = self.graph["claims"]
        self.claim_texts = [claim.get("text", "") for claim in self.claims]
        self.tfidf = TfidfIndex(self.claim_texts)

        self.nodes = {node["node_id"]: node for node in self.graph["nodes"]}
        self.claims_by_node: dict[str, list[int]] = defaultdict(list)
        for index, claim in enumerate(self.claims):
            for name in set(claim.get("entities", [])) | {claim.get("subject", "")}:
                node_id = self.graph["spelling_to_node"].get(normalise(name))
                if node_id:
                    self.claims_by_node[node_id].append(index)

    def _load_communities(self) -> list[dict]:
        if not config.COMMUNITIES_PATH.exists():
            return []
        try:
            payload = json.loads(config.COMMUNITIES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return payload.get("communities", [])

    # -- entity matching ------------------------------------------------

    @staticmethod
    def _spelling_score(question_norm: str, question_tokens: set[str], spelling: str) -> float:
        """How well one spelling (a name or an alias) matches the question."""
        key = normalise(spelling)
        if not key or len(key) < 3:
            return 0.0

        if key in question_norm:
            # A whole phrase of the question. Longer phrases are stronger
            # evidence, but an alias match is what we are hunting for.
            return 1.0 + 0.08 * len(key.split())

        spelling_tokens = set(key.split())
        if not spelling_tokens:
            return 0.0
        overlap = len(spelling_tokens & question_tokens) / len(spelling_tokens)
        if overlap >= 0.75:
            return 0.55 * overlap
        return 0.0

    def score_entities(self, question: str) -> list[dict]:
        """Rank the entities the question mentions."""
        question_norm = normalise(question)
        question_tokens = set(tokenise(question))

        scored = []
        for node in self.graph["nodes"]:
            best = 0.0
            matched_spelling = None
            matched_kind = None

            for spelling in [node["name"], *node.get("aliases", [])]:
                score = self._spelling_score(question_norm, question_tokens, spelling)
                if score > best:
                    best = score
                    matched_spelling = spelling
                    matched_kind = "alias" if spelling != node["name"] else "name"

            if best > 0:
                scored.append(
                    {
                        "node_id": node["node_id"],
                        "name": node["name"],
                        "type": node["type"],
                        "aliases": node.get("aliases", []),
                        "score": round(best, 4),
                        "matched": matched_spelling,
                        "matched_kind": matched_kind,
                    }
                )

        scored.sort(key=lambda item: -item["score"])
        return scored

    # -- evidence -------------------------------------------------------

    def _evidence_from_claim(
        self, claim: dict, score: float, source: str, entity: str = ""
    ) -> dict:
        unit = self.unit_index.get(claim.get("unit_id", ""), {})
        section = self.section_index.get(claim.get("section_id", ""), {})
        return {
            "text": claim.get("text", ""),
            "subject": claim.get("subject", ""),
            "predicate": claim.get("predicate", ""),
            "object": claim.get("object", ""),
            "unit_id": claim.get("unit_id", ""),
            "section_id": claim.get("section_id", ""),
            "section_title": unit.get("section_title") or section.get("title", ""),
            "start_line": unit.get("start_line", 0),
            "end_line": unit.get("end_line", 0),
            "score": round(score, 4),
            "source": source,
            "entity": entity,
            "grounded": claim.get("grounded", True),
        }

    def _local_evidence(
        self, question: str, entities: list[dict], top_k: int, intent: str
    ) -> list[dict]:
        """Claims from the neighbourhood of the matched entities."""
        lexical = self.tfidf.scores(question)
        tagger = CLAIM_INTENT_TAGS.get(intent)

        candidates: dict[int, dict] = {}
        for entity in entities:
            for index in self.claims_by_node.get(entity["node_id"], []):
                claim = self.claims[index]
                if not claim.get("grounded", True):
                    continue
                lexical_score = lexical[index] if index < len(lexical) else 0.0
                # The entity match is the graph's contribution; the lexical
                # score is the baseline's. Keep both so G8 can attribute
                # the difference.
                score = max(entity["score"], lexical_score) + 0.25 * lexical_score
                # The intent decides which of the entity's facts answers
                # the question: the price, the duration, or the policy.
                if tagger is not None and tagger(claim):
                    score += 0.60
                existing = candidates.get(index)
                if existing is None or score > existing["score"]:
                    candidates[index] = self._evidence_from_claim(
                        claim, score, "local", entity["name"]
                    )

        evidence = list(candidates.values())
        evidence.sort(key=lambda item: -item["score"])
        return evidence[:top_k]

    def _global_evidence(
        self, question: str, top_k: int
    ) -> tuple[list[dict], list[dict]]:
        """Communities that match the question, plus their best claims."""
        question_norm = normalise(question)
        question_tokens = set(tokenise(question))

        scored_communities = []
        for community in self.communities:
            best = 0.0
            for name in [
                community.get("title", ""),
                community.get("summary", ""),
                *[self.nodes[m]["name"] for m in community.get("members", []) if m in self.nodes],
            ]:
                best = max(best, self._spelling_score(question_norm, question_tokens, name))

            summary_tokens = set(tokenise(community.get("summary", "")))
            overlap = len(summary_tokens & question_tokens) / max(len(question_tokens), 1)
            score = max(best, overlap)

            if score > 0.05:
                scored_communities.append(
                    {
                        "community_id": community["community_id"],
                        "level": community.get("level", 0),
                        "title": community.get("title", ""),
                        "summary": community.get("summary", ""),
                        "sections": community.get("sections", []),
                        "score": round(score, 4),
                        "size": community.get("size", 0),
                        # Carried through, not re-looked-up: the evidence
                        # loop below reads this field, and dropping it here
                        # silently starved global mode of all evidence
                        # (best_score 0 → abstain) until 24 Oct.
                        "sample_claims": community.get("sample_claims", []),
                    }
                )

        scored_communities.sort(key=lambda item: -item["score"])

        evidence: list[dict] = []
        for community in scored_communities[:top_k]:
            for text in community.get("sample_claims", [])[:3]:
                claim = next((c for c in self.claims if c.get("text") == text), None)
                if claim and claim.get("grounded", True):
                    evidence.append(
                        self._evidence_from_claim(
                            claim, community["score"], "community", community["community_id"]
                        )
                    )

        return evidence, scored_communities[:top_k]

    @staticmethod
    def _sweep_spreads(communities: list[dict]) -> bool:
        """Do the matched communities genuinely cover the knowledge base?

        A real sweep touches three or more decent-sized communities that
        together span four or more sections. A stray generic-word match
        ("price", "month") clusters in one part of the graph, so it fails
        here and stays an abstention.
        """
        if len(communities) < 3 or communities[0]["score"] < 0.15:
            return False
        sections: set[str] = set()
        for community in communities[:5]:
            sections.update(community.get("sections", []))
        return len(sections) >= 4

    # -- public ---------------------------------------------------------

    def query(
        self, question: str, mode: str = "hybrid", top_k: int = 8
    ) -> dict:
        """Retrieve the evidence for a question."""
        entities = self.score_entities(question)
        lexical = self.tfidf.scores(question)
        intent = detect_intent(question)

        # A sweep-certified question may answer off community breadth; a
        # specific question must clear the strict per-claim gate.
        sweep = mode == "global" or bool(GLOBAL_MARKERS.search(question))

        best_lexical = max(lexical) if lexical else 0.0
        best_entity = entities[0]["score"] if entities else 0.0

        evidence: list[dict] = []
        communities: list[dict] = []

        if mode in ("local", "hybrid"):
            local = self._local_evidence(question, entities, top_k, intent)
            evidence.extend(local)

        if mode in ("global", "hybrid"):
            global_evidence, communities = self._global_evidence(question, top_k)
            # Community selection always runs (G8 scores coverage on it),
            # but community claims join the ranking only for sweep-
            # certified questions: summary word-overlap is a breadth
            # measure, and left ungated it outranks legitimate local
            # entity evidence on specific questions ("where can I park"
            # matching the What-to-bring summary's "drop off your car").
            if sweep:
                evidence.extend(global_evidence)

        # Merge and rank, keeping the strongest copy of each claim.
        merged: dict[str, dict] = {}
        for item in evidence:
            key = f"{item['unit_id']}|{item['text'][:80]}"
            existing = merged.get(key)
            if existing is None or item["score"] > existing["score"]:
                merged[key] = item
        ranked = sorted(merged.values(), key=lambda item: -item["score"])[:top_k]

        best_score = ranked[0]["score"] if ranked else 0.0

        # A router-certified sweep may be answered off community BREADTH:
        # "summarise all your policies" legitimately matches many
        # communities weakly (~0.2 each), and no single claim clears the
        # pinpoint gate. Local and hybrid keep the strict gate — that is
        # how the C14 COE/insurance abstentions survive ("COE price" hits
        # the price community at 0.333, just under 0.35).
        abstain = best_score < config.ABSTAIN_THRESHOLD and not (
            sweep and bool(ranked) and self._sweep_spreads(communities)
        )

        return {
            "question": question,
            "mode": mode,
            "intent": intent,
            "evidence": ranked,
            "entities": entities[:8],
            "communities": communities,
            "best_score": round(best_score, 4),
            "best_entity_score": round(best_entity, 4),
            "best_lexical_score": round(best_lexical, 4),
            "abstain": abstain,
        }


if __name__ == "__main__":  # pragma: no cover
    import sys

    engine = QueryEngine()
    question = sys.argv[1] if len(sys.argv) > 1 else "How much does a maintenance cost?"
    for item in engine.query(question)["evidence"]:
        print(f"[{item['score']:.3f}] {item['section_title']} :: {item['text'][:90]}")
