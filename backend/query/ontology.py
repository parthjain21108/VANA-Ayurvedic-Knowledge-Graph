"""Authoritative graph ontology and query-pattern catalog for NLP planning.

This module describes the graph that the NLP layer is allowed to traverse.
It intentionally contains *no entity-specific aliases* (no diabetes, EGFR,
quercetin, etc.). Entity values come from the user's query and are resolved
against Neo4j later.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Iterable

NODE_TYPES = (
    "plant",
    "plant_part",
    "phytochemical",
    "therapeutic_use",
    "bioactivity",
    "target",
    "document",
)

NODE_LABELS = {
    "plant": "Plant Name",
    "plant_part": "Plant Part",
    "phytochemical": "Phytochemicals",
    "therapeutic_use": "Therapeutic Uses",
    "bioactivity": "Bio-Activity",
    "target": "Targets",
    "document": "Documents",
}

CANONICAL_KEYS = {
    "plant": "scientific_name",
    "plant_part": "plant_part_id",
    "phytochemical": "inchi_key",
    "therapeutic_use": "mesh_id",
    "bioactivity": "bioactivity_id",
    "target": "target_chembl_id",
    "document": "document_chembl_id",
}

DISPLAY_FIELDS = {
    "plant": ("scientific_name", "common_names"),
    "plant_part": ("plant_part", "plant_part_id", "source_name"),
    "phytochemical": ("phytochemical_name", "inchi_key", "chembl_id", "imppat_id"),
    "therapeutic_use": ("therapeutic_use", "mesh_id", "mesh_category_name", "mesh_sub_category_name"),
    "bioactivity": (
        "bioactivity_id", "standard_type", "standard_relation", "standard_value",
        "standard_units", "normalized_value_nm", "high_confidence_tier",
    ),
    "target": ("target_name", "target_chembl_id", "uniprot_id", "target_organism", "target_type"),
    "document": ("document_chembl_id", "source_id", "document_journal", "document_year"),
}

# Directed graph edges exactly matching the persisted Neo4j relationship model.
EDGES = (
    ("plant", "Has Part", "plant_part"),
    ("plant_part", "Contains", "phytochemical"),
    ("plant_part", "Used For", "therapeutic_use"),
    ("phytochemical", "Has Bioactivity", "bioactivity"),
    ("bioactivity", "Measured On", "target"),
    ("bioactivity", "Reported In", "document"),
)

OUT = {node: tuple() for node in NODE_TYPES}
for src, rel, dst in EDGES:
    OUT[src] = OUT[src] + ((rel, dst),)

IN = {node: tuple() for node in NODE_TYPES}
for src, rel, dst in EDGES:
    IN[dst] = IN[dst] + ((rel, src),)


@dataclass(frozen=True)
class QueryPattern:
    """A graph-valid natural-language query family."""

    intent: str
    subject: str
    requested: str | None
    path_nodes: tuple[str, ...]
    path_relationships: tuple[str, ...]
    optional_entity: str | None = None
    optional_filters: tuple[str, ...] = ()
    description: str = ""


# Existing graph-query-engine intent families. The NLP layer maps semantic
# requests into these patterns; it does not match wording directly to intent.
PATTERNS = (
    QueryPattern("plant_information", "plant", None, ("plant",), (), description="Plant attributes."),
    QueryPattern("plant_parts", "plant", "plant_part", ("plant", "plant_part"), ("Has Part",), description="Parts belonging to a plant."),
    QueryPattern("plant_phytochemicals", "plant", "phytochemical", ("plant", "plant_part", "phytochemical"), ("Has Part", "Contains"), optional_entity="plant_part", description="Compounds present through plant parts."),
    QueryPattern("plant_therapeutic_uses", "plant", "therapeutic_use", ("plant", "plant_part", "therapeutic_use"), ("Has Part", "Used For"), optional_entity="plant_part", description="Uses/conditions associated through plant parts."),
    QueryPattern("plant_bioactivities", "plant", "bioactivity", ("plant", "plant_part", "phytochemical", "bioactivity"), ("Has Part", "Contains", "Has Bioactivity"), optional_filters=("bioactivity",), description="Bioactivity records associated with a plant."),
    QueryPattern("plant_targets", "plant", "target", ("plant", "plant_part", "phytochemical", "bioactivity", "target"), ("Has Part", "Contains", "Has Bioactivity", "Measured On"), optional_filters=("bioactivity", "target"), description="Targets reached through plant bioactivities."),
    QueryPattern("plant_phytochemical_bioactivities", "plant", "phytochemical", ("plant", "plant_part", "phytochemical", "bioactivity"), ("Has Part", "Contains", "Has Bioactivity"), optional_entity="phytochemical", optional_filters=("bioactivity", "target"), description="Which phytochemicals satisfy bioactivity constraints."),
    QueryPattern("plant_phytochemical_targets", "plant", "phytochemical", ("plant", "plant_part", "phytochemical", "bioactivity", "target"), ("Has Part", "Contains", "Has Bioactivity", "Measured On"), optional_entity="phytochemical", optional_filters=("bioactivity", "target"), description="Which phytochemicals satisfy target constraints."),
    QueryPattern("plant_therapeutic_use_phytochemicals", "plant", "phytochemical", ("plant", "plant_part", "therapeutic_use", "phytochemical"), ("Has Part", "Used For", "Contains"), optional_filters=("therapeutic_use",), description="Compounds in plant parts associated with a use."),
    QueryPattern("phytochemical_information", "phytochemical", None, ("phytochemical",), (), description="Phytochemical attributes."),
    QueryPattern("phytochemical_plants", "phytochemical", "plant", ("phytochemical", "plant_part", "plant"), ("Contains<-", "Has Part<-"), description="Plants containing a phytochemical."),
    QueryPattern("phytochemical_bioactivities", "phytochemical", "bioactivity", ("phytochemical", "bioactivity"), ("Has Bioactivity",), optional_filters=("bioactivity",), description="Bioactivities for a phytochemical."),
    QueryPattern("phytochemical_targets", "phytochemical", "target", ("phytochemical", "bioactivity", "target"), ("Has Bioactivity", "Measured On"), optional_filters=("bioactivity", "target"), description="Targets associated with a phytochemical."),
    QueryPattern("therapeutic_use_plants", "therapeutic_use", "plant", ("therapeutic_use", "plant_part", "plant"), ("Used For<-", "Has Part<-"), description="Plants associated with a therapeutic use."),
    QueryPattern("therapeutic_use_phytochemicals", "therapeutic_use", "phytochemical", ("therapeutic_use", "plant_part", "phytochemical"), ("Used For<-", "Contains"), description="Phytochemicals associated with a therapeutic use through plant parts."),
    QueryPattern("target_phytochemicals", "target", "phytochemical", ("target", "bioactivity", "phytochemical"), ("Measured On<-", "Has Bioactivity<-"), optional_filters=("bioactivity",), description="Phytochemicals associated with a target."),
    QueryPattern("target_bioactivities", "target", "bioactivity", ("target", "bioactivity"), ("Measured On<-",), optional_filters=("bioactivity",), description="Bioactivities measured on a target."),
    QueryPattern("bioactivity_information", "bioactivity", None, ("bioactivity",), (), description="Bioactivity attributes."),
    QueryPattern("bioactivity_targets", "bioactivity", "target", ("bioactivity", "target"), ("Measured On",), optional_filters=("target",), description="Targets measured for a bioactivity."),
    QueryPattern("bioactivity_documents", "bioactivity", "document", ("bioactivity", "document"), ("Reported In",), description="Documents reporting a bioactivity."),
    QueryPattern("document_information", "document", None, ("document",), (), description="Document attributes."),
)

PATTERN_BY_INTENT = {p.intent: p for p in PATTERNS}

# Filters actually supported by the persisted Bio-Activity / Targets schema.
BIOACTIVITY_FILTER_FIELDS = (
    "standard_type",
    "standard_relation",
    "standard_units",
    "min_standard_value",
    "max_standard_value",
    "normalized_value_nm_min",
    "normalized_value_nm_max",
    "high_confidence_tier",
)
TARGET_FILTER_FIELDS = ("target_organism", "target_type")
ALL_FILTER_FIELDS = BIOACTIVITY_FILTER_FIELDS + TARGET_FILTER_FIELDS


def filter_implied_types(filters: Any) -> set[str]:
    """Node types that must appear on the schema path because a filter targets them.

    e.g. a standard_type/standard_relation filter implies a `bioactivity` node
    must be on the path even if the user never explicitly named a bioactivity
    entity; a target_organism filter implies a `target` node.
    """
    implied: set[str] = set()
    for field in BIOACTIVITY_FILTER_FIELDS:
        value = getattr(filters, field, None)
        if value is not None and value != "":
            implied.add("bioactivity")
            break
    for field in TARGET_FILTER_FIELDS:
        value = getattr(filters, field, None)
        if value is not None and value != "":
            implied.add("target")
            break
    return implied


def _edge_removal_components(remove_edge: tuple[str, str, str]) -> tuple[set[str], set[str]]:
    """Node types reachable from each side of the tree once one edge is cut."""
    remaining = [e for e in EDGES if e != remove_edge]
    adj: dict[str, list[str]] = {n: [] for n in NODE_TYPES}
    for a, _, b in remaining:
        adj[a].append(b)
        adj[b].append(a)

    src = remove_edge[0]
    seen = {src}
    stack = [src]
    while stack:
        cur = stack.pop()
        for nxt in adj[cur]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen, set(NODE_TYPES) - seen


@dataclass(frozen=True)
class SchemaPath:
    """The minimal schema-valid subtree connecting a set of required node types."""

    terminal_types: frozenset[str]
    node_types: tuple[str, ...]  # every node type touched, in a valid MATCH order
    edges: tuple[tuple[str, str, str], ...]  # (src_type, relationship, dst_type), real graph direction


def plan_schema_path(terminal_types: Iterable[str], root_hint: str | None = None) -> SchemaPath | None:
    """Compute the minimal schema path connecting arbitrary combinations of node types.

    This is the core generalization: the persisted schema graph is a tree (6
    edges over 7 node types, fully connected, no cycles), so the minimal
    subtree connecting *any* subset of node types is unique and can be derived
    mechanically (a Steiner tree on a tree = keep an edge iff cutting it would
    separate terminals on both sides). This replaces matching a request against
    a fixed, named catalog of query patterns: any legal combination of
    subject/requested/constraint entity types -- including combinations no one
    enumerated in advance -- resolves to a path this way.
    """
    terminals = frozenset(terminal_types)
    if not terminals or not terminals.issubset(NODE_TYPES):
        return None

    if len(terminals) == 1:
        only = next(iter(terminals))
        return SchemaPath(terminal_types=terminals, node_types=(only,), edges=())

    kept_edges: list[tuple[str, str, str]] = []
    for edge in EDGES:
        side_a, side_b = _edge_removal_components(edge)
        if (side_a & terminals) and (side_b & terminals):
            kept_edges.append(edge)

    if not kept_edges:
        return None

    touched: set[str] = set()
    for a, _, b in kept_edges:
        touched.add(a)
        touched.add(b)
    if not terminals.issubset(touched):
        # Fail closed rather than silently dropping a requested entity type.
        return None

    root = root_hint if root_hint in touched else None
    if root is None:
        for node in NODE_TYPES:
            if node in touched:
                root = node
                break

    adj: dict[str, list[tuple[str, str, bool]]] = {n: [] for n in touched}
    for a, rel, b in kept_edges:
        adj[a].append((b, rel, True))   # a -> b is the real direction
        adj[b].append((a, rel, False))  # a -> b is the real direction, traversed from b

    visited = {root}
    order: list[str] = [root]
    ordered_edges: list[tuple[str, str, str]] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        for neighbor, rel, forward in adj[cur]:
            if neighbor in visited:
                continue
            visited.add(neighbor)
            order.append(neighbor)
            if forward:
                ordered_edges.append((cur, rel, neighbor))
            else:
                ordered_edges.append((neighbor, rel, cur))
            stack.append(neighbor)

    return SchemaPath(terminal_types=terminals, node_types=tuple(order), edges=tuple(ordered_edges))


def enumerate_simple_paths(max_edges: int = 4) -> list[tuple[str, ...]]:
    """Enumerate directed simple node-type paths permitted by the schema."""
    paths: set[tuple[str, ...]] = set()

    def walk(path: tuple[str, ...]) -> None:
        paths.add(path)
        if len(path) - 1 >= max_edges:
            return
        for _, dst in OUT[path[-1]]:
            if dst in path:
                continue
            walk(path + (dst,))

    for node in NODE_TYPES:
        walk((node,))
    return sorted(paths, key=lambda p: (len(p), p))


def ontology_prompt_block() -> str:
    """Render the ontology into model-readable instructions, without domain entities."""
    node_lines = []
    for node in NODE_TYPES:
        node_lines.append(
            f"- {node}: Neo4j label={NODE_LABELS[node]}, canonical key={CANONICAL_KEYS[node]}, display fields={', '.join(DISPLAY_FIELDS[node])}"
        )
    edge_lines = [f"- {src} -[{rel}]-> {dst}" for src, rel, dst in EDGES]
    example_lines = []
    for p in PATTERNS:
        path = " -> ".join(p.path_nodes)
        rels = " / ".join(p.path_relationships) if p.path_relationships else "none"
        optional = f"; optional={p.optional_entity}" if p.optional_entity else ""
        filters = f"; filters={','.join(p.optional_filters)}" if p.optional_filters else ""
        example_lines.append(f"- {p.subject} -> {p.requested or 'attributes'}: {path}; relationships={rels}{optional}{filters}; {p.description}")

    return (
        "GRAPH NODE TYPES:\n"
        + "\n".join(node_lines)
        + "\n\nGRAPH EDGES (DIRECTED):\n"
        + "\n".join(edge_lines)
        + "\n\nThe planner supports ANY schema-valid combination of these node types as"
        " subject/requested/constraint -- it is not limited to a fixed list. The"
        " query below is illustrative of the kinds of subject->requested"
        " relationships the schema supports, not an exhaustive list:\n"
        + "\n".join(example_lines)
        + "\n\nBIOACTIVITY FILTER FIELDS:\n"
        + ", ".join(BIOACTIVITY_FILTER_FIELDS)
        + "\nTARGET FILTER FIELDS:\n"
        + ", ".join(TARGET_FILTER_FIELDS)
        + "\n\nIMPORTANT: entity values are never inferred from this ontology."
        " They must remain the user's mention until graph-backed resolution."
    )
