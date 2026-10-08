from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import StructuredQuery
from .schema import (
    NODE_TYPES,
    NODE_LABELS,
    CANONICAL_KEYS,
    FILTER_FIELDS,
    minimal_tree,
)


@dataclass(frozen=True)
class PreparedQuery:
    intent: str
    cypher: str
    params: dict[str, Any]


class CypherBuildError(RuntimeError):
    """Raised when a structured query cannot be translated safely."""


def _etype(entity: Any) -> str:
    return getattr(
        entity.entity_type,
        "value",
        str(entity.entity_type),
    )


def _resolved_entities(q: StructuredQuery) -> dict[str, Any]:
    entities: dict[str, Any] = {}

    for entity in q.entities:
        entity_type = _etype(entity)

        if entity_type not in NODE_TYPES:
            raise CypherBuildError(
                f"Unknown entity type: {entity_type}"
            )

        if entity_type in entities:
            raise CypherBuildError(
                f"Multiple entities of type {entity_type} "
                "are not yet supported"
            )

        status = getattr(
            entity.resolution_status,
            "value",
            entity.resolution_status,
        )

        if status != "resolved":
            raise CypherBuildError(
                f"{entity_type} is not resolved: "
                f"{getattr(entity, 'user_term', '')}"
            )

        value = (
            getattr(entity, "canonical_id", None)
            or getattr(entity, "canonical_value", None)
        )

        if value in (None, ""):
            raise CypherBuildError(
                f"Resolved {entity_type} has no canonical identity"
            )

        if not isinstance(value, (str, int, float)):
            raise CypherBuildError(
                f"Resolved {entity_type} has an unsupported "
                f"canonical identity type: {type(value).__name__}"
            )

        entities[entity_type] = entity

    return entities


def _implied(q: StructuredQuery) -> set[str]:
    implied: set[str] = set()
    filters = q.filters

    if filters is None:
        return set()

    for field, (entity_type, _, _) in FILTER_FIELDS.items():
        value = getattr(filters, field, None)
        if value not in (None, ""):
            implied.add(entity_type)

    return implied


def _validate_terminals(
    terminals: set[str],
    requested: str | None,
) -> None:
    if requested is not None and requested not in NODE_TYPES:
        raise CypherBuildError(
            f"Unknown requested entity type: {requested}"
        )

    unknown = terminals - set(NODE_TYPES)
    if unknown:
        raise CypherBuildError(
            f"Unknown graph entity types: {sorted(unknown)}"
        )


def _node_pattern(entity_type: str) -> str:
    try:
        label = NODE_LABELS[entity_type]
    except KeyError as exc:
        raise CypherBuildError(
            f"No Neo4j label configured for {entity_type}"
        ) from exc

    return f"({entity_type}:`{label}`)"


def _relationship_pattern(
    source: str,
    relationship: str,
    target: str,
    direction: int,
    index: int,
) -> str:
    if source not in NODE_TYPES or target not in NODE_TYPES:
        raise CypherBuildError(
            f"Schema edge contains unknown node type: "
            f"{source} -> {target}"
        )

    source_pattern = _node_pattern(source)
    target_pattern = _node_pattern(target)
    relationship_var = f"r{index}"

    if direction == 1:
        return (
            f"MATCH {source_pattern}"
            f"-[{relationship_var}:`{relationship}`]->"
            f"{target_pattern}"
        )

    if direction == -1:
        return (
            f"MATCH {source_pattern}"
            f"<-[{relationship_var}:`{relationship}`]-"
            f"{target_pattern}"
        )

    raise CypherBuildError(
        f"Unsupported relationship direction: {direction}"
    )


def _validate_prepared(
    *,
    nodes: list[str],
    edges: list[tuple[str, str, str, int]],
    where: list[str],
    returns: list[str],
    cypher: str,
    params: dict[str, Any],
) -> None:
    """
    Defensive checker for common builder regressions.

    It specifically prevents the historical bug where a single-node query
    emitted WHERE without a preceding MATCH.
    """
    if not cypher.strip():
        raise CypherBuildError("Generated Cypher is empty")

    if "WHERE " in cypher and "MATCH " not in cypher:
        raise CypherBuildError(
            "Generated Cypher contains WHERE without MATCH"
        )

    if where and not any(line.startswith("MATCH ") for line in cypher.splitlines()):
        raise CypherBuildError(
            "WHERE predicates exist but no MATCH clause was generated"
        )

    if not returns:
        raise CypherBuildError(
            "Generated Cypher has no RETURN expressions"
        )

    for key in params:
        placeholder = f"${key}"
        if key != "limit" and placeholder not in cypher:
            raise CypherBuildError(
                f"Unused Cypher parameter generated: {key}"
            )

    if "LIMIT $limit" not in cypher:
        raise CypherBuildError(
            "Generated Cypher must use the bounded $limit parameter"
        )

    if len(nodes) == 1 and edges:
        raise CypherBuildError(
            "A single-node schema tree cannot contain relationships"
        )

    if len(nodes) > 1 and not edges:
        raise CypherBuildError(
            "Multiple graph nodes were returned without schema relationships"
        )

    if len(edges) != len(
        [name for name in returns if name.startswith("r")]
    ):
        raise CypherBuildError(
            "Relationship RETURN columns do not match schema edges"
        )


def _is_plant_overview(
    q: StructuredQuery,
    entities: dict[str, Any],
    requested: str | None,
) -> bool:
    """Detect an explicit info request for one resolved plant."""
    return (
        q.operation == "info"
        and requested is None
        and set(entities) == {"plant"}
    )


def _build_plant_overview(
    plant_entity: Any,
    *,
    limit: int,
) -> PreparedQuery:
    """Return the complete downstream subgraph rooted at one plant.

    Each UNION arm returns one edge and its endpoints. The graph executor
    deduplicates nodes/relationships across rows, avoiding a Cartesian product.
    """
    value = (
        getattr(plant_entity, "canonical_id", None)
        or getattr(plant_entity, "canonical_value", None)
    )
    if value in (None, ""):
        raise CypherBuildError(
            "Plant overview requires a resolved scientific_name"
        )

    arms = [
        """MATCH (p:`Plant Name`)
WHERE p.`scientific_name` = $plant_scientific_name
RETURN p AS graph_n0, null AS graph_r0, null AS graph_n1""",
        """MATCH (p:`Plant Name`)-[r:`Has Part`]->(pp:`Plant Part`)
WHERE p.`scientific_name` = $plant_scientific_name
RETURN p AS graph_n0, r AS graph_r0, pp AS graph_n1""",
        """MATCH (p:`Plant Name`)-[:`Has Part`]->(pp:`Plant Part`)
-[r:`Contains`]->(ph:`Phytochemicals`)
WHERE p.`scientific_name` = $plant_scientific_name
RETURN pp AS graph_n0, r AS graph_r0, ph AS graph_n1""",
        """MATCH (p:`Plant Name`)-[:`Has Part`]->(pp:`Plant Part`)
-[r:`Used For`]->(u:`Therapeutic Uses`)
WHERE p.`scientific_name` = $plant_scientific_name
RETURN pp AS graph_n0, r AS graph_r0, u AS graph_n1""",
        """MATCH (p:`Plant Name`)-[:`Has Part`]->(pp:`Plant Part`)
-[:`Contains`]->(ph:`Phytochemicals`)
-[r:`Has Bioactivity`]->(ba:`Bio-Activity`)
WHERE p.`scientific_name` = $plant_scientific_name
RETURN ph AS graph_n0, r AS graph_r0, ba AS graph_n1""",
        """MATCH (p:`Plant Name`)-[:`Has Part`]->(pp:`Plant Part`)
-[:`Contains`]->(ph:`Phytochemicals`)
-[:`Has Bioactivity`]->(ba:`Bio-Activity`)
-[r:`Measured On`]->(t:`Targets`)
WHERE p.`scientific_name` = $plant_scientific_name
RETURN ba AS graph_n0, r AS graph_r0, t AS graph_n1""",
        """MATCH (p:`Plant Name`)-[:`Has Part`]->(pp:`Plant Part`)
-[:`Contains`]->(ph:`Phytochemicals`)
-[:`Has Bioactivity`]->(ba:`Bio-Activity`)
-[r:`Reported In`]->(d:`Documents`)
WHERE p.`scientific_name` = $plant_scientific_name
RETURN ba AS graph_n0, r AS graph_r0, d AS graph_n1""",
    ]

    # Plant overview is intentionally complete: unlike ordinary queries, the
    # frontend/evidence graph must retain every connected relationship returned
    # by the seven schema arms. A LIMIT appended after UNION ALL would only
    # constrain the final union arm rather than the complete compound query,
    # so do not emit a misleading partial limit here.
    cypher = "\nUNION ALL\n".join(arms)
    params = {
        "plant_scientific_name": str(value),
    }
    if "MATCH " not in cypher or "RETURN " not in cypher:
        raise CypherBuildError("Plant overview generated invalid Cypher")
    return PreparedQuery(
        intent="semantic:info:plant_overview",
        cypher=cypher,
        params=params,
    )


def build_query(
    q: StructuredQuery,
    *,
    limit: int = 5000,
) -> PreparedQuery:
    """
    Translate an already structured and entity-resolved query into
    deterministic Neo4j Cypher.

    Gemini never participates in this function. All topology, labels,
    canonical keys, and filters come from the graph schema.
    """
    entities = _resolved_entities(q)
    requested = q.requested_entity_type

    terminals = set(entities)
    if requested:
        terminals.add(requested)

    terminals |= _implied(q)

    if not terminals:
        raise CypherBuildError(
            "No graph entity is present in semantic query"
        )

    _validate_terminals(terminals, requested)

    path_requirements = {
        str(path_type).strip()
        for path_type in getattr(q, "path_requirements", []) or []
        if str(path_type).strip()
    }
    _validate_terminals(terminals | path_requirements, requested)

    if _is_plant_overview(q, entities, requested):
        return _build_plant_overview(
            entities["plant"],
            limit=limit,
        )

    # Structural path requirements force required graph nodes even when the
    # user did not name a concrete entity of that type.
    terminals.update(path_requirements)
    _validate_terminals(terminals, requested)

    try:
        nodes, edges = minimal_tree(terminals)
    except Exception as exc:
        raise CypherBuildError(
            f"Could not construct schema tree for {sorted(terminals)}: {exc}"
        ) from exc

    if not nodes:
        raise CypherBuildError(
            "Schema returned an empty graph tree"
        )

    params: dict[str, Any] = {
        "limit": max(1, min(int(limit), 5000))
    }

    # A tree with relationships declares/binds its nodes through each MATCH.
    # A one-node tree therefore needs an explicit standalone MATCH.
    match_lines: list[str] = []

    if edges:
        for index, edge in enumerate(edges):
            if len(edge) != 4:
                raise CypherBuildError(
                    f"Invalid schema edge returned by minimal_tree: {edge!r}"
                )

            source, relationship, target, direction = edge

            match_lines.append(
                _relationship_pattern(
                    source,
                    relationship,
                    target,
                    int(direction),
                    index,
                )
            )
    else:
        if len(nodes) != 1:
            raise CypherBuildError(
                "Schema returned multiple nodes without relationships"
            )

        match_lines.append(
            f"MATCH {_node_pattern(nodes[0])}"
        )

    where: list[str] = []

    # Resolved entity identities are always constrained by their schema
    # canonical key.
    for entity_type, entity in entities.items():
        key = CANONICAL_KEYS.get(entity_type)
        if not key:
            raise CypherBuildError(
                f"No canonical key configured for {entity_type}"
            )

        parameter_name = f"{entity_type}_{key}"
        value = (
            getattr(entity, "canonical_id", None)
            or getattr(entity, "canonical_value", None)
        )

        params[parameter_name] = str(value)
        where.append(
            f"{entity_type}.`{key}` = ${parameter_name}"
        )

    # Filters are constrained to the approved schema fields/operators.
    filters = q.filters

    if filters is not None:
        for field, (entity_type, property_name, operator) in FILTER_FIELDS.items():
            value = getattr(filters, field, None)

            if value in (None, ""):
                continue

            if entity_type not in nodes:
                raise CypherBuildError(
                    f"Filter {field} targets {entity_type}, "
                    f"but the schema tree contains {nodes}"
                )

            params[field] = value

            # CSV/canonical loaders may materialize numeric measurement fields
            # as strings. Convert them at query time before comparison.
            numeric_filter_fields = {
                "min_standard_value",
                "max_standard_value",
                "normalized_value_nm_min",
                "normalized_value_nm_max",
            }
            property_expr = (
                f"toFloat({entity_type}.`{property_name}`)"
                if field in numeric_filter_fields
                else f"{entity_type}.`{property_name}`"
            )
            where.append(
                f"{property_expr} {operator} ${field}"
            )

    returns = [
        f"{entity_type} AS n_{entity_type}"
        for entity_type in nodes
    ]

    returns.extend(
        f"r{index} AS r{index}"
        for index in range(len(edges))
    )

    query_text = "\n".join(match_lines)

    if where:
        query_text += "\nWHERE " + "\n  AND ".join(where)

    query_text += (
        "\nRETURN "
        + ", ".join(returns)
        + "\nLIMIT $limit"
    )

    intent = (
        f"semantic:{q.operation}:"
        f"{requested or 'subject_info'}"
    )

    _validate_prepared(
        nodes=nodes,
        edges=edges,
        where=where,
        returns=returns,
        cypher=query_text,
        params=params,
    )

    return PreparedQuery(
        intent=intent,
        cypher=query_text,
        params=params,
    )
