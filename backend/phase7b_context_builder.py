"""
Phase 7B - deterministic Evidence -> LLM Context builder.

This layer prepares a grounded, inspectable payload for a later LLM call.
It does NOT call Gemini, Neo4j, or ingestion and it does NOT invent facts.

Input:
    Phase 7A evidence object produced by build_evidence().

Output:
    A generic LLM-ready context contract containing:
      - user query / intent / graph parameters
      - deterministic graph summary
      - all evidence facts, preserving provenance
      - strict grounding instructions for the future LLM
      - a serialized context_text for a prompt
"""

from __future__ import annotations

import json
from typing import Any


class ContextBuildError(ValueError):
    """Raised when Phase 7A evidence cannot safely become 7B context."""


GROUNDING_INSTRUCTIONS = [
    "Use only facts present in the supplied evidence context.",
    "Do not invent entities, relationships, measurements, mechanisms, efficacy, or citations.",
    "Do not infer a stronger biological or clinical claim than the graph explicitly represents.",
    "Describe graph associations as associations; do not turn them into treatment or causation claims.",
    "Preserve measurement values, units, relation operators, and confidence tiers exactly as supplied when mentioned.",
    "When evidence is absent or insufficient, say that the supplied knowledge-graph evidence does not establish it.",
    "Do not claim to have read a paper or source document unless its actual content is supplied in the evidence context.",
    "Keep provenance references available so every factual statement can be traced to graph rows/nodes/relationships.",
]


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContextBuildError(f"{path} must be an object.")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContextBuildError(f"{path} must be an array.")
    return value


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextBuildError(f"{path} must be a non-empty string.")
    return value


def _validate_evidence_shape(evidence: dict[str, Any]) -> None:
    _require_dict(evidence.get("query_context"), "evidence.query_context")
    summary = _require_dict(evidence.get("summary"), "evidence.summary")
    facts = _require_list(evidence.get("facts"), "evidence.facts")
    provenance = _require_dict(evidence.get("provenance"), "evidence.provenance")

    _nonempty_string(evidence["query_context"].get("intent"), "evidence.query_context.intent")
    _require_dict(evidence["query_context"].get("params") or {}, "evidence.query_context.params")

    if summary.get("fact_count") != len(facts):
        raise ContextBuildError(
            f"evidence.summary.fact_count={summary.get('fact_count')} != len(evidence.facts)={len(facts)}."
        )

    for index, fact in enumerate(facts):
        fact_obj = _require_dict(fact, f"evidence.facts[{index}]")
        if fact_obj.get("row_index") != index:
            raise ContextBuildError(
                f"evidence.facts[{index}].row_index={fact_obj.get('row_index')} does not match position {index}."
            )
        _require_dict(fact_obj.get("path"), f"evidence.facts[{index}].path")
        _require_list(fact_obj["path"].get("nodes"), f"evidence.facts[{index}].path.nodes")
        _require_list(
            fact_obj["path"].get("relationships"),
            f"evidence.facts[{index}].path.relationships",
        )
        _require_dict(fact_obj.get("entities"), f"evidence.facts[{index}].entities")
        _require_list(fact_obj.get("measurements"), f"evidence.facts[{index}].measurements")
        _require_dict(fact_obj.get("provenance"), f"evidence.facts[{index}].provenance")

    # Ensure this stays serializable before it is ever passed to an LLM client.
    try:
        json.dumps(evidence, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ContextBuildError(f"Evidence is not JSON serializable: {exc}") from exc


def _fact_line(fact: dict[str, Any]) -> str:
    path = fact["path"]
    nodes = path["nodes"]
    relationships = path["relationships"]

    node_text = []
    for node in nodes:
        node_type = node.get("type", "unknown")
        name = node.get("name") or node.get("id")
        node_text.append(f"{node_type}={name}")

    rel_text = []
    for rel in relationships:
        rel_text.append(
            f"{rel.get('source')} -[{rel.get('type')}]-> {rel.get('target')}"
        )

    measurements = fact.get("measurements") or []
    measurement_text = ""
    if measurements:
        measurement_text = " measurements=" + json.dumps(
            measurements, ensure_ascii=False, sort_keys=True
        )

    provenance = fact.get("provenance") or {}
    return (
        f"row_index={fact.get('row_index')}; "
        f"path={' | '.join(node_text)}; "
        f"relationships={' ; '.join(rel_text)};"
        f"{measurement_text} "
        f"provenance={json.dumps(provenance, ensure_ascii=False, sort_keys=True)}"
    )


def _plant_overview_compact_context(
    *,
    user_query: str,
    query_context: dict[str, Any],
    summary: dict[str, Any],
    facts: list[dict[str, Any]],
) -> str:
    """Build a compact deterministic context for large plant-overview graphs.

    The complete evidence remains in ``llm_context["facts"]``. This function
    only changes the serialized text sent to the answer model so a highly
    connected plant cannot create an oversized Gemini request.
    """
    plant_names: set[str] = set()
    plant_part_fact_counts: dict[str, int] = {}
    plant_part_uses: dict[str, set[str]] = {}
    phytochemicals: set[str] = set()
    targets: set[str] = set()
    bioactivities: set[str] = set()
    documents: set[str] = set()

    for fact in facts:
        entities = fact.get("entities") or {}
        if not isinstance(entities, dict):
            continue

        def clean(value: Any) -> str | None:
            if value is None:
                return None
            text = str(value).strip()
            return text if text else None

        plant = clean(entities.get("plant"))
        part = clean(entities.get("plant_part"))
        use = clean(entities.get("therapeutic_use"))
        phytochemical = clean(entities.get("phytochemical"))
        target = clean(entities.get("target"))
        bioactivity = clean(entities.get("bioactivity"))
        document = clean(entities.get("document"))

        if plant:
            plant_names.add(plant)
        if part:
            plant_part_fact_counts[part] = plant_part_fact_counts.get(part, 0) + 1
            if use:
                plant_part_uses.setdefault(part, set()).add(use)
        if phytochemical:
            phytochemicals.add(phytochemical)
        if target:
            targets.add(target)
        if bioactivity:
            bioactivities.add(bioactivity)
        if document:
            documents.add(document)

    part_cards = []
    for part in sorted(plant_part_fact_counts, key=str.casefold):
        uses = sorted(plant_part_uses.get(part, set()), key=str.casefold)
        part_cards.append({
            "plant_part": part,
            "fact_count": plant_part_fact_counts[part],
            "unique_associated_therapeutic_uses": len(uses),
            "associated_therapeutic_uses": uses,
        })

    compact = {
        "context_mode": "compact_plant_overview",
        "note": (
            "The graph/evidence payload remains complete in llm_context.facts. "
            "This text is a deterministic overview representation for the answer model; "
            "it is not a replacement for the stored evidence."
        ),
        "plants": sorted(plant_names, key=str.casefold),
        "node_counts_by_type": summary.get("node_counts_by_type", {}),
        "relationship_counts_by_type": summary.get("relationship_counts_by_type", {}),
        "bioactivity_counts_by_standard_type": summary.get(
            "bioactivity_counts_by_standard_type", {}
        ),
        "bioactivity_counts_by_confidence": summary.get(
            "bioactivity_counts_by_confidence", {}
        ),
        "unique_entity_counts_from_facts": {
            "phytochemical": len(phytochemicals),
            "bioactivity": len(bioactivities),
            "target": len(targets),
            "document": len(documents),
        },
        "plant_part_cards": part_cards,
    }

    lines = [
        "GROUNDING POLICY",
        *[f"- {instruction}" for instruction in GROUNDING_INSTRUCTIONS],
        "",
        "USER QUERY",
        user_query,
        "",
        "QUERY CONTEXT",
        json.dumps(query_context, ensure_ascii=False, sort_keys=True),
        "",
        "DETERMINISTIC PLANT OVERVIEW SUMMARY",
        json.dumps(compact, ensure_ascii=False, sort_keys=True, indent=2),
        "",
        "GROUNDING NOTE",
        "Use the exact plant-part and therapeutic-use names in the overview cards. "
        "Do not invent additional associations. Backend validation retains the "
        "complete evidence facts separately.",
    ]
    return "\n".join(lines)


def _build_context_text(
    *,
    user_query: str,
    query_context: dict[str, Any],
    summary: dict[str, Any],
    facts: list[dict[str, Any]],
) -> str:
    # Plant-overview queries can legitimately span thousands of graph facts.
    # Keep the full facts in the contract, but hand Gemini a compact deterministic
    # representation rather than serializing thousands of repeated paths.
    if str(query_context.get("intent", "")) == "semantic:info:plant_overview":
        return _plant_overview_compact_context(
            user_query=user_query,
            query_context=query_context,
            summary=summary,
            facts=facts,
        )

    lines = [
        "GROUNDING POLICY",
        *[f"- {instruction}" for instruction in GROUNDING_INSTRUCTIONS],
        "",
        "USER QUERY",
        user_query,
        "",
        "QUERY CONTEXT",
        json.dumps(query_context, ensure_ascii=False, sort_keys=True),
        "",
        "DETERMINISTIC SUMMARY",
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        "",
        "EVIDENCE FACTS",
    ]
    lines.extend(f"- {_fact_line(fact)}" for fact in facts)
    return "\n".join(lines)


def build_llm_context(
    evidence: Any,
    *,
    user_query: str | None = None,
) -> dict[str, Any]:
    """
    Build the Phase 7B context contract from Phase 7A evidence.

    The full evidence fact list is retained. No top-k truncation, ranking,
    semantic filtering, or LLM-generated summary occurs here.
    """
    evidence_obj = _require_dict(evidence, "evidence")
    _validate_evidence_shape(evidence_obj)

    query_context = dict(evidence_obj["query_context"])
    summary = dict(evidence_obj["summary"])
    facts = list(evidence_obj["facts"])
    provenance = dict(evidence_obj["provenance"])

    query_text = user_query
    if query_text is None:
        query_text = query_context.get("user_query")
    if query_text is None:
        # Phase 7A intentionally does not require user query text, so allow
        # callers to omit it while making the missing value explicit.
        query_text = ""
    if not isinstance(query_text, str):
        raise ContextBuildError("user_query must be a string when supplied.")

    context_text = _build_context_text(
        user_query=query_text,
        query_context=query_context,
        summary=summary,
        facts=facts,
    )

    output = {
        "contract_version": "7B-1",
        "user_query": query_text,
        "query_context": query_context,
        "grounding_instructions": list(GROUNDING_INSTRUCTIONS),
        "summary": summary,
        "facts": facts,
        "provenance": provenance,
        "context_stats": {
            "fact_count": len(facts),
            "context_char_count": len(context_text),
            "context_utf8_bytes": len(context_text.encode("utf-8")),
        },
        "context_text": context_text,
    }

    try:
        json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ContextBuildError(f"Phase 7B context is not JSON serializable: {exc}") from exc

    return output


# ---------- standalone test fixtures ----------


def _minimal_evidence() -> dict[str, Any]:
    fact = {
        "row_index": 0,
        "path": {
            "nodes": [
                {"id": "P1", "type": "plant", "label": "Plant Name", "name": "Mangifera indica", "properties": {"scientific_name": "Mangifera indica"}},
                {"id": "TU1", "type": "therapeutic_use", "label": "Therapeutic Uses", "name": "Diabetes", "properties": {"therapeutic_use": "Diabetes", "mesh_id": "D003920"}},
            ],
            "relationships": [
                {"id": "R1", "type": "Used For", "source": "P1", "target": "TU1", "properties": {}},
            ],
            "direction_check": "positional",
        },
        "entities": {"plant": "Mangifera indica", "therapeutic_use": "Diabetes"},
        "measurements": [],
        "provenance": {"node_ids": ["P1", "TU1"], "relationship_ids": ["R1"], "graph_row_index": 0},
    }
    return {
        "query_context": {"intent": "plant_therapeutic_uses", "params": {"plant": "Mangifera indica", "limit": 5000}},
        "summary": {"row_count": 1, "node_count": 2, "relationship_count": 1, "node_counts_by_type": {"plant": 1, "therapeutic_use": 1}, "relationship_counts_by_type": {"Used For": 1}, "bioactivity_counts_by_standard_type": {}, "bioactivity_counts_by_confidence": {}, "subject_candidates": [{"type": "plant", "id": "P1", "name": "Mangifera indica"}], "fact_count": 1},
        "facts": [fact],
        "provenance": {"source": "phase6_graph_response", "graph_intent": "plant_therapeutic_uses", "node_ids": ["P1", "TU1"], "relationship_ids": ["R1"]},
    }


def _offline_test() -> None:
    evidence = _minimal_evidence()
    context = build_llm_context(evidence, user_query="Tell me the use of mango")
    assert context["contract_version"] == "7B-1"
    assert context["user_query"] == "Tell me the use of mango"
    assert context["summary"]["fact_count"] == 1
    assert len(context["facts"]) == 1
    assert "Mangifera indica" in context["context_text"]
    assert "Diabetes" in context["context_text"]
    assert "do not infer" in context["context_text"].lower()
    assert context["context_stats"]["fact_count"] == 1
    print("Evidence -> LLM context: PASS")

    # Large plant overview must retain all facts while keeping context_text bounded.
    large_fact = {
        "row_index": 0,
        "path": {
            "nodes": [],
            "relationships": [],
        },
        "entities": {
            "plant": "Azadirachta indica",
            "plant_part": "leaf",
            "therapeutic_use": "Anti-Bacterial Agents",
        },
        "measurements": [],
        "provenance": {"graph_row_index": 0},
    }
    large_facts = []
    for i in range(6137):
        fact = json.loads(json.dumps(large_fact))
        fact["row_index"] = i
        large_facts.append(fact)
    large_evidence = {
        "query_context": {
            "intent": "semantic:info:plant_overview",
            "params": {"plant_scientific_name": "Azadirachta indica"},
        },
        "summary": {
            "row_count": 6137,
            "fact_count": 6137,
            "node_count": 1327,
            "relationship_count": 3536,
            "node_counts_by_type": {
                "plant": 1,
                "plant_part": 10,
                "phytochemical": 183,
                "therapeutic_use": 132,
                "bioactivity": 593,
                "target": 219,
                "document": 189,
            },
            "relationship_counts_by_type": {
                "Has Part": 10,
                "Contains": 241,
                "Used For": 309,
                "Has Bioactivity": 808,
                "Measured On": 1135,
                "Reported In": 1033,
            },
            "bioactivity_counts_by_standard_type": {"Ki": 109, "IC50": 190, "Potency": 245, "EC50": 29, "Kd": 20},
            "bioactivity_counts_by_confidence": {"HIGH": 185, "MEDIUM": 408},
        },
        "facts": large_facts,
        "provenance": {"source": "phase6_graph_response"},
    }
    large_context = build_llm_context(
        large_evidence,
        user_query="Tell me about neem",
    )
    assert len(large_context["facts"]) == 6137
    assert large_context["context_stats"]["context_char_count"] < 100000
    assert "Anti-Bacterial Agents" in large_context["context_text"]
    print("Large plant overview compaction: PASS")

    bad = json.loads(json.dumps(evidence))
    bad["summary"]["fact_count"] = 2
    try:
        build_llm_context(bad, user_query="x")
    except ContextBuildError:
        print("Malformed evidence detection: PASS")
    else:
        raise AssertionError("Malformed evidence was not rejected.")

    bad = json.loads(json.dumps(evidence))
    bad["facts"][0]["row_index"] = 7
    try:
        build_llm_context(bad, user_query="x")
    except ContextBuildError:
        print("Fact ordering detection: PASS")
    else:
        raise AssertionError("Fact ordering defect was not rejected.")

    print("PHASE 7B CONTEXT BUILDER TEST: PASS")


if __name__ == "__main__":
    _offline_test()
