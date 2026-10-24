"""G3 - Knowledge graph and entity resolution.

Turns the extracted entities, aliases and relationships into one graph
where every real-world thing is a single node.

The problem this solves: the same service is named in four sections
("Basic servicing package" in *Services and prices*, again in *Servicing
packages*, "Basic servicing" in a table). Left alone that is four nodes,
the graph fragments, and community detection finds noise.

So names and aliases are merged with a union-find. Two entities merge
when:

* their normalised names are equal, or
* one's name is an alias of the other, or
* they share an alias.

The merged node keeps every alias, and the aliases become **edges**, so
the question "how much does a maintenance cost" can reach the node
"Basic servicing package" and its claims.

Everything here is deterministic code - the model extracted the raw
material, the code decides what is true.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone

from . import config

# Words that should never distinguish two entities.
GENERIC = {
    "service", "services", "package", "packages", "check", "fee", "fees",
    "policy", "policies", "the", "a", "an", "and", "or", "of", "for",
}


def normalise(name: str) -> str:
    """A comparison key for an entity name.

    Lowercases, drops a parenthetical qualifier ("Brake pad replacement
    (front)" -> "brake pad replacement") and collapses punctuation.
    """
    text = name.lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class UnionFind:
    """Minimal union-find with path compression."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            # Keep the lexicographically smaller root as the representative
            # so the result does not depend on iteration order.
            if root_a <= root_b:
                self.parent[root_b] = root_a
            else:
                self.parent[root_a] = root_b

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            out[self.find(item)].append(item)
        return dict(out)


def _entity_key(entity: dict) -> str:
    """A stable key for one entity occurrence."""
    return f"{entity['name']}::{normalise(entity['name'])}"


def _build_union_find(entities: list[dict]) -> UnionFind:
    """Merge entities that are the same thing."""
    uf = UnionFind()

    # name -> set of entity keys
    by_name: dict[str, set[str]] = defaultdict(set)
    alias_to_keys: dict[str, set[str]] = defaultdict(set)
    topic_keys: set[str] = set()

    for entity in entities:
        key = _entity_key(entity)
        uf.add(key)
        if entity.get("is_topic"):
            topic_keys.add(key)
        by_name[normalise(entity["name"])].add(key)
        for alias in entity.get("aliases", []):
            alias_to_keys[normalise(alias)].add(key)

    # Same normalised name -> same thing. This applies to topics too, so a
    # section titled "Courtesy car" merges with the "Courtesy car" entity.
    for keys in by_name.values():
        first = next(iter(keys))
        for key in keys:
            uf.union(first, key)

    # A section topic is a hub, not a synonym. It merges with a content
    # entity only when the names match exactly (handled above), never
    # because it happens to share an alias - otherwise the section titled
    # "Warranty" would be swallowed by the "Workmanship warranty" entity
    # and stop acting as the hub for its section.
    content_alias_to_keys = {
        alias: keys - topic_keys for alias, keys in alias_to_keys.items()
    }
    content_alias_to_keys = {
        alias: keys for alias, keys in content_alias_to_keys.items() if keys
    }
    content_by_name = {
        name: keys - topic_keys for name, keys in by_name.items()
    }
    content_by_name = {
        name: keys for name, keys in content_by_name.items() if keys
    }

    # An alias that is also another entity's name -> the same thing.
    for alias_norm, alias_keys in content_alias_to_keys.items():
        if alias_norm in content_by_name:
            targets = content_by_name[alias_norm]
            first = next(iter(alias_keys))
            for key in alias_keys:
                uf.union(first, key)
            first_target = next(iter(targets))
            for key in targets:
                uf.union(first_target, key)

    # Two entities sharing an alias are probably the same thing - but only
    # when the alias is specific enough to be evidence, not a generic word.
    for alias_norm, alias_keys in content_alias_to_keys.items():
        if alias_norm in GENERIC or len(alias_norm) < 4:
            continue
        if len(alias_keys) > 1:
            keys = sorted(alias_keys)
            for key in keys[1:]:
                uf.union(keys[0], key)

    return uf


def _choose_canonical(names: list[str]) -> str:
    """The node's display name.

    The longest name wins, because the KB's own wording is the most
    specific ("Basic servicing package" over "Basic"). Ties break
    alphabetically so the result is stable.
    """
    return sorted(names, key=lambda n: (-len(n), n))[0]


def build_graph() -> dict:
    """Build the knowledge graph from the M2 artifact."""
    if not config.EXTRACTED_PATH.exists():
        raise FileNotFoundError(
            f"{config.EXTRACTED_PATH} is missing - run: python -m graphrag_demo.cli extract"
        )

    extracted = json.loads(config.EXTRACTED_PATH.read_text(encoding="utf-8"))

    # ---- gather every entity occurrence and every claim ---------------
    entities: list[dict] = []
    claims: list[dict] = []
    relationships: list[dict] = []

    for section in extracted.get("sections", []):
        for entity in section.get("entities", []):
            entities.append({**entity, "section_id": section["section_id"]})
        for claim in section.get("claims", []):
            claims.append({**claim, "section_id": section["section_id"]})
        for rel in section.get("relationships", []):
            relationships.append({**rel, "section_id": section["section_id"]})

    # ---- merge ------------------------------------------------------
    uf = _build_union_find(entities)
    groups = uf.groups()

    nodes: dict[str, dict] = {}
    for keys in groups.values():
        occurrences = [e for e in entities if _entity_key(e) in keys]
        if not occurrences:
            continue

        names = sorted({e["name"] for e in occurrences})
        canonical = _choose_canonical(names)
        node_id = f"e{len(nodes) + 1:03d}"

        aliases: list[str] = []
        for entity in occurrences:
            for alias in entity.get("aliases", []):
                if alias.lower() != canonical.lower() and alias not in aliases:
                    aliases.append(alias)
        # Other spellings of the same thing are aliases too.
        for name in names:
            if name != canonical and name not in aliases:
                aliases.append(name)

        types = [e.get("type", "other") for e in occurrences]
        node_type = max(set(types), key=types.count)

        nodes[node_id] = {
            "node_id": node_id,
            "name": canonical,
            "type": node_type,
            "aliases": aliases,
            "summary": next(
                (e.get("summary", "") for e in occurrences if e.get("summary")), ""
            ),
            "section_ids": sorted({e["section_id"] for e in occurrences}),
            "mentions": len(occurrences),
            "is_topic": any(e.get("is_topic") for e in occurrences),
        }

    # Map every alias/name spelling onto its node id. Content entities win
    # over section topics, so a question about "warranty" reaches the
    # Workmanship warranty facts rather than the section hub.
    spelling_to_node: dict[str, str] = {}
    for topics_last in (False, True):
        for node in nodes.values():
            if node["is_topic"] != topics_last:
                continue
            spellings = [node["name"], *node["aliases"]]
            for spelling in spellings:
                key = normalise(spelling)
                if key:
                    spelling_to_node.setdefault(key, node["node_id"])

    def resolve(name: str) -> str | None:
        return spelling_to_node.get(normalise(name))

    # ---- edges -------------------------------------------------------
    edge_counts: dict[tuple[str, str], dict] = {}

    def add_edge(source: str, target: str, kind: str, weight: float = 1.0) -> None:
        if source == target or not source or not target:
            return
        key = (source, target) if source < target else (target, source)
        edge = edge_counts.get(key)
        if edge is None:
            edge = {
                "source": key[0],
                "target": key[1],
                "kind": kind,
                "weight": 0.0,
            }
            edge_counts[key] = edge
        edge["weight"] += weight

    for rel in relationships:
        source = resolve(rel.get("source", ""))
        target = resolve(rel.get("target", ""))
        if source and target:
            add_edge(source, target, "related", 1.0)

    # A claim's entities are related to each other - this is what gives
    # the graph its connective tissue.
    for claim in claims:
        entity_ids = [
            resolve(name) for name in claim.get("entities", [])
        ]
        entity_ids = [e for e in entity_ids if e]
        for i, source in enumerate(entity_ids):
            for target in entity_ids[i + 1 :]:
                add_edge(source, target, "co-claimed", 1.0)

    # Entities that appear in the same cited unit are related too. The unit
    # is the citation granularity, so this stays defensible: if two things
    # are stated in the same cited passage, a reader would meet them
    # together. Without this, entities mentioned one-per-sentence end up
    # isolated and community detection finds nothing.
    entities_by_unit: dict[str, set[str]] = defaultdict(set)
    for claim in claims:
        for name in claim.get("entities", []):
            node_id = resolve(name)
            if node_id:
                entities_by_unit[claim.get("unit_id", "")].add(node_id)
    for node_ids in entities_by_unit.values():
        ordered = sorted(node_ids)
        for i, source in enumerate(ordered):
            for target in ordered[i + 1 :]:
                add_edge(source, target, "co-unit", 1.0)

    # An entity mentioned in a claim is linked to the subject of that claim.
    for claim in claims:
        subject = resolve(claim.get("subject", ""))
        if not subject:
            continue
        for name in claim.get("entities", []):
            target = resolve(name)
            if target and target != subject:
                add_edge(subject, target, "mentions", 1.0)

    # The topic of a section is connected to everything that section is
    # about. Without this, a section whose text names one entity at a time
    # leaves that entity isolated, and community detection has nothing to
    # group. With it, communities land on the sections - which are the
    # themes of the knowledge base.
    section_topics: dict[str, str] = {}
    for section in extracted.get("sections", []):
        topic_id = resolve(section.get("section_title", ""))
        if topic_id:
            section_topics[section["section_id"]] = topic_id

    for entity in entities:
        topic_id = section_topics.get(entity["section_id"])
        if not topic_id:
            continue
        entity_id = resolve(entity["name"])
        if entity_id and entity_id != topic_id:
            add_edge(topic_id, entity_id, "in-section", 1.0)

    edges = list(edge_counts.values())
    for edge in edges:
        edge["weight"] = round(edge["weight"], 3)

    # ---- degrees -----------------------------------------------------
    degree: dict[str, float] = defaultdict(float)
    for edge in edges:
        degree[edge["source"]] += edge["weight"]
        degree[edge["target"]] += edge["weight"]
    for node in nodes.values():
        node["degree"] = round(degree.get(node["node_id"], 0.0), 3)

    # ---- section topic nodes ------------------------------------------
    # Which node is the hub for each section. Resolved by name, not through
    # the spelling map: a topic's own name ("Warranty") may be shadowed by a
    # content entity that claims the word as an alias.
    section_topics: dict[str, str] = {}
    for section in extracted.get("sections", []):
        title = normalise(section.get("section_title", ""))
        if not title:
            continue
        for node in nodes.values():
            if node["is_topic"] and normalise(node["name"]) == title:
                section_topics[section["section_id"]] = node["node_id"]
                break

    # ---- attach claims to nodes ---------------------------------------
    claims_by_node: dict[str, list[str]] = defaultdict(list)
    for index, claim in enumerate(claims):
        for name in claim.get("entities", []) + [claim.get("subject", "")]:
            node_id = resolve(name)
            if node_id:
                claims_by_node[node_id].append(str(index))

    for node in nodes.values():
        node["claim_indices"] = sorted(set(claims_by_node.get(node["node_id"], [])))

    graph = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": extracted.get("engine", "unknown"),
        "nodes": list(nodes.values()),
        "edges": edges,
        "claims": claims,
        "relationships": relationships,
        "spelling_to_node": spelling_to_node,
        "section_topics": section_topics,
        "stats": {
            "entity_occurrences": len(entities),
            "nodes": len(nodes),
            "edges": len(edges),
            "claims": len(claims),
            "grounded_claims": sum(1 for c in claims if c.get("grounded")),
            "merged_away": len(entities) - len(nodes),
        },
    }

    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    config.GRAPH_PATH.write_text(
        json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return graph


def load_graph() -> dict:
    """Load the G3 artifact, building it if it does not exist."""
    if not config.GRAPH_PATH.exists():
        return build_graph()
    return json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    result = build_graph()
    print(json.dumps(result["stats"], indent=2))
