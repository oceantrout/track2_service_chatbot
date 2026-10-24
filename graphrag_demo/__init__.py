"""GraphRAG demo for the Kallang Auto Care FAQ knowledge base.

A standalone, module-by-module implementation of the GraphRAG pipeline
described in docs/GRAPHRAG_DEMO.md. It sits alongside the mandated
TF-IDF chatbot and is used to compare retrieval quality.

Modules:
    corpus      M1  markdown -> sections -> atomic text units
    extract     M2  LLM entity / alias / claim / relationship extraction
    graph       M3  graph construction + entity resolution
    community   M4  community detection + community summaries
    query       M5  local / global / hybrid retrieval
    answer      M6  grounded answer generation with citations
    evaluate    M6  GraphRAG vs TF-IDF benchmark
"""

__all__ = ["config"]
