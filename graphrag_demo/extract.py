"""M2 - Entity, alias, claim and relationship extraction.

One model call per knowledge-base section. Each call turns a section's
text units into:

* **entities**   - the things the section is about (a service, a fee, a
  policy, a facility), each with the words real customers use for it.
* **claims**     - atomic facts, each quoting the words that support it.
* **relationships** - how those entities connect to each other.

## The alias list is the point

Test **C03** in the test set is the "synonym trap": customers say
"maintenance" and "polish", and neither word appears anywhere in
`faq_kb.md`. Lexical retrieval cannot connect them. An alias list can:

    "Basic servicing package"  aliases: ["maintenance", "servicing", "tune-up"]
    "Exterior detailing"       aliases: ["polish", "wax", "ceramic coating"]

So the prompt explicitly asks for aliases *that do not appear in the
text* - the words customers bring with them.

## Grounding

Every claim must quote its source verbatim. Claims whose quote is not
found in the unit they cite are marked `grounded: false` and excluded
from the citation path downstream. This is checked in code, not trusted
from the model.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import config
from .corpus import load_corpus
from .llm import LLM, LLMError

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
                "required": ["name", "type", "aliases", "summary"],
                "additionalProperties": False,
            },
        },
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "unit_id": {"type": "string"},
                    "text": {"type": "string"},
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "unit_id",
                    "text",
                    "subject",
                    "predicate",
                    "object",
                    "entities",
                ],
                "additionalProperties": False,
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "relation": {"type": "string"},
                    "unit_id": {"type": "string"},
                },
                "required": ["source", "target", "relation", "unit_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["entities", "claims", "relationships"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are a knowledge-graph extraction engine for a car workshop's customer
knowledge base. You are given one section of the knowledge base, already
split into numbered text units. You return structured facts about that
section.

Extract:

ENTITIES
- The things the section is about: services, prices, fees, policies,
  facilities, locations, time periods, documents, payment methods.
- type: one of service | price | fee | policy | facility | location |
  time | document | payment_method | vehicle_type | other
- name: the business's own wording for the entity. Use the same wording
  every time, so "Basic servicing package" never becomes "basic service".
- aliases: OTHER names for the same entity. Crucially, include the words
  real CUSTOMERS use for it even when those words do NOT appear in the
  text. For example "maintenance", "servicing" and "tune-up" for a
  servicing package; "polish", "wax" or "ceramic coating" for detailing;
  "valet" for a courtesy car. Only include an alias a customer would
  plausibly use to mean this entity. Do not repeat the name as an alias.
- summary: one sentence on what this entity is.

CLAIMS
- One atomic fact per claim: a subject, a predicate and an object.
  "Basic servicing package", "costs", "SGD 128".
- text: a VERBATIM excerpt from the unit that supports the claim. Copy the
  exact words from the text, do not paraphrase and do not add anything.
- unit_id: the id of the unit the excerpt came from.
- entities: the names of the entities the claim mentions.

RELATIONSHIPS
- How two entities in this section relate: source, target and a short
  relation phrase. Examples: "Basic servicing package" --includes-->
  "Engine oil change"; "Full servicing package" --includes-->
  "Basic servicing package"; "Late cancellation fee" --applies when-->
  "Cancel less than 24 hours before the appointment".

Rules:
- Extract only what the section actually says. Do not infer, do not add
  outside knowledge, do not invent numbers.
- A section with nothing to extract returns empty arrays.
- Prefer several specific claims over one vague claim.
"""


def _normalise(text: str) -> str:
    """Lowercase and collapse whitespace, so quotes can be compared."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _is_grounded(quote: str, unit_text: str) -> bool:
    """Is `quote` really present in `unit_text`?

    Compared on normalised whitespace, and with a little tolerance for
    the smart quotes and dashes models sometimes reproduce.
    """
    def clean(value: str) -> str:
        value = value.replace("’", "'").replace("‘", "'")
        value = value.replace("“", '"').replace("”", '"')
        value = value.replace("–", "-").replace("—", "-")
        return _normalise(value)

    quote_clean = clean(quote)
    if not quote_clean:
        return False
    if quote_clean in clean(unit_text):
        return True

    # Tolerate a quote that dropped the trailing punctuation or a trailing
    # character (models sometimes truncate a sentence).
    if len(quote_clean) > 40 and quote_clean[:-1] in clean(unit_text):
        return True
    return False


def _section_prompt(section: dict, units: list[dict]) -> str:
    """Render one section as a prompt."""
    body = []
    for unit in units:
        body.append(f"[{unit['unit_id']}]\n{unit['text']}")
    listing = "\n\n".join(body)

    return (
        f"Knowledge base section: {section['title']}\n"
        f"section_id: {section['section_id']}\n\n"
        f"Text units from this section:\n\n{listing}\n\n"
        f"Extract the entities, claims and relationships for this section. "
        f"Every claim's unit_id must be one of the ids above."
    )


def _extract_section(
    llm: LLM, section: dict, units: list[dict]
) -> dict:
    """One model call for one section."""
    prompt = _section_prompt(section, units)
    data = llm.structured(
        system=SYSTEM_PROMPT,
        prompt=prompt,
        schema=EXTRACTION_SCHEMA,
        label=f"extract:{section['section_id']}",
    )

    units_by_id = {u["unit_id"]: u for u in units}
    valid_ids = set(units_by_id)

    # Ground the claims against the source, in code.
    grounded_claims = []
    dropped = 0
    for claim in data.get("claims", []):
        unit_id = claim.get("unit_id", "")
        if unit_id not in valid_ids:
            # The model cited a unit from another section. Keep the claim
            # but mark it: it must not produce a citation.
            claim["grounded"] = False
            claim["grounding_note"] = "unknown unit_id"
            dropped += 1
            grounded_claims.append(claim)
            continue

        is_grounded = _is_grounded(claim.get("text", ""), units_by_id[unit_id]["text"])
        claim["grounded"] = is_grounded
        if not is_grounded:
            claim["grounding_note"] = "quote not found in the cited unit"
            dropped += 1
        grounded_claims.append(claim)

    relationships = []
    for rel in data.get("relationships", []):
        rel["grounded"] = rel.get("unit_id", "") in valid_ids
        relationships.append(rel)

    return {
        "section_id": section["section_id"],
        "section_title": section["title"],
        "entities": data.get("entities", []),
        "claims": grounded_claims,
        "relationships": relationships,
        "stats": {
            "entities": len(data.get("entities", [])),
            "claims": len(grounded_claims),
            "relationships": len(relationships),
            "ungrounded": dropped,
        },
    }


def run_extraction(
    limit: int = 0, force: bool = False, llm: LLM | None = None
) -> dict:
    """Extract from every section, caching the result on disk.

    `limit` caps the number of sections processed (0 = all), which is
    useful for a fast smoke test.
    """
    corpus = load_corpus()
    sections = corpus["sections"]
    if limit:
        sections = sections[:limit]

    units_by_section: dict[str, list[dict]] = {}
    for unit in corpus["units"]:
        units_by_section.setdefault(unit["section_id"], []).append(unit)

    cache_path = config.EXTRACTED_PATH
    cached: dict[str, dict] = {}
    if cache_path.exists() and not force:
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            cached = {r["section_id"]: r for r in raw.get("sections", [])}
        except (json.JSONDecodeError, OSError, KeyError):
            cached = {}

    todo = [s for s in sections if force or s["section_id"] not in cached]
    if not todo:
        print("everything cached - nothing to extract (use --force to redo)")
    else:
        own_llm = llm is None
        llm = llm or LLM()
        caps = llm.capabilities
        print(
            f"endpoint: {caps.served_model or 'unknown'} "
            f"(structured output: {'yes' if caps.supports_structured_output else 'no, using prompt mode'})"
        )
        print(f"extracting {len(todo)} section(s), {len(sections) - len(todo)} cached")

        def work(section: dict) -> dict:
            try:
                return _extract_section(
                    llm, section, units_by_section.get(section["section_id"], [])
                )
            except LLMError as exc:
                return {
                    "section_id": section["section_id"],
                    "section_title": section["title"],
                    "entities": [],
                    "claims": [],
                    "relationships": [],
                    "stats": {"entities": 0, "claims": 0, "relationships": 0, "ungrounded": 0},
                    "error": f"{type(exc).__name__}: {exc}",
                }

        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(work, todo):
                cached[result["section_id"]] = result
                status = result.get("error") or (
                    f"{result['stats']['entities']} entities, "
                    f"{result['stats']['claims']} claims, "
                    f"{result['stats']['relationships']} rels"
                )
                print(f"  {result['section_id']:<32} {status}")

        if own_llm:
            print(f"usage: {llm.usage.to_dict()}")
            print(f"cost:  {llm.usage.format_cost()}")

    # Keep only the sections we have, in corpus order.
    ordered = [
        cached[s["section_id"]]
        for s in corpus["sections"]
        if s["section_id"] in cached
    ]

    totals = {
        "entities": sum(r["stats"]["entities"] for r in ordered),
        "claims": sum(r["stats"]["claims"] for r in ordered),
        "relationships": sum(r["stats"]["relationships"] for r in ordered),
        "ungrounded": sum(r["stats"]["ungrounded"] for r in ordered),
        "errors": sum(1 for r in ordered if r.get("error")),
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "sections": ordered,
    }
    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # A flat claims file is what M3 and M5 actually read.
    with config.CLAIMS_PATH.open("w", encoding="utf-8") as handle:
        for section in ordered:
            for claim in section["claims"]:
                handle.write(
                    json.dumps(
                        {**claim, "section_id": section["section_id"]},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    payload["usage"] = llm.usage.to_dict() if llm else None
    payload["cost"] = llm.usage.format_cost() if llm else None
    return payload
