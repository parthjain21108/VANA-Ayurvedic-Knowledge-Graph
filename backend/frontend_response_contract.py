"""Phase 8 frontend response contract / view-model builder.

This layer converts the existing backend response into deterministic UI data.
It does not query Neo4j, call Gemini, change graph data, or discard evidence.

Authoritative sources remain:
    graph      -> nodes / relationships
    evidence   -> facts / provenance
    answer     -> Phase 7C explanation

The frontend model adds stable presentation metadata and cross-references so
interactive UI components can highlight plant parts, uses, graph paths, and
evidence rows without recomputing backend facts. The full graph is not duplicated
here; graph_binding points to the authoritative top-level response.graph object.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any


class FrontendContractError(ValueError):
    """Raised when a backend response cannot safely become a UI view model."""


CONTRACT_VERSION = "8-1"


def _dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FrontendContractError(f"{path} must be an object.")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise FrontendContractError(f"{path} must be an array.")
    return value


def _str(value: Any, path: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise FrontendContractError(f"{path} must be a non-empty string.")
    return value


def _node_name(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "")


def _build_graph_index(graph: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes = _list(graph.get("nodes"), "graph.nodes")
    relationships = _list(graph.get("relationships"), "graph.relationships")

    node_by_id: dict[str, dict[str, Any]] = {}
    rel_by_id: dict[str, dict[str, Any]] = {}

    for node in nodes:
        obj = _dict(node, "graph.nodes[]")
        node_id = _str(obj.get("id"), "graph.nodes[].id")
        if node_id in node_by_id:
            raise FrontendContractError(f"Duplicate graph node id: {node_id!r}")
        node_by_id[node_id] = obj

    for rel in relationships:
        obj = _dict(rel, "graph.relationships[]")
        rel_id = _str(obj.get("id"), "graph.relationships[].id")
        if rel_id in rel_by_id:
            raise FrontendContractError(f"Duplicate graph relationship id: {rel_id!r}")
        source = _str(obj.get("source"), f"graph.relationships[{rel_id}].source")
        target = _str(obj.get("target"), f"graph.relationships[{rel_id}].target")
        if source not in node_by_id or target not in node_by_id:
            raise FrontendContractError(
                f"Graph relationship {rel_id!r} has a dangling endpoint."
            )
        rel_by_id[rel_id] = obj

    return node_by_id, rel_by_id


def _fact_node_id(fact: dict[str, Any], node_type: str) -> str | None:
    path = _dict(fact.get("path"), "evidence.facts[].path")
    for raw in _list(path.get("nodes"), "evidence.facts[].path.nodes"):
        node = _dict(raw, "evidence.facts[].path.nodes[]")
        if str(node.get("type", "")).casefold() == node_type.casefold():
            node_id = node.get("id")
            return str(node_id) if node_id is not None else None
    return None


def _fact_use_node_id(fact: dict[str, Any]) -> str | None:
    return _fact_node_id(fact, "therapeutic_use")


def _fact_part_node_id(fact: dict[str, Any]) -> str | None:
    return _fact_node_id(fact, "plant_part")


def _fact_provenance(fact: dict[str, Any]) -> dict[str, Any]:
    provenance = fact.get("provenance") or {}
    return _dict(provenance, "evidence.facts[].provenance")


def _build_plant_part_explorer(
    facts: list[dict[str, Any]],
    graph_nodes: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    parts: dict[str, dict[str, Any]] = {}

    for fact in facts:
        entities = _dict(fact.get("entities"), "evidence.facts[].entities")
        part_name = entities.get("plant_part")
        use_name = entities.get("therapeutic_use")
        if not part_name:
            continue

        part_name = str(part_name)
        bucket = parts.setdefault(
            part_name.casefold(),
            {
                "plant_part": part_name,
                "node_ids": set(),
                "fact_indices": [],
                "uses": {},
            },
        )

        part_id = _fact_part_node_id(fact)
        if part_id:
            bucket["node_ids"].add(part_id)

        row_index = fact.get("row_index")
        if isinstance(row_index, int):
            bucket["fact_indices"].append(row_index)

        if use_name:
            use_name = str(use_name)
            use_key = use_name.casefold()
            use_bucket = bucket["uses"].setdefault(
                use_key,
                {
                    "name": use_name,
                    "node_ids": set(),
                    "fact_indices": [],
                },
            )
            use_id = _fact_use_node_id(fact)
            if use_id:
                use_bucket["node_ids"].add(use_id)
            if isinstance(row_index, int):
                use_bucket["fact_indices"].append(row_index)

    output: list[dict[str, Any]] = []
    for bucket in sorted(
        parts.values(),
        key=lambda item: (-len(item["fact_indices"]), item["plant_part"].casefold()),
    ):
        use_items = sorted(
            bucket["uses"].values(),
            key=lambda item: item["name"].casefold(),
        )
        representative = [item["name"] for item in use_items[:8]]

        output.append(
            {
                "plant_part": bucket["plant_part"],
                "node_ids": sorted(bucket["node_ids"]),
                "fact_count": len(bucket["fact_indices"]),
                "fact_indices": sorted(bucket["fact_indices"]),
                "unique_use_count": len(use_items),
                "representative_uses": representative,
                "uses": [
                    {
                        "name": item["name"],
                        "node_ids": sorted(item["node_ids"]),
                        "fact_indices": sorted(item["fact_indices"]),
                    }
                    for item in use_items
                ],
            }
        )

    return output


def _build_fact_index(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for fact in facts:
        row_index = fact.get("row_index")
        provenance = _fact_provenance(fact)
        entities = _dict(fact.get("entities"), "evidence.facts[].entities")
        index.append(
            {
                "row_index": row_index,
                "entities": entities,
                "node_ids": list(provenance.get("node_ids", []) or []),
                "relationship_ids": list(provenance.get("relationship_ids", []) or []),
                "measurements": list(fact.get("measurements", []) or []),
            }
        )
    return index


def _subject_profile(graph: dict[str, Any], entities: list[dict[str, Any]]) -> dict[str, Any]:
    node_by_id, _ = _build_graph_index(graph)

    resolved_entities = [
        entity for entity in entities
        if isinstance(entity, dict) and entity.get("resolution_status") == "resolved"
    ]
    plants = [
        entity for entity in resolved_entities
        if entity.get("entity_type") == "plant"
    ]

    plant_node = None
    if plants:
        canonical = plants[0].get("canonical_value") or plants[0].get("canonical_id")
        if canonical:
            plant_node = node_by_id.get(str(canonical))

    if plant_node is None:
        plant_nodes = [
            node for node in node_by_id.values()
            if str(node.get("type", "")).casefold() == "plant"
            or "Plant Name" in (node.get("labels") or [])
        ]
        if plant_nodes:
            plant_node = plant_nodes[0]

    if plant_node is None:
        return {"type": "unknown", "name": None, "scientific_name": None}

    properties = plant_node.get("properties") or {}
    return {
        "type": "plant",
        "name": _node_name(plant_node),
        "scientific_name": properties.get("scientific_name") or _node_name(plant_node),
        "node_id": plant_node.get("id"),
        "common_names": properties.get("common_names"),
        "taxonomy_id": properties.get("taxonomy_id"),
        "source": properties.get("source"),
        "global_status": properties.get("global_status"),
        "regional_status": properties.get("regional_status"),
        "iucn_year": properties.get("iucn_year"),
    }


def _validate_answer(answer: Any) -> dict[str, Any] | None:
    if answer is None:
        return None
    obj = _dict(answer, "answer")
    return obj


def build_frontend_response(result: Any) -> dict[str, Any]:
    """Build the deterministic Phase 8 view model from a successful backend response."""
    root = _dict(result, "response")
    status = root.get("status")

    output: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "query": root.get("query"),
        "intent": root.get("intent"),
        "answer": _validate_answer(root.get("answer")),
        "subject": None,
        "statistics": None,
        "plant_parts": [],
        "fact_index": [],
        "graph_binding": None,
        "interaction": {
            "can_expand_graph": False,
            "can_open_evidence": False,
            "can_highlight_fact": False,
        },
        "ui_notes": [],
    }

    if status != "success":
        clarification = root.get("clarification_question")
        unresolved = root.get("unresolved_entities", [])
        if clarification:
            output["ui_notes"].append({"type": "clarification", "text": clarification})
        if unresolved:
            output["ui_notes"].append({"type": "unresolved_entities", "entities": unresolved})
        return output

    graph = _dict(root.get("graph"), "response.graph")
    evidence = _dict(root.get("evidence"), "response.evidence")
    entities = _list(root.get("entities"), "response.entities")

    node_by_id, rel_by_id = _build_graph_index(graph)
    facts = _list(evidence.get("facts"), "response.evidence.facts")

    output["subject"] = _subject_profile(graph, entities)
    output["statistics"] = {
        "row_count": graph.get("row_count"),
        "fact_count": evidence.get("summary", {}).get("fact_count"),
        "node_count": len(node_by_id),
        "relationship_count": len(rel_by_id),
        "node_counts_by_type": evidence.get("summary", {}).get("node_counts_by_type", {}),
        "relationship_counts_by_type": evidence.get("summary", {}).get("relationship_counts_by_type", {}),
        "bioactivity_counts_by_standard_type": evidence.get("summary", {}).get(
            "bioactivity_counts_by_standard_type", {}
        ),
        "bioactivity_counts_by_confidence": evidence.get("summary", {}).get(
            "bioactivity_counts_by_confidence", {}
        ),
    }
    output["plant_parts"] = _build_plant_part_explorer(facts, node_by_id)
    output["fact_index"] = _build_fact_index(facts)
    output["graph_binding"] = {
        "source": "response.graph",
        "node_ids": sorted(node_by_id),
        "relationship_ids": sorted(rel_by_id),
    }
    output["interaction"] = {
        "can_expand_graph": bool(graph.get("nodes")) and bool(graph.get("relationships")),
        "can_open_evidence": bool(facts),
        "can_highlight_fact": bool(facts),
    }

    # Stable reference metadata lets the frontend connect answer sections to
    # graph/evidence without trying to interpret the LLM prose.
    output["ui_notes"].append(
        {
            "type": "grounding",
            "text": "Answer content is grounded in the returned graph/evidence response.",
        }
    )

    try:
        json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise FrontendContractError(f"Frontend response is not JSON serializable: {exc}") from exc

    return output


def _minimal_result() -> dict[str, Any]:
    graph = {
        "intent": "plant_therapeutic_uses",
        "cypher": "",
        "params": {"plant": "Mangifera indica", "limit": 5000},
        "row_count": 2,
        "nodes": [
            {
                "id": "Mangifera indica",
                "labels": ["Plant Name"],
                "type": "plant",
                "name": "Mangifera indica",
                "properties": {
                    "scientific_name": "Mangifera indica",
                    "common_names": "Mango",
                },
            },
            {
                "id": "PP1",
                "labels": ["Plant Part"],
                "type": "plant_part",
                "name": "leaf",
                "properties": {"plant_part": "leaf"},
            },
            {
                "id": "TU1",
                "labels": ["Therapeutic Uses"],
                "type": "therapeutic_use",
                "name": "Diabetes",
                "properties": {"therapeutic_use": "Diabetes"},
            },
        ],
        "relationships": [
            {"id": "R1", "type": "Has Part", "source": "Mangifera indica", "target": "PP1", "properties": {}},
            {"id": "R2", "type": "Used For", "source": "PP1", "target": "TU1", "properties": {}},
        ],
        "rows": [
            {"nodes": ["Mangifera indica", "PP1", "TU1"], "relationships": ["R1", "R2"]},
            {"nodes": ["Mangifera indica", "PP1", "TU1"], "relationships": ["R1", "R2"]},
        ],
    }
    facts = [
        {
            "row_index": 0,
            "path": {
                "nodes": [
                    {"id": "Mangifera indica", "type": "plant", "name": "Mangifera indica"},
                    {"id": "PP1", "type": "plant_part", "name": "leaf"},
                    {"id": "TU1", "type": "therapeutic_use", "name": "Diabetes"},
                ],
                "relationships": [
                    {"id": "R1", "type": "Has Part", "source": "Mangifera indica", "target": "PP1"},
                    {"id": "R2", "type": "Used For", "source": "PP1", "target": "TU1"},
                ],
            },
            "entities": {"plant": "Mangifera indica", "plant_part": "leaf", "therapeutic_use": "Diabetes"},
            "measurements": [],
            "provenance": {"node_ids": ["Mangifera indica", "PP1", "TU1"], "relationship_ids": ["R1", "R2"], "graph_row_index": 0},
        },
        {
            "row_index": 1,
            "path": {
                "nodes": [
                    {"id": "Mangifera indica", "type": "plant", "name": "Mangifera indica"},
                    {"id": "PP1", "type": "plant_part", "name": "leaf"},
                    {"id": "TU1", "type": "therapeutic_use", "name": "Diabetes"},
                ],
                "relationships": [
                    {"id": "R1", "type": "Has Part", "source": "Mangifera indica", "target": "PP1"},
                    {"id": "R2", "type": "Used For", "source": "PP1", "target": "TU1"},
                ],
            },
            "entities": {"plant": "Mangifera indica", "plant_part": "leaf", "therapeutic_use": "Diabetes"},
            "measurements": [],
            "provenance": {"node_ids": ["Mangifera indica", "PP1", "TU1"], "relationship_ids": ["R1", "R2"], "graph_row_index": 1},
        },
    ]
    return {
        "status": "success",
        "query": "Tell me the use of mango",
        "intent": "plant_therapeutic_uses",
        "entities": [{
            "entity_type": "plant",
            "user_term": "mango",
            "resolution_status": "resolved",
            "canonical_value": "Mangifera indica",
        }],
        "graph": graph,
        "evidence": {
            "summary": {
                "row_count": 2,
                "fact_count": 2,
                "node_count": 3,
                "relationship_count": 4,
                "node_counts_by_type": {"plant": 1, "plant_part": 1, "therapeutic_use": 1},
                "relationship_counts_by_type": {"Has Part": 1, "Used For": 2},
                "bioactivity_counts_by_standard_type": {},
                "bioactivity_counts_by_confidence": {},
            },
            "facts": facts,
        },
        "answer": {
            "title": "Mango (Mangifera indica)",
            "overview": "The graph contains a leaf association with Diabetes.",
            "plant_facts": ["Scientific name: Mangifera indica"],
            "key_takeaways": ["The graph contains leaf-related evidence."],
            "plant_part_sections": [],
            "interpretation": "This is a graph association.",
            "caveats": [],
            "unsupported_claims": [],
            "grounded": True,
        },
    }


def _self_test() -> None:
    result = build_frontend_response(_minimal_result())
    assert result["contract_version"] == CONTRACT_VERSION
    assert result["subject"]["scientific_name"] == "Mangifera indica"
    assert result["statistics"]["fact_count"] == 2
    assert len(result["plant_parts"]) == 1
    assert result["plant_parts"][0]["plant_part"] == "leaf"
    assert result["plant_parts"][0]["fact_count"] == 2
    assert result["plant_parts"][0]["unique_use_count"] == 1
    assert result["plant_parts"][0]["uses"][0]["name"] == "Diabetes"
    assert result["plant_parts"][0]["uses"][0]["fact_indices"] == [0, 1]
    assert result["fact_index"][0]["node_ids"] == ["Mangifera indica", "PP1", "TU1"]
    assert result["interaction"]["can_expand_graph"] is True
    print("Frontend response contract: PASS")
    print("Subject profile mapping: PASS")
    print("Plant-part explorer mapping: PASS")
    print("Evidence-to-graph cross-reference: PASS")
    print("Interactive capability flags: PASS")
    print("PHASE 8 FRONTEND RESPONSE CONTRACT TEST: PASS")


if __name__ == "__main__":
    _self_test()
