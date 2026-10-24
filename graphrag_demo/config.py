"""Paths, model settings and shared constants for the GraphRAG demo."""

from __future__ import annotations

from pathlib import Path

# graphrag_demo/config.py -> graphrag_demo/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TEST_DIR = PROJECT_ROOT / "test_set"
DOCS_DIR = PROJECT_ROOT / "docs"

FAQ_KB_PATH = DATA_DIR / "faq_kb.md"
SERVICES_PATH = DATA_DIR / "services.csv"
TEST_SET_PATH = TEST_DIR / "conversations.jsonl"

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"

# --- Artifacts produced by each module -------------------------------
CORPUS_PATH = ARTIFACT_DIR / "m1_corpus.json"
CLAIMS_PATH = ARTIFACT_DIR / "m2_claims.jsonl"
EXTRACTED_PATH = ARTIFACT_DIR / "m2_extracted.json"
GRAPH_PATH = ARTIFACT_DIR / "m3_graph.json"
COMMUNITIES_PATH = ARTIFACT_DIR / "m4_communities.json"
EVAL_PATH = ARTIFACT_DIR / "m6_eval.json"
TRACE_PATH = ARTIFACT_DIR / "m9_trace.jsonl"

# --- Model settings ---------------------------------------------------
# Extraction and answering are both intelligence-sensitive, so we use the
# strongest current model. `effort` is set explicitly because Opus 5.5
# defaults to "medium".
MODEL = "claude-opus-5-5"
EXTRACTION_EFFORT = "medium"
ANSWER_EFFORT = "high"

# Server-side refusal fallback: routes to a different model by category
# instead of us maintaining a model list.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

MAX_TOKENS_EXTRACTION = 8000
MAX_TOKENS_ANSWER = 4000

# --- GraphRAG knobs ---------------------------------------------------
# Target characters per text unit before a paragraph is split further.
MAX_UNIT_CHARS = 700

# Leiden resolution: higher = smaller communities.
LEIDEN_RESOLUTION = 1.0

# Similarity floor for a claim to count as "relevant" in local search.
LOCAL_MIN_SCORE = 0.15

# Below this score the engine says it does not know (test C14).
ABSTAIN_THRESHOLD = 0.35
