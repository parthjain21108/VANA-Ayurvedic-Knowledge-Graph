from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any
import json
import re


class EvidenceBuildError(ValueError):
    """Raised when a Phase 6 graph cannot be converted into Phase 7A evidence."""


_LABEL_TO_TYPE = {
    "Plant Name": "plant",
    "Plant Part": "plant_part",
    "Phytochemicals": "phytochemical",
    "Therapeutic Uses": "therapeutic_use",
    "Bio-Activity": "bioactivity",
    "Targets": "target",
    "Documents": "document",
}

_LABEL_TO_PRIMARY_FIELD = {
    "Plant Name": "scientific_name",
    "Plant Part": "plant_part_id",
    "Phytochemicals": "inchi_key",
    "Therapeutic Uses": "mesh_id",
    "Bio-Activity": "bioactivity_id",
    "Targets": "target_chembl_id",
    "Documents": "document_chembl_id",
}

# Fields that are especially useful for human-readable explanations.
_LABEL_DISPLAY_FIELDS = {
    "Plant Name": ("scientific_name", "common_names"),
    "Plant Part": ("plant_part", "plant_part_id"),
    "Phytochemicals": ("phytochemical_name", "inchi_key", "chembl_id"),
    "Therapeutic Uses": ("therapeutic_use", "mesh_id"),
    "Bio-Activity": (
        "bioactivity_id",
        "standard_type",
        "standard_relation",
        "standard_value",
        "standard_units",
        "normalized_value_nm",
        "high_confidence_tier",
    ),
    "Targets": ("target_name", "target_chembl_id", "uniprot_id"),
    "Documents": (
        "document_chembl_id",
        "source_id",
        "document_journal",
        "document_year",
    ),
}


@dataclass(frozen=True)
class EvidenceContract:
    """
    Phase 7A output.

    `facts` is the canonical evidence representation. Each fact is one
    graph-query row translated into an ordered node/relationship path.

    `summary` is deterministic metadata calculated from the graph payload.
    The later LLM layer can use it, but does not calculate these values.
    """

    query_context: dict[str, Any]
    summary: dict[str, Any]
    facts: list[dict[str, Any]]
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_context": self.query_context,
            "summary": self.summary,
            "facts": self.facts,
            "provenance": self.provenance,
        }


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceBuildError(f"{path} must be an object.")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidenceBuildError(f"{path} must be an array.")
    return value


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _natural_index(key: str, prefix: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", key)
    return int(match.group(1)) if match else None


def _index_map(row: dict[str, Any], prefix: str) -> dict[int, Any]:
    values: dict[int, Any] = {}
    for key, value in row.items():
        index = _natural_index(str(key), prefix)
        if index is not None:
            values[index] = value
    return values


def _node_type(node: dict[str, Any]) -> str:
    labels = node.get("labels") or []
    for label in labels:
        if label in _LABEL_TO_TYPE:
            return _LABEL_TO_TYPE[label]
    return "unknown"


def _node_label(node: dict[str, Any]) -> str:
    labels = node.get("labels") or []
    return str(labels[0]) if labels else "Unknown"


def _node_display_name(node: dict[str, Any]) -> str | None:
    properties = node.get("properties") or {}
    labels = node.get("labels") or []
    for label in labels:
        for field in _LABEL_DISPLAY_FIELDS.get(label, ()):
            value = properties.get(field)
            if value is not None and str(value).strip():
                return str(value)
    value = node.get("id")
    return str(value) if value is not None else None


def _compact_properties(node: dict[str, Any]) -> dict[str, Any]:
    """
    Preserve useful factual properties without inventing or rewriting them.

    We retain every property that is already present in the graph node.
    This is deliberately lossless for evidence.
    """
    return dict(node.get("properties") or {})


def _resolve_node_ref(ref: Any, node_by_id: dict[str, dict[str, Any]], path: str) -> dict[str, Any]:
    ref_obj = _require_dict(ref, path)
    if set(ref_obj.keys()) != {"node_ref"}:
        raise EvidenceBuildError(f"{path} must contain exactly node_ref.")
    node_id = ref_obj.get("node_ref")
    if not _nonempty(node_id):
        raise EvidenceBuildError(f"{path}.node_ref must be a non-empty string.")
    if node_id not in node_by_id:
        raise EvidenceBuildError(
            f"{path}.node_ref {node_id!r} is not present in graph.nodes."
        )
    return node_by_id[node_id]


def _resolve_relationship_ref(
    ref: Any,
    relationship_by_id: dict[str, dict[str, Any]],
    path: str,
) -> dict[str, Any]:
    ref_obj = _require_dict(ref, path)
    if set(ref_obj.keys()) != {"relationship_ref"}:
        raise EvidenceBuildError(f"{path} must contain exactly relationship_ref.")
    rel_id = ref_obj.get("relationship_ref")
    if not _nonempty(rel_id):
        raise EvidenceBuildError(
            f"{path}.relationship_ref must be a non-empty string."
        )
    if rel_id not in relationship_by_id:
        raise EvidenceBuildError(
            f"{path}.relationship_ref {rel_id!r} is not present in graph.relationships."
        )
    return relationship_by_id[rel_id]


def _validate_graph_refs(
    graph: dict[str, Any],
    node_by_id: dict[str, dict[str, Any]],
    relationship_by_id: dict[str, dict[str, Any]],
) -> None:
    nodes = _require_list(graph.get("nodes"), "graph.nodes")
    relationships = _require_list(graph.get("relationships"), "graph.relationships")
    rows = _require_list(graph.get("rows"), "graph.rows")

    if graph.get("row_count") != len(rows):
        raise EvidenceBuildError(
            f"graph.row_count={graph.get('row_count')} != len(graph.rows)={len(rows)}."
        )

    seen_node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        node = _require_dict(node, f"graph.nodes[{index}]")
        node_id = str(node.get("id", ""))
        if not node_id:
            raise EvidenceBuildError(f"graph.nodes[{index}].id is missing.")
        if node_id in seen_node_ids:
            raise EvidenceBuildError(f"Duplicate graph node id: {node_id!r}.")
        seen_node_ids.add(node_id)

    seen_relationship_ids: set[str] = set()
    for index, rel in enumerate(relationships):
        rel = _require_dict(rel, f"graph.relationships[{index}]")
        rel_id = str(rel.get("id", ""))
        if not rel_id:
            raise EvidenceBuildError(f"graph.relationships[{index}].id is missing.")
        if rel_id in seen_relationship_ids:
            raise EvidenceBuildError(
                f"Duplicate graph relationship id: {rel_id!r}."
            )
        seen_relationship_ids.add(rel_id)
        source = str(rel.get("source", ""))
        target = str(rel.get("target", ""))
        if source not in node_by_id:
            raise EvidenceBuildError(
                f"Relationship {rel_id!r} source {source!r} is missing."
            )
        if target not in node_by_id:
            raise EvidenceBuildError(
                f"Relationship {rel_id!r} target {target!r} is missing."
            )


def _make_node_evidence(node: dict[str, Any]) -> dict[str, Any]:
    labels = [str(x) for x in (node.get("labels") or [])]
    return {
        "id": str(node["id"]),
        "type": _node_type(node),
        "label": _node_label(node),
        "name": _node_display_name(node),
        "properties": _compact_properties(node),
    }


def _make_relationship_evidence(rel: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(rel["id"]),
        "type": str(rel["type"]),
        "source": str(rel["source"]),
        "target": str(rel["target"]),
        "properties": dict(rel.get("properties") or {}),
    }


def _fact_from_row(
    row: dict[str, Any],
    row_index: int,
    node_by_id: dict[str, dict[str, Any]],
    relationship_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    node_refs = _index_map(row, "n")
    relationship_refs = _index_map(row, "r")

    if not node_refs:
        raise EvidenceBuildError(f"graph.rows[{row_index}] contains no node refs.")

    nodes_ordered: list[dict[str, Any]] = []
    relationships_ordered: list[dict[str, Any]] = []

    for node_index in sorted(node_refs):
        node = _resolve_node_ref(
            node_refs[node_index],
            node_by_id,
            f"graph.rows[{row_index}].n{node_index}",
        )
        nodes_ordered.append(_make_node_evidence(node))

    for rel_index in sorted(relationship_refs):
        rel = _resolve_relationship_ref(
            relationship_refs[rel_index],
            relationship_by_id,
            f"graph.rows[{row_index}].r{rel_index}",
        )
        relationships_ordered.append(_make_relationship_evidence(rel))

    # Verify the row's ordered path is coherent wherever adjacent node/edge
    # positions exist. This is a structural check, not a biological inference.
    node_positions = {index: nodes_ordered[pos] for pos, index in enumerate(sorted(node_refs))}
    for rel_index, rel_ref in relationship_refs.items():
        if rel_index not in node_positions or (rel_index + 1) not in node_positions:
            # Some graph query shapes do not expose a simple n0-r0-n1 sequence.
            # We retain the evidence but skip an invalid assumption about order.
            continue
        rel = _resolve_relationship_ref(
            rel_ref,
            relationship_by_id,
            f"graph.rows[{row_index}].r{rel_index}",
        )
        left = node_positions[rel_index]["id"]
        right = node_positions[rel_index + 1]["id"]
        if rel["source"] != left or rel["target"] != right:
            # Cypher can return relationship direction that is not represented
            # by positional naming. The graph itself is authoritative; do not
            # rewrite the path. Record the mismatch explicitly.
            path_direction = "non_positional"
        else:
            path_direction = "positional"
    else:
        path_direction = "positional"

    semantic_entities = {
        node["type"]: node["name"]
        for node in nodes_ordered
        if node.get("type") != "unknown" and node.get("name")
    }

    # Measurements are deterministic extracts from any Bio-Activity nodes
    # present in this row.
    measurements: list[dict[str, Any]] = []
    for node in nodes_ordered:
        if node["type"] == "bioactivity":
            props = node["properties"]
            measurement = {
                key: props[key]
                for key in (
                    "bioactivity_id",
                    "standard_type",
                    "standard_relation",
                    "standard_value",
                    "standard_units",
                    "normalized_value_nm",
                    "high_confidence_tier",
                )
                if key in props
            }
            if measurement:
                measurements.append(measurement)

    return {
        "row_index": row_index,
        "path": {
            "nodes": nodes_ordered,
            "relationships": relationships_ordered,
            "direction_check": path_direction,
        },
        "entities": semantic_entities,
        "measurements": measurements,
        "provenance": {
            "node_ids": [node["id"] for node in nodes_ordered],
            "relationship_ids": [rel["id"] for rel in relationships_ordered],
            "graph_row_index": row_index,
        },
    }


def _summary(
    graph: dict[str, Any],
    nodes: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    node_type_counts = Counter(node["type"] for node in nodes)
    relation_counts = Counter(rel["type"] for rel in relationships)

    measurement_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    for node in nodes:
        if node["type"] != "bioactivity":
            continue
        props = node["properties"]
        standard_type = props.get("standard_type")
        if standard_type:
            measurement_counts[str(standard_type)] += 1
        tier = props.get("high_confidence_tier")
        if tier:
            confidence_counts[str(tier)] += 1

    subject_candidates: list[dict[str, str]] = []
    seen_subjects: set[tuple[str, str]] = set()
    for fact in facts:
        for node in fact["path"]["nodes"]:
            key = (node["type"], node["id"])
            if key in seen_subjects:
                continue
            seen_subjects.add(key)
            if node["type"] in {"plant", "phytochemical", "therapeutic_use", "target", "bioactivity", "document"}:
                subject_candidates.append(
                    {
                        "type": node["type"],
                        "id": node["id"],
                        "name": node["name"] or node["id"],
                    }
                )

    return {
        "row_count": int(graph["row_count"]),
        "node_count": len(nodes),
        "relationship_count": len(relationships),
        "node_counts_by_type": dict(node_type_counts),
        "relationship_counts_by_type": dict(relation_counts),
        "bioactivity_counts_by_standard_type": dict(measurement_counts),
        "bioactivity_counts_by_confidence": dict(confidence_counts),
        "subject_candidates": subject_candidates,
        "fact_count": len(facts),
    }


def build_evidence(graph_response: Any) -> dict[str, Any]:
    """
    Convert a Phase 6 graph response into a generic Phase 7A evidence object.

    Scope:
      - works from returned graph nodes/relationships/rows
      - does not query Neo4j
      - does not call an LLM
      - does not infer unsupported biology
      - preserves provenance back to graph row/node/relationship IDs
    """
    graph = _require_dict(graph_response, "graph")

    intent = graph.get("intent")
    if not _nonempty(intent):
        raise EvidenceBuildError("graph.intent must be a non-empty string.")

    nodes_raw = _require_list(graph.get("nodes"), "graph.nodes")
    relationships_raw = _require_list(
        graph.get("relationships"),
        "graph.relationships",
    )
    rows_raw = _require_list(graph.get("rows"), "graph.rows")

    node_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_node in enumerate(nodes_raw):
        node = _require_dict(raw_node, f"graph.nodes[{index}]")
        node_id = str(node.get("id", ""))
        if not node_id:
            raise EvidenceBuildError(f"graph.nodes[{index}].id is missing.")
        node_by_id[node_id] = node

    relationship_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_rel in enumerate(relationships_raw):
        rel = _require_dict(raw_rel, f"graph.relationships[{index}]")
        rel_id = str(rel.get("id", ""))
        if not rel_id:
            raise EvidenceBuildError(
                f"graph.relationships[{index}].id is missing."
            )
        relationship_by_id[rel_id] = rel

    _validate_graph_refs(graph, node_by_id, relationship_by_id)

    facts: list[dict[str, Any]] = []
    for row_index, raw_row in enumerate(rows_raw):
        row = _require_dict(raw_row, f"graph.rows[{row_index}]")
        facts.append(
            _fact_from_row(
                row,
                row_index,
                node_by_id,
                relationship_by_id,
            )
        )

    nodes = [_make_node_evidence(node) for node in node_by_id.values()]
    relationships = [
        _make_relationship_evidence(rel)
        for rel in relationship_by_id.values()
    ]

    result = EvidenceContract(
        query_context={
            "intent": str(intent),
            "params": dict(graph.get("params") or {}),
        },
        summary=_summary(graph, nodes, relationships, facts),
        facts=facts,
        provenance={
            "source": "phase6_graph_response",
            "graph_intent": str(intent),
            "node_ids": [node["id"] for node in nodes],
            "relationship_ids": [rel["id"] for rel in relationships],
        },
    )

    output = result.to_dict()
    # Full evidence must itself be safe to hand to the next API/LLM layer.
    try:
        json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise EvidenceBuildError(
            f"Phase 7A evidence is not JSON serializable: {exc}"
        ) from exc

    return output


def _node(
    node_id: str,
    label: str,
    properties: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": node_id,
        "labels": [label],
        "properties": properties,
    }


def _rel(
    rel_id: str,
    rel_type: str,
    source: str,
    target: str,
) -> dict[str, Any]:
    return {
        "id": rel_id,
        "type": rel_type,
        "source": source,
        "target": target,
        "properties": {},
    }


def _row(
    nodes: list[tuple[str, str, dict[str, Any]]],
    rels: list[tuple[str, str, str, str]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, (node_id, _label, _properties) in enumerate(nodes):
        result[f"n{index}"] = {"node_ref": node_id}
    for index, (rel_id, _type, _source, _target) in enumerate(rels):
        result[f"r{index}"] = {"relationship_ref": rel_id}
    return result


def _synthetic_graph(
    intent: str,
    nodes: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "intent": intent,
        "cypher": "MATCH (n) RETURN n LIMIT $limit",
        "params": {"limit": 5000},
        "row_count": len(rows),
        "nodes": nodes,
        "relationships": relationships,
        "rows": rows,
    }


def _offline_test() -> None:
    # Plant -> part -> therapeutic use
    nodes = [
        _node("P1", "Plant Name", {"scientific_name": "Mangifera indica"}),
        _node("PP1", "Plant Part", {"plant_part": "fruit", "plant_part_id": "M-FR"}),
        _node("TU1", "Therapeutic Uses", {"therapeutic_use": "Hypercholesterolemia", "mesh_id": "D006937"}),
    ]
    rels = [
        _rel("R1", "Has Part", "P1", "PP1"),
        _rel("R2", "Used For", "PP1", "TU1"),
    ]
    row = _row(
        [
            ("P1", "Plant Name", nodes[0]["properties"]),
            ("PP1", "Plant Part", nodes[1]["properties"]),
            ("TU1", "Therapeutic Uses", nodes[2]["properties"]),
        ],
        [("R1", "Has Part", "P1", "PP1"), ("R2", "Used For", "PP1", "TU1")],
    )
    evidence = build_evidence(
        _synthetic_graph("plant_therapeutic_uses", nodes, rels, [row])
    )
    assert evidence["summary"]["row_count"] == 1
    assert evidence["facts"][0]["entities"]["plant"] == "Mangifera indica"
    assert evidence["facts"][0]["entities"]["therapeutic_use"] == "Hypercholesterolemia"
    print("Plant -> therapeutic use: PASS")

    # Plant -> part -> phytochemical -> bioactivity
    nodes = [
        _node("P1", "Plant Name", {"scientific_name": "Acacia leucophloea"}),
        _node("PP1", "Plant Part", {"plant_part": "flower", "plant_part_id": "AC-LE-FL"}),
        _node("PC1", "Phytochemicals", {"phytochemical_name": "Quercetin", "inchi_key": "Q"}),
        _node("BA1", "Bio-Activity", {
            "bioactivity_id": "BA_1",
            "standard_type": "IC50",
            "standard_relation": "=",
            "standard_value": "900.0",
            "standard_units": "nM",
            "normalized_value_nm": "900.0",
            "high_confidence_tier": "HIGH",
        }),
    ]
    rels = [
        _rel("R1", "Has Part", "P1", "PP1"),
        _rel("R2", "Contains", "PP1", "PC1"),
        _rel("R3", "Has Bioactivity", "PC1", "BA1"),
    ]
    row = _row(
        [(n["id"], n["labels"][0], n["properties"]) for n in nodes],
        [(r["id"], r["type"], r["source"], r["target"]) for r in rels],
    )
    evidence = build_evidence(
        _synthetic_graph("plant_phytochemical_bioactivities", nodes, rels, [row])
    )
    fact = evidence["facts"][0]
    assert fact["entities"]["phytochemical"] == "Quercetin"
    assert fact["measurements"][0]["standard_type"] == "IC50"
    assert fact["measurements"][0]["standard_units"] == "nM"
    print("Plant -> phytochemical -> bioactivity: PASS")

    # Plant -> phytochemical -> bioactivity -> target
    nodes = [
        _node("P1", "Plant Name", {"scientific_name": "Acacia leucophloea"}),
        _node("PP1", "Plant Part", {"plant_part": "flower", "plant_part_id": "AC-LE-FL"}),
        _node("PC1", "Phytochemicals", {"phytochemical_name": "Quercetin", "inchi_key": "Q"}),
        _node("BA1", "Bio-Activity", {
            "bioactivity_id": "BA_696",
            "standard_type": "IC50",
            "standard_value": "900.0",
            "standard_units": "nM",
            "high_confidence_tier": "HIGH",
        }),
        _node("T1", "Targets", {
            "target_name": "Epidermal growth factor receptor",
            "target_chembl_id": "CHEMBL203",
        }),
    ]
    rels = [
        _rel("R1", "Has Part", "P1", "PP1"),
        _rel("R2", "Contains", "PP1", "PC1"),
        _rel("R3", "Has Bioactivity", "PC1", "BA1"),
        _rel("R4", "Measured On", "BA1", "T1"),
    ]
    row = _row(
        [(n["id"], n["labels"][0], n["properties"]) for n in nodes],
        [(r["id"], r["type"], r["source"], r["target"]) for r in rels],
    )
    evidence = build_evidence(
        _synthetic_graph("plant_phytochemical_bioactivities", nodes, rels, [row])
    )
    assert evidence["facts"][0]["entities"]["target"] == "Epidermal growth factor receptor"
    assert evidence["summary"]["relationship_counts_by_type"]["Measured On"] == 1
    print("Plant -> phytochemical -> bioactivity -> target: PASS")

    # Bioactivity -> document
    nodes = [
        _node("BA1", "Bio-Activity", {
            "bioactivity_id": "BA_1",
            "standard_type": "IC50",
            "standard_value": "100.0",
            "standard_units": "nM",
        }),
        _node("D1", "Documents", {
            "document_chembl_id": "CHEMBL1201862",
            "source_id": "SRC1",
            "document_journal": "Example Journal",
            "document_year": 2001,
        }),
    ]
    rels = [_rel("R1", "Reported In", "BA1", "D1")]
    row = _row(
        [(n["id"], n["labels"][0], n["properties"]) for n in nodes],
        [(r["id"], r["type"], r["source"], r["target"]) for r in rels],
    )
    evidence = build_evidence(
        _synthetic_graph("bioactivity_documents", nodes, rels, [row])
    )
    assert evidence["facts"][0]["entities"]["document"] == "CHEMBL1201862"
    print("Bioactivity -> document: PASS")

    # Malformed relationship endpoint must fail loudly.
    bad_graph = _synthetic_graph(
        "test",
        [_node("P1", "Plant Name", {"scientific_name": "Test"})],
        [_rel("R1", "Has Part", "P1", "MISSING")],
        [{"n0": {"node_ref": "P1"}, "r0": {"relationship_ref": "R1"}}],
    )
    try:
        build_evidence(bad_graph)
    except EvidenceBuildError:
        print("Malformed graph detection: PASS")
    else:
        raise AssertionError("Malformed graph should fail Phase 7A.")

    print("PHASE 7A EVIDENCE BUILDER TEST: PASS")


if __name__ == "__main__":
    _offline_test()