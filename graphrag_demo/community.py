"""G4 - Community detection and community summaries.

The graph is partitioned into communities with Louvain (implemented here in
pure Python, because `networkx` and `graspologic` are not installed and the
graph is far too small to need them). Each community is then summarised.

The summaries are the artefact that makes **global** questions answerable.
A community summary is a pre-computed, cross-document abstraction - the
thing naive vector RAG has no way to produce. A question like "what are all
your fees?" is answered by reading the summaries of the communities that
mention fees, not by retrieving a paragraph.

The summary comes from the model when one is reachable, and from an
extractive fallback otherwise, so the module always produces a usable
artifact.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone

from . import config
from .graph import load_graph, normalise

# ---------------------------------------------------------------------------
# Louvain
# ---------------------------------------------------------------------------


def _weighted_adjacency(nodes: list[str], edges: list[dict]) -> dict[str, dict[str, float]]:
    adjacency: dict[str, dict[str, float]] = {node: {} for node in nodes}
    for edge in edges:
        source, target = edge["source"], edge["target"]
        weight = float(edge.get("weight", 1.0))
        adjacency.setdefault(source, {})[target] = (
            adjacency.setdefault(source, {}).get(target, 0.0) + weight
        )
        adjacency.setdefault(target, {})[source] = (
            adjacency.setdefault(target, {}).get(target, 0.0) + weight
        )
    return adjacency


def _louvain_level(
    nodes: list[str], edges: list[dict], resolution: float
) -> dict[str, int]:
    """One Louvain level: optimise modularity, return node -> community id."""
    adjacency = _weighted_adjacency(nodes, edges)

    total_weight = sum(float(e.get("weight", 1.0)) for e in edges)
    if total_weight == 0:
        return {node: index for index, node in enumerate(nodes)}

    degree = {
        node: sum(neighbour_weights.values()) for node, neighbour_weights in adjacency.items()
    }

    community: dict[str, int] = {node: index for index, node in enumerate(nodes)}
    community_total = {index: degree[node] for index, node in enumerate(nodes)}

    improved = True
    while improved:
        improved = False
        for node in nodes:
            node_community = community[node]
            node_degree = degree[node]
            if node_degree == 0:
                continue

            # Remove the node, then see where it would do best.
            community_total[node_community] -= node_degree
            links: dict[int, float] = defaultdict(float)
            for neighbour, weight in adjacency.get(node, {}).items():
                links[community[neighbour]] += weight

            best_community = node_community
            best_gain = links.get(node_community, 0.0) - resolution * (
                community_total.get(node_community, 0.0) * node_degree / total_weight
            )

            for candidate, weight in links.items():
                gain = weight - resolution * (
                    community_total.get(candidate, 0.0) * node_degree / total_weight
                )
                if gain > best_gain + 1e-12:
                    best_gain = gain
                    best_community = candidate

            community[node] = best_community
            community_total[best_community] = community_total.get(best_community, 0.0) + node_degree
            if best_community != node_community:
                improved = True

    # Renumber communities densely.
    remap: dict[int, int] = {}
    assignment: dict[str, int] = {}
    for node in nodes:
        old = community[node]
        if old not in remap:
            remap[old] = len(remap)
        assignment[node] = remap[old]
    return assignment


def detect_communities(
    nodes: list[str], edges: list[dict], resolution: float = config.LEIDEN_RESOLUTION
) -> list[dict]:
    """Run Louvain to convergence, returning the hierarchy of levels.

    Every level reports the **original** node ids in each group, so a
    community at level 1 can be traced back to the entities it contains.
    """
    levels: list[dict] = []
    current_nodes = list(nodes)
    current_edges = list(edges)
    # Maps a node id at the current level to the original node ids it covers.
    origin: dict[str, set[str]] = {node: {node} for node in nodes}

    for level in range(3):  # a small graph converges well before three levels
        if len(current_nodes) <= 1:
            break
        assignment = _louvain_level(current_nodes, current_edges, resolution)

        groups: dict[int, list[str]] = defaultdict(list)
        for node, community_id in assignment.items():
            groups[community_id].append(node)

        # The original members of each group.
        group_original: dict[int, set[str]] = {}
        for community_id, members in groups.items():
            covered: set[str] = set()
            for member in members:
                covered |= origin.get(member, {member})
            group_original[community_id] = covered

        levels.append(
            {
                "level": level,
                "groups": {
                    community_id: sorted(group_original[community_id])
                    for community_id in sorted(groups)
                },
            }
        )

        # Aggregate: communities become the nodes of the next level.
        aggregated_edges: dict[tuple[int, int], float] = defaultdict(float)
        for edge in current_edges:
            source = assignment.get(edge["source"])
            target = assignment.get(edge["target"])
            if source is None or target is None or source == target:
                continue
            key = (source, target) if source < target else (target, source)
            aggregated_edges[key] += float(edge.get("weight", 1.0))

        next_nodes = sorted(groups)
        next_edges = [
            {"source": source, "target": target, "weight": weight}
            for (source, target), weight in aggregated_edges.items()
        ]
        if len(next_nodes) == len(current_nodes):
            break

        origin = {
            community_id: set(group_original[community_id])
            for community_id in groups
        }
        current_nodes, current_edges = next_nodes, next_edges

    return levels


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM = """\
You summarise a cluster of facts from a car workshop's customer knowledge
base. Write 2-3 sentences that capture what the cluster is about.

Rules:
- Use only the facts given. Do not add anything, do not infer, do not
  generalise beyond the text.
- Keep the concrete details: prices, times, limits, durations, names.
- Write for a customer, not for an analyst. No bullet points, no headers.
- Do not start with "This cluster" or "This section". Start with the subject.
"""

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "how", "i", "if", "in", "is", "it", "its", "my", "of", "on", "or",
    "our", "so", "that", "the", "their", "there", "they", "this", "to", "was",
    "we", "what", "when", "where", "which", "who", "will", "with", "you",
    "your", "can", "could", "do", "does", "much", "many", "would", "should",
}


def _extractive_summary(claims: list[str], limit: int = 3) -> str:
    """A deterministic fallback summary: the most informative sentences."""
    scored = []
    for text in claims:
        tokens = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]
        # Prefer sentences with numbers - they carry the actionable facts.
        numbers = len(re.findall(r"\d", text))
        scored.append((len(set(tokens)) + numbers * 3, text))
    scored.sort(key=lambda item: -item[0])
    picked = [text for _, text in scored[:limit]]
    return " ".join(picked)


def _summarise_with_model(llm, claims: list[str], label: str) -> str | None:
    """Ask the model for a summary; None if it cannot be reached."""
    if not claims:
        return None
    body = "\n".join(f"- {text}" for text in claims[:40])
    try:
        return llm.text(
            system=SUMMARY_SYSTEM,
            prompt=f"Facts from one cluster of the knowledge base:\n\n{body}\n\nSummarise this cluster.",
            label=label,
            effort="medium",
            max_tokens=400,
        )
    except Exception:  # noqa: BLE001 - a summary is optional, not required
        return None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_communities(force_summaries: bool = False, use_llm: bool = True) -> dict:
    """Detect communities and summarise each one."""
    graph = load_graph()
    nodes = [node["node_id"] for node in graph["nodes"]]
    edges = graph["edges"]
    claims = graph["claims"]

    # The section list, for mapping a claim back to its section's topic.
    extracted_sections: list[dict] = []
    if config.EXTRACTED_PATH.exists():
        extracted_sections = json.loads(
            config.EXTRACTED_PATH.read_text(encoding="utf-8")
        ).get("sections", [])

    if not nodes:
        raise ValueError("the graph has no nodes - run: python -m graphrag_demo.cli graph")

    levels = detect_communities(nodes, edges)

    node_by_id = {node["node_id"]: node for node in graph["nodes"]}

    # Reuse cached summaries where we can.
    cached: dict[str, str] = {}
    if config.COMMUNITIES_PATH.exists() and not force_summaries:
        try:
            previous = json.loads(config.COMMUNITIES_PATH.read_text(encoding="utf-8"))
            cached = {
                comm["key"]: comm["summary"]
                for comm in previous.get("communities", [])
            }
        except (json.JSONDecodeError, OSError, KeyError):
            cached = {}

    llm = None
    if use_llm:
        try:
            from .llm import LLM

            llm = LLM()
        except Exception:  # noqa: BLE001 - fall back to extractive summaries
            llm = None

    communities: list[dict] = []
    counter = 0

    # The node that represents each section's topic.
    section_topic_node: dict[str, str] = dict(graph.get("section_topics", {}))
    if not section_topic_node:
        for section in extracted_sections:
            topic_id = graph["spelling_to_node"].get(normalise(section.get("section_title", "")))
            if topic_id:
                section_topic_node[section["section_id"]] = topic_id

    for level in levels:
        for group_id, member_ids in level["groups"].items():
            counter += 1
            members = sorted(member_ids)
            member_set = set(members)

            # The claims that belong to this community: a claim belongs if it
            # mentions a member entity, or if it comes from a section whose
            # topic is a member.
            member_claims = []
            seen = set()
            for claim in claims:
                names = set(claim.get("entities", [])) | {claim.get("subject", "")}
                node_ids = {
                    graph["spelling_to_node"].get(normalise(name))
                    for name in names
                }
                node_ids.discard(None)
                topic_id = section_topic_node.get(claim.get("section_id", ""))
                if topic_id:
                    node_ids.add(topic_id)

                if not (node_ids & member_set):
                    continue
                text = claim.get("text", "")
                if text and text not in seen and claim.get("grounded", True):
                    seen.add(text)
                    member_claims.append({**claim, "node_id": sorted(node_ids & member_set)[0]})

            texts = [c["text"] for c in member_claims]
            names = [node_by_id[m]["name"] for m in members if m in node_by_id]

            # A stable key so summaries survive a re-run.
            key = f"L{level['level']}:" + "|".join(sorted(members))

            summary = cached.get(key)
            summary_source = "cached"
            if summary is None:
                summary = None
                if llm is not None:
                    summary = _summarise_with_model(llm, texts, f"summarize:L{level['level']}")
                    if summary:
                        summary_source = "model"
                if not summary:
                    summary = _extractive_summary(texts)
                    summary_source = "extractive"

            sections = sorted({c.get("section_id", "") for c in member_claims if c.get("section_id")})

            communities.append(
                {
                    "community_id": f"c{counter:03d}",
                    "key": key,
                    "level": level["level"],
                    "members": members,
                    "size": len(members),
                    "title": ", ".join(names[:4]) + ("…" if len(names) > 4 else ""),
                    "summary": summary,
                    "summary_source": summary_source,
                    "sections": sections,
                    "claim_count": len(member_claims),
                    "sample_claims": [c["text"] for c in member_claims[:3]],
                }
            )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": graph.get("engine", "unknown"),
        "levels": len(levels),
        "communities": communities,
        "usage": llm.usage.to_dict() if llm else None,
    }
    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    config.COMMUNITIES_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return payload


def load_communities() -> dict:
    """Load the G4 artifact, building it if it does not exist."""
    if not config.COMMUNITIES_PATH.exists():
        return build_communities(use_llm=False)
    return json.loads(config.COMMUNITIES_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    result = build_communities()
    print(f"{len(result['communities'])} communities")
    for comm in result["communities"]:
        print(f"  {comm['community_id']} L{comm['level']} n={comm['size']} {comm['title']}")
