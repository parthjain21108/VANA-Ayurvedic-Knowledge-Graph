from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json


class ResponseContractError(ValueError):
    """Raised when a Phase 6 response violates the frontend contract."""


@dataclass(frozen=True)
class GraphResponseContract:
    """
    Frontend-facing graph contract.

    Authoritative visualization data:
      - nodes
      - relationships

    Result/detail data:
      - rows

    Diagnostic metadata:
      - intent
      - cypher
      - params
      - row_count
    """

    intent: str
    cypher: str
    params: dict[str, Any]
    row_count: int
    nodes: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    rows: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "cypher": self.cypher,
            "params": self.params,
            "row_count": self.row_count,
            "nodes": self.nodes,
            "relationships": self.relationships,
            "rows": self.rows,
        }


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResponseContractError(f"{path} must be an object.")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ResponseContractError(f"{path} must be an array.")
    return value


def _require_nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResponseContractError(f"{path} must be a non-empty string.")
    return value


def _validate_node(node: Any, index: int) -> None:
    path = f"graph.nodes[{index}]"
    node = _require_dict(node, path)

    _require_nonempty_string(node.get("id"), f"{path}.id")

    labels = _require_list(node.get("labels"), f"{path}.labels")
    for label_index, label in enumerate(labels):
        _require_nonempty_string(label, f"{path}.labels[{label_index}]")

    _require_dict(node.get("properties"), f"{path}.properties")


def _validate_relationship(
    rel: Any,
    index: int,
    node_ids: set[str],
) -> None:
    path = f"graph.relationships[{index}]"
    rel = _require_dict(rel, path)

    _require_nonempty_string(rel.get("id"), f"{path}.id")
    _require_nonempty_string(rel.get("type"), f"{path}.type")
    source = _require_nonempty_string(rel.get("source"), f"{path}.source")
    target = _require_nonempty_string(rel.get("target"), f"{path}.target")

    if source not in node_ids:
        raise ResponseContractError(
            f"{path}.source {source!r} does not reference a returned node."
        )

    if target not in node_ids:
        raise ResponseContractError(
            f"{path}.target {target!r} does not reference a returned node."
        )

    _require_dict(rel.get("properties"), f"{path}.properties")


def _validate_row_refs(
    row: Any,
    row_index: int,
    node_ids: set[str],
    relationship_ids: set[str],
) -> None:
    path = f"graph.rows[{row_index}]"
    row = _require_dict(row, path)

    def visit(value: Any, value_path: str) -> None:
        if isinstance(value, dict):
            if set(value.keys()) == {"node_ref"}:
                node_ref = _require_nonempty_string(
                    value.get("node_ref"),
                    f"{value_path}.node_ref",
                )
                if node_ref not in node_ids:
                    raise ResponseContractError(
                        f"{value_path}.node_ref {node_ref!r} "
                        "does not reference a returned node."
                    )
                return

            if set(value.keys()) == {"relationship_ref"}:
                rel_ref = _require_nonempty_string(
                    value.get("relationship_ref"),
                    f"{value_path}.relationship_ref",
                )
                if rel_ref not in relationship_ids:
                    raise ResponseContractError(
                        f"{value_path}.relationship_ref {rel_ref!r} "
                        "does not reference a returned relationship."
                    )
                return

            for key, child in value.items():
                visit(child, f"{value_path}.{key}")
            return

        if isinstance(value, list):
            for child_index, child in enumerate(value):
                visit(child, f"{value_path}[{child_index}]")
            return

        if isinstance(value, tuple):
            for child_index, child in enumerate(value):
                visit(child, f"{value_path}[{child_index}]")

    for key, value in row.items():
        visit(value, f"{path}.{key}")


def validate_graph_response(graph: Any) -> GraphResponseContract:
    """
    Validate the graph_query_engine payload.

    This is intentionally strict:
    malformed graph data fails loudly rather than being silently repaired.
    """
    graph = _require_dict(graph, "graph")

    intent = _require_nonempty_string(graph.get("intent"), "graph.intent")
    cypher = _require_nonempty_string(graph.get("cypher"), "graph.cypher")

    params = _require_dict(graph.get("params"), "graph.params")

    row_count = graph.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        raise ResponseContractError("graph.row_count must be an integer.")
    if row_count < 0:
        raise ResponseContractError("graph.row_count must be >= 0.")

    nodes = _require_list(graph.get("nodes"), "graph.nodes")
    relationships = _require_list(graph.get("relationships"), "graph.relationships")
    rows = _require_list(graph.get("rows"), "graph.rows")

    node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        _validate_node(node, index)
        node_id = str(node["id"])
        if node_id in node_ids:
            raise ResponseContractError(
                f"Duplicate node id found: {node_id!r}."
            )
        node_ids.add(node_id)

    relationship_ids: set[str] = set()
    for index, rel in enumerate(relationships):
        _validate_relationship(rel, index, node_ids)
        rel_id = str(rel["id"])
        if rel_id in relationship_ids:
            raise ResponseContractError(
                f"Duplicate relationship id found: {rel_id!r}."
            )
        relationship_ids.add(rel_id)

    if row_count != len(rows):
        raise ResponseContractError(
            f"graph.row_count={row_count} does not match "
            f"len(graph.rows)={len(rows)}."
        )

    for index, row in enumerate(rows):
        _validate_row_refs(row, index, node_ids, relationship_ids)

    payload = {
        "intent": intent,
        "cypher": cypher,
        "params": params,
        "row_count": row_count,
        "nodes": nodes,
        "relationships": relationships,
        "rows": rows,
    }

    # Confirm the entire graph contract is JSON serializable.
    try:
        json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ResponseContractError(
            f"Graph response is not JSON serializable: {exc}"
        ) from exc

    return GraphResponseContract(
        intent=intent,
        cypher=cypher,
        params=params,
        row_count=row_count,
        nodes=nodes,
        relationships=relationships,
        rows=rows,
    )


def validate_api_response(response: Any) -> dict[str, Any]:
    """
    Validate the top-level Phase 6 API response.

    For successful responses, graph must be present and valid.
    Non-success states may legitimately return graph=None.
    """
    response = _require_dict(response, "response")

    status = _require_nonempty_string(response.get("status"), "response.status")

    if "query" in response and response["query"] is not None:
        _require_nonempty_string(response["query"], "response.query")

    if "intent" in response and response["intent"] is not None:
        _require_nonempty_string(response["intent"], "response.intent")

    entities = _require_list(response.get("entities", []), "response.entities")
    filters = _require_dict(response.get("filters", {}), "response.filters")
    ingestion = _require_dict(response.get("ingestion", {}), "response.ingestion")
    notes = _require_list(response.get("notes", []), "response.notes")

    # Validate basic list content without rewriting it.
    for index, entity in enumerate(entities):
        _require_dict(entity, f"response.entities[{index}]")

    for index, note in enumerate(notes):
        _require_nonempty_string(note, f"response.notes[{index}]")

    if status == "success":
        graph = validate_graph_response(response.get("graph"))
        normalized = dict(response)
        normalized["graph"] = graph.to_dict()
        # Explicit serialization test of the entire returned contract.
        json.dumps(normalized, ensure_ascii=False)
        return normalized

    if response.get("graph") is not None:
        validate_graph_response(response["graph"])

    return dict(response)


def _sample_valid_graph() -> dict[str, Any]:
    return {
        "intent": "test",
        "cypher": "MATCH (n) RETURN n LIMIT $limit",
        "params": {"limit": 5000},
        "row_count": 1,
        "nodes": [
            {
                "id": "P1",
                "labels": ["Plant Name"],
                "properties": {"scientific_name": "Test plant"},
            },
            {
                "id": "B1",
                "labels": ["Bio-Activity"],
                "properties": {"bioactivity_id": "BA_1"},
            },
        ],
        "relationships": [
            {
                "id": "R1",
                "type": "Has Bioactivity",
                "source": "P1",
                "target": "B1",
                "properties": {},
            }
        ],
        "rows": [
            {
                "n0": {"node_ref": "P1"},
                "n1": {"node_ref": "B1"},
                "r0": {"relationship_ref": "R1"},
            }
        ],
    }


def _offline_test() -> None:
    valid = _sample_valid_graph()
    result = validate_graph_response(valid)
    assert result.row_count == 1
    assert result.nodes[0]["id"] == "P1"
    assert result.relationships[0]["source"] == "P1"
    print("Valid graph contract: PASS")

    # 1. Dangling relationship endpoint.
    bad = json.loads(json.dumps(valid))
    bad["relationships"][0]["target"] = "MISSING"
    try:
        validate_graph_response(bad)
    except ResponseContractError:
        print("Dangling relationship detection: PASS")
    else:
        raise AssertionError("Dangling relationship should fail validation.")

    # 2. Duplicate node ID.
    bad = json.loads(json.dumps(valid))
    bad["nodes"].append(json.loads(json.dumps(bad["nodes"][0])))
    try:
        validate_graph_response(bad)
    except ResponseContractError:
        print("Duplicate node detection: PASS")
    else:
        raise AssertionError("Duplicate node should fail validation.")

    # 3. Duplicate relationship ID.
    bad = json.loads(json.dumps(valid))
    bad["relationships"].append(json.loads(json.dumps(bad["relationships"][0])))
    try:
        validate_graph_response(bad)
    except ResponseContractError:
        print("Duplicate relationship detection: PASS")
    else:
        raise AssertionError("Duplicate relationship should fail validation.")

    # 4. row_count mismatch.
    bad = json.loads(json.dumps(valid))
    bad["row_count"] = 2
    try:
        validate_graph_response(bad)
    except ResponseContractError:
        print("Row-count consistency detection: PASS")
    else:
        raise AssertionError("row_count mismatch should fail validation.")

    # 5. Missing row node reference.
    bad = json.loads(json.dumps(valid))
    bad["rows"][0]["n0"]["node_ref"] = "MISSING"
    try:
        validate_graph_response(bad)
    except ResponseContractError:
        print("Row node-reference detection: PASS")
    else:
        raise AssertionError("Missing row node_ref should fail validation.")

    # 6. Missing row relationship reference.
    bad = json.loads(json.dumps(valid))
    bad["rows"][0]["r0"]["relationship_ref"] = "MISSING"
    try:
        validate_graph_response(bad)
    except ResponseContractError:
        print("Row relationship-reference detection: PASS")
    else:
        raise AssertionError("Missing row relationship_ref should fail validation.")

    # 7. Top-level successful API response.
    api_response = {
        "status": "success",
        "query": "test",
        "intent": "test",
        "entities": [],
        "filters": {},
        "ingestion": {},
        "graph": valid,
        "clarification_question": None,
        "notes": [],
    }
    normalized = validate_api_response(api_response)
    assert normalized["graph"]["row_count"] == 1
    print("Successful API response contract: PASS")

    print("PHASE 6 RESPONSE CONTRACT TEST: PASS")


if __name__ == "__main__":
    _offline_test()