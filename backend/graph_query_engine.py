from __future__ import annotations

from typing import Any

from query.cypher_builder import (
    PreparedQuery,
    CypherBuildError,
    build_query,
)


NODE_KEYS = {
    "Plant Name": "scientific_name",
    "Plant Part": "plant_part_id",
    "Phytochemicals": "inchi_key",
    "Therapeutic Uses": "mesh_id",
    "Bio-Activity": "bioactivity_id",
    "Targets": "target_chembl_id",
    "Documents": "document_chembl_id",
}


class GraphQueryError(RuntimeError):
    """Raised when a structured query cannot be executed safely."""


def _serialize_node(node: Any) -> dict[str, Any]:
    labels = list(getattr(node, "labels", []))
    properties = dict(node)

    node_id: str | None = None

    for label in labels:
        key = NODE_KEYS.get(label)
        if key and str(properties.get(key, "")).strip():
            node_id = str(properties[key])
            break

    if node_id is None:
        node_id = str(
            getattr(
                node,
                "element_id",
                getattr(node, "id", ""),
            )
        )

    return {
        "id": node_id,
        "labels": labels,
        "properties": properties,
    }


def _serialize_relationship(
    rel: Any,
    node_ids: dict[str, str],
) -> dict[str, Any]:
    start_element_id = str(rel.start_node.element_id)
    end_element_id = str(rel.end_node.element_id)

    relationship_id = str(
        getattr(
            rel,
            "element_id",
            getattr(rel, "id", ""),
        )
    )

    return {
        "id": relationship_id,
        "type": str(rel.type),
        "source": node_ids.get(
            start_element_id,
            start_element_id,
        ),
        "target": node_ids.get(
            end_element_id,
            end_element_id,
        ),
        "properties": dict(rel),
    }


def _is_node_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value.keys()) == {"node_ref"}
        and bool(str(value.get("node_ref", "")).strip())
    )


def _is_relationship_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value.keys()) == {"relationship_ref"}
        and bool(
            str(value.get("relationship_ref", "")).strip()
        )
    )


def _normalize_row(
    raw_row: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert the clean semantic Cypher builder's column names into
    the existing Phase 6 / Phase 7 row contract.

    The downstream contract expects:

        n0, n1, n2, ...
        r0, r1, r2, ...

    The new Cypher builder may instead return:

        n_plant
        n_plant_part
        n_phytochemical
        ...
        r0, r1, ...

    We preserve the actual graph references and only normalize the
    column names. No graph data is changed.
    """

    normalized: dict[str, Any] = {}

    next_node_index = 0
    next_relationship_index = 0

    # Preserve non-node/non-relationship scalar fields, if any.
    deferred: list[tuple[str, Any]] = []

    for key, value in raw_row.items():
        key_str = str(key)

        if _is_node_ref(value):
            normalized[
                f"n{next_node_index}"
            ] = value
            next_node_index += 1
            continue

        if _is_relationship_ref(value):
            normalized[
                f"r{next_relationship_index}"
            ] = value
            next_relationship_index += 1
            continue

        deferred.append((key_str, value))

    # Keep ordinary returned scalar fields after graph refs.
    for key, value in deferred:
        if key not in normalized:
            normalized[key] = value

    return normalized


def execute_query(
    session: Any,
    query: Any,
) -> dict[str, Any]:
    """
    Execute the deterministic Cypher query and return the graph response
    expected by the existing Phase 6 / Phase 7 pipeline.
    """

    try:
        prepared: PreparedQuery = build_query(query)
    except CypherBuildError as exc:
        raise GraphQueryError(str(exc)) from exc

    try:
        result = session.run(
            prepared.cypher,
            **prepared.params,
        )
    except Exception as exc:
        raise GraphQueryError(
            f"Neo4j query execution failed: {exc}"
        ) from exc

    nodes: dict[str, dict[str, Any]] = {}
    relationships_raw: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []

    def visit(value: Any) -> Any:
        if value is None:
            return None

        # Neo4j Node
        if (
            hasattr(value, "labels")
            and hasattr(value, "items")
        ):
            element_id = str(value.element_id)

            if element_id not in nodes:
                nodes[element_id] = _serialize_node(value)

            return {
                "node_ref": nodes[element_id]["id"]
            }

        # Neo4j Relationship
        if (
            hasattr(value, "start_node")
            and hasattr(value, "end_node")
            and hasattr(value, "items")
        ):
            element_id = str(value.element_id)

            if element_id not in relationships_raw:
                relationships_raw[element_id] = value

            return {
                "relationship_ref": element_id
            }

        # Lists
        if isinstance(value, list):
            return [
                visit(item)
                for item in value
            ]

        # Tuples
        if isinstance(value, tuple):
            return [
                visit(item)
                for item in value
            ]

        # Dictionaries
        if isinstance(value, dict):
            return {
                str(key): visit(item)
                for key, item in value.items()
            }

        return value

    for record in result:
        raw_row = {
            str(key): visit(record[key])
            for key in record.keys()
        }

        rows.append(
            _normalize_row(raw_row)
        )

    node_ids = {
        element_id: payload["id"]
        for element_id, payload in nodes.items()
    }

    relationships = [
        _serialize_relationship(
            relationship,
            node_ids,
        )
        for relationship in relationships_raw.values()
    ]

    return {
        "intent": prepared.intent,
        "cypher": prepared.cypher,
        "params": prepared.params,
        "row_count": len(rows),
        "nodes": list(nodes.values()),
        "relationships": relationships,
        "rows": rows,
    }