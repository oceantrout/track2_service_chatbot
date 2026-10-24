"""M2 (alternative engine) - deterministic, rules-based extraction.

Same output format as `extract.py`, but no model is involved. It exists
for three reasons:

* the demo is fully testable with no API key and no cost;
* it is a **baseline**. The rules engine is deliberately crude, so
  comparing it against the LLM engine shows what the model actually buys;
* it makes the pipeline robust - if the endpoint is down, the graph still
  builds.

What it does:

* finds entities by matching a curated domain gazetteer plus capitalised
  phrases, so `Basic servicing package` and `SGD 128` become entities;
* finds claims by splitting each unit into sentences and attaching the
  primary entity;
* finds relationships by co-occurrence within a sentence;
* attaches **aliases** from a curated synonym table.

## The alias table is where C03 is solved

    "maintenance" -> Basic servicing package
    "polish"      -> Exterior detailing

Neither word appears anywhere in `faq_kb.md`, so no lexical method can
bridge the gap. The table is what does it.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone

from . import config
from .corpus import load_corpus

# ---------------------------------------------------------------------------
# Gazetteers
# ---------------------------------------------------------------------------

# Canonical entity -> the words customers actually use for it.
# These are the aliases a lexical retriever cannot invent for itself.
ALIAS_GAZETTEER: dict[str, list[str]] = {
    "Basic servicing package": [
        "maintenance",
        "basic maintenance",
        "routine service",
        "basic service",
        "servicing",
        "tune up",
        "tune-up",
        "oil change",
    ],
    "Full servicing package": [
        "full service",
        "full maintenance",
        "comprehensive service",
        "major service",
        "tune up",
        "tune-up",
        "big service",
    ],
    "EV and hybrid health check": [
        "ev check",
        "electric car check",
        "hybrid check",
        "battery check",
        "ev service",
        "electric servicing",
    ],
    "Tyre rotation and balancing": [
        "tyre rotation",
        "tire rotation",
        "wheel rotation",
        "tyre balance",
        "balancing",
        "rotate tyres",
    ],
    "Aircon servicing": [
        "aircon service",
        "air conditioning service",
        "ac servicing",
        "aircon",
    ],
    "Brake pad replacement (front)": [
        "brake pads",
        "brake pad replacement",
        "brake service",
        "new brake pads",
    ],
    "Interior detailing": [
        "interior detail",
        "inside detailing",
        "cabin detailing",
        "interior cleaning",
        "upholstery cleaning",
    ],
    "Exterior detailing": [
        "car polish",
        "polish",
        "exterior polish",
        "wax",
        "ceramic coating",
        "paint correction",
        "swirl mark removal",
        "body detailing",
    ],
    "Courtesy car": [
        "courtesy vehicle",
        "loan car",
        "replacement car",
        "spare car",
        "valet car",
    ],
    "Late cancellation fee": [
        "late cancellation",
        "late cancel fee",
        "late cancellation charge",
    ],
    "No-show fee": [
        "no show fee",
        "noshow fee",
        "missed appointment fee",
    ],
    "Diagnostic fee": [
        "diagnostic charge",
        "warning light fee",
        "fault finding fee",
    ],
    "PayNow": ["pay now", "paynow transfer", "paynow payment"],
    "NETS": ["nets payment", "nets card"],
    "Vehicle collection after closing": [
        "overnight storage",
        "collect next day",
        "after hours collection",
        "leaving my car overnight",
    ],
    "Customer parking": [
        "parking",
        "car park",
        "where to park",
        "parking lot",
    ],
    "Workmanship warranty": [
        "warranty",
        "service warranty",
        "guarantee",
    ],
    "Protective coating guarantee": [
        "coating guarantee",
        "coating warranty",
    ],
}

# Entities that are always worth having, even when phrased differently.
PAYMENT_METHODS = [
    "PayNow",
    "NETS",
    "Visa",
    "Mastercard",
    "American Express",
    "corporate account",
    "credit card",
]

# Words that start a sentence and would otherwise look like entities.
CAPITALISED_STOPWORDS = {
    "we", "our", "if", "the", "a", "an", "this", "that", "it", "you", "your",
    "on", "in", "at", "to", "for", "and", "but", "or", "when", "after",
    "before", "during", "outside", "inside", "please", "do", "does", "are",
    "is", "as", "by", "from", "all", "any", "no", "not", "yes", "customers",
    "customer", "cars", "car", "our", "us", "they", "them", "their", "there",
    "then", "so", "only", "also", "there", "here", "what", "which", "who",
    "how", "can", "will", "would", "should", "may", "must", "have", "has",
    "had", "was", "were", "be", "been", "being", "other", "another", "each",
    "every", "both", "more", "most", "some", "such", "than", "too", "very",
}

ENTITY_TYPE_RULES = [
    (r"\bPayNow\b|\bNETS\b|\bVisa\b|\bMastercard\b|\bAmerican Express\b|\bcash\b|\bcheque\b|\bcredit card\b|\bpayment", "payment_method"),
    (r"\bEV\b|\belectric\b|\bhybrid\b|\bpetrol\b|\bdiesel\b|\bSUV\b|\bcontinental\b|\bvehicle", "vehicle_type"),
    (r"\bcourtesy car\b|\bparking\b|\bbay\b|\bworkshop\b|\bwaiting area\b|\bcharger\b|\bcustomer lots?\b|\bcollection\b|\bwaiting\b|\blot\b", "facility"),
    (r"\bfee\b|\bfees\b|\bcharge\b|\bprice\b|\bcost\b|\bdeposit\b|\bSGD\b|\bno-show\b", "fee"),
    (r"\bservic|\bpackage\b|\bhealth check\b|\bdetailing\b|\brotation\b|\breplacement\b|\bbalancing\b", "service"),
    (r"\bwarranty\b|\bguarantee\b|\bpolicy\b|\brefund\b|\bterms\b|\bcoverage\b|\bwe do not\b|\bcannot\b|\bmust\b|\breschedul|\bcancell|\bappointment\b|\bbooking\b|\bslot\b|\bno-show\b", "policy"),
    (r"\bhours?\b|\bMonday\b|\bTuesday\b|\bWednesday\b|\bThursday\b|\bFriday\b|\bSaturday\b|\bSunday\b|\bam\b|\bpm\b|\bweekend\b|\bholiday\b|\bovernight\b|\bopen\b|\bclose\b", "time"),
    (r"\bbooklet\b|\bkey\b|\binvoice\b|\bemail\b|\blicence\b|\bID\b|\bcard\b|\bprotection\b|\bPDPA\b|\bprivacy\b|\bdata\b|\bnotice\b|\bbring\b|\bdrop[- ]off\b|\bdrop off\b", "document"),
    (r"\bWhere\b|\bLocation\b|\bBlock\b|\bWay\b|\bMRT\b|\baddress\b", "location"),
]

PRICE_RE = re.compile(r"SGD\s?\d+(?:\.\d+)?", re.IGNORECASE)
HOURS_RE = re.compile(r"\b\d+(?:\.\d+)?\s*-?\s*hour(?:s)?\b", re.IGNORECASE)
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s?(?:am|pm)\b", re.IGNORECASE)
PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%", re.IGNORECASE)

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _entity_type(name: str) -> str:
    for pattern, kind in ENTITY_TYPE_RULES:
        if re.search(pattern, name, re.IGNORECASE):
            return kind
    return "other"


def _canonicalise(name: str) -> str:
    """Map a surface form onto its gazetteer canonical name, if any."""
    lowered = _normalise(name)
    for canonical in ALIAS_GAZETTEER:
        if lowered == _normalise(canonical):
            return canonical
    return name.strip()


def _load_service_names() -> list[str]:
    """Service names from services.csv, so they are always exact entities."""
    try:
        with config.SERVICES_PATH.open(encoding="utf-8", newline="") as handle:
            return [row["name"] for row in csv.DictReader(handle)]
    except OSError:
        return []


def _find_entities(text: str, service_names: list[str]) -> list[dict]:
    """Find the entities mentioned in one piece of text."""
    found: dict[str, dict] = {}

    def add(name: str, kind: str | None = None, summary: str = "") -> None:
        canonical = _canonicalise(name)
        if not canonical or len(canonical) < 3:
            return
        if canonical not in found:
            found[canonical] = {
                "name": canonical,
                "type": kind or _entity_type(canonical),
                "aliases": list(ALIAS_GAZETTEER.get(canonical, [])),
                "summary": summary,
            }

    lowered = text.lower()

    # 1. exact service names
    for service in service_names:
        if service.lower() in lowered:
            add(service, "service", f"A service offered by the workshop.")

    # 2. gazetteer canonical names
    for canonical in ALIAS_GAZETTEER:
        if _normalise(canonical) in lowered:
            add(canonical)

    # 3. gazetteer aliases that appear in the text - these are how a
    #    customer's word gets mapped back to the canonical entity
    for canonical, aliases in ALIAS_GAZETTEER.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias.lower())}\b", lowered):
                add(canonical)

    # 4. payment methods
    for method in PAYMENT_METHODS:
        if re.search(rf"\b{re.escape(method.lower())}\b", lowered):
            add(method, "payment_method", f"A payment method the workshop accepts.")

    # 5. capitalised multi-word phrases
    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
        phrase = match.group(1).strip()
        first = phrase.split()[0].lower()
        if first in CAPITALISED_STOPWORDS:
            continue
        # "Mastercard and American Express" is two payment methods, not one
        # entity - a phrase joining two things with and/or is not a thing.
        if re.search(r"\b(and|or)\b", phrase):
            continue
        add(phrase)

    return list(found.values())


def _make_claims(unit: dict, entities: list[dict]) -> list[dict]:
    """Split a unit into sentence-level claims."""
    claims = []
    sentences = [s.strip() for s in SENTENCE_SPLIT_RE.split(unit["text"]) if s.strip()]
    if not sentences:
        sentences = [unit["text"]]

    for sentence in sentences:
        mentioned = [
            e["name"]
            for e in entities
            if _normalise(e["name"]) in _normalise(sentence)
            or any(
                re.search(rf"\b{re.escape(alias.lower())}\b", sentence.lower())
                for alias in e["aliases"]
            )
        ]
        subject = mentioned[0] if mentioned else unit["section_title"]

        # Numeric facts get a real predicate; prose keeps its sentence.
        price = PRICE_RE.search(sentence)
        hours = HOURS_RE.search(sentence)
        percent = PERCENT_RE.search(sentence)
        times = TIME_RE.findall(sentence)

        if price and subject not in mentioned[:1]:
            predicate, obj = "costs", price.group(0)
        elif price:
            predicate, obj = "costs", price.group(0)
        elif percent:
            predicate, obj = "includes a rate of", percent.group(0)
        elif hours:
            predicate, obj = "takes", hours.group(0)
        elif times:
            predicate, obj = "happens at", ", ".join(times)
        else:
            predicate, obj = "states", sentence

        claims.append(
            {
                "unit_id": unit["unit_id"],
                "text": sentence,
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "entities": mentioned,
                "grounded": True,  # rules engine quotes the source verbatim
                "grounding_note": "",
            }
        )
    return claims


def _make_relationships(
    claims: list[dict], entities_by_unit: dict[str, list[dict]]
) -> list[dict]:
    """Co-occurrence relationships: entities in the same sentence are linked."""
    relationships = []
    for claim in claims:
        mentioned = entities_by_unit.get(claim["unit_id"], [])
        names = [e["name"] for e in mentioned]
        for i, source in enumerate(names):
            for target in names[i + 1 :]:
                relationships.append(
                    {
                        "source": source,
                        "target": target,
                        "relation": "is mentioned alongside",
                        "unit_id": claim["unit_id"],
                        "grounded": True,
                    }
                )
    return relationships


def run_extraction_rules(limit: int = 0) -> dict:
    """Run the rules engine and write the M2 artifact."""
    corpus = load_corpus()
    sections = corpus["sections"]
    if limit:
        sections = sections[:limit]

    service_names = _load_service_names()

    section_results = []
    for section in sections:
        units = [u for u in corpus["units"] if u["section_id"] == section["section_id"]]
        if not units:
            continue

        entities: dict[str, dict] = {}
        claims: list[dict] = []
        entities_by_unit: dict[str, list[dict]] = {}

        # The section's own topic is an entity. Without it, sections whose
        # text is all lowercase ("Opening hours", "What to bring") contribute
        # no node at all and drop out of the graph.
        title = section["title"]
        entities[title] = {
            "name": title,
            "type": _entity_type(title),
            "aliases": list(ALIAS_GAZETTEER.get(title, [])),
            "summary": f"The topic of the '{title}' section of the knowledge base.",
            # A section topic is a hub that connects the things its section
            # mentions. It is not the same thing as a content entity, so the
            # graph builder never merges one into another on the strength of
            # an alias alone.
            "is_topic": True,
        }

        for unit in units:
            unit_entities = _find_entities(unit["text"], service_names)
            entities_by_unit[unit["unit_id"]] = unit_entities
            for entity in unit_entities:
                existing = entities.get(entity["name"])
                if existing is None:
                    entities[entity["name"]] = entity
                else:
                    # Merge alias lists.
                    for alias in entity["aliases"]:
                        if alias not in existing["aliases"]:
                            existing["aliases"].append(alias)

            claims.extend(_make_claims(unit, unit_entities))

        relationships = _make_relationships(claims, entities_by_unit)

        section_results.append(
            {
                "section_id": section["section_id"],
                "section_title": section["title"],
                "entities": list(entities.values()),
                "claims": claims,
                "relationships": relationships,
                "stats": {
                    "entities": len(entities),
                    "claims": len(claims),
                    "relationships": len(relationships),
                    "ungrounded": 0,
                },
                "engine": "rules",
            }
        )

    totals = {
        "entities": sum(r["stats"]["entities"] for r in section_results),
        "claims": sum(r["stats"]["claims"] for r in section_results),
        "relationships": sum(r["stats"]["relationships"] for r in section_results),
        "ungrounded": 0,
        "errors": 0,
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": "rules",
        "totals": totals,
        "sections": section_results,
    }
    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    config.EXTRACTED_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    with config.CLAIMS_PATH.open("w", encoding="utf-8") as handle:
        for section in section_results:
            for claim in section["claims"]:
                handle.write(
                    json.dumps(
                        {**claim, "section_id": section["section_id"]},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    return payload
