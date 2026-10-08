"""Single source of truth for the persisted Neo4j query schema."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass

NODE_TYPES = (
    "plant", "plant_part", "phytochemical", "therapeutic_use",
    "bioactivity", "target", "document",
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

SEARCH_FIELDS = {
    "plant": ("scientific_name", "common_names", "taxonomy_id"),
    "plant_part": ("plant_part", "plant_part_id", "source_name", "plant_name"),
    "phytochemical": ("phytochemical_name", "inchi_key", "imppat_id", "chembl_id"),
    "therapeutic_use": ("therapeutic_use", "mesh_id", "mesh_category_name", "mesh_sub_category_name"),
    "bioactivity": ("bioactivity_id", "standard_type", "standard_relation", "standard_units", "high_confidence_tier"),
    "target": ("target_name", "target_chembl_id", "uniprot_id", "target_organism", "target_type"),
    "document": ("document_chembl_id", "source_id", "document_journal", "document_year"),
}

DISPLAY_FIELDS = {
    "plant": ("scientific_name", "common_names"),
    "plant_part": ("plant_part", "plant_part_id", "source_name"),
    "phytochemical": ("phytochemical_name", "inchi_key", "chembl_id", "imppat_id"),
    "therapeutic_use": ("therapeutic_use", "mesh_id", "mesh_category_name", "mesh_sub_category_name"),
    "bioactivity": ("bioactivity_id", "standard_type", "standard_relation", "standard_value", "standard_units", "normalized_value_nm", "high_confidence_tier"),
    "target": ("target_name", "target_chembl_id", "uniprot_id", "target_organism", "target_type"),
    "document": ("document_chembl_id", "source_id", "document_journal", "document_year"),
}

# Actual persisted direction.
EDGES = (
    ("plant", "Has Part", "plant_part"),
    ("plant_part", "Contains", "phytochemical"),
    ("plant_part", "Used For", "therapeutic_use"),
    ("phytochemical", "Has Bioactivity", "bioactivity"),
    ("bioactivity", "Measured On", "target"),
    ("bioactivity", "Reported In", "document"),
)

ADJ = {t: [] for t in NODE_TYPES}
for a, rel, b in EDGES:
    ADJ[a].append((b, rel, 1))
    ADJ[b].append((a, rel, -1))  # traverse inverse of persisted relationship

FILTER_FIELDS = {
    "standard_type": ("bioactivity", "standard_type", "="),
    "standard_relation": ("bioactivity", "standard_relation", "="),
    "standard_units": ("bioactivity", "standard_units", "="),
    "min_standard_value": ("bioactivity", "standard_value", ">="),
    "max_standard_value": ("bioactivity", "standard_value", "<="),
    "normalized_value_nm_min": ("bioactivity", "normalized_value_nm", ">="),
    "normalized_value_nm_max": ("bioactivity", "normalized_value_nm", "<="),
    "high_confidence_tier": ("bioactivity", "high_confidence_tier", "="),
    "target_organism": ("target", "target_organism", "="),
    "target_type": ("target", "target_type", "="),
}

@dataclass(frozen=True)
class PathStep:
    source: str
    relationship: str
    target: str
    direction: int  # +1 persisted direction, -1 inverse


def shortest_path(source: str, target: str) -> tuple[PathStep, ...]:
    if source not in NODE_TYPES or target not in NODE_TYPES:
        raise ValueError(f"Unknown node type: {source} or {target}")
    if source == target:
        return ()
    q = deque([source])
    parent: dict[str, tuple[str, str, int]] = {}
    seen = {source}
    while q:
        cur = q.popleft()
        for nxt, rel, direction in ADJ[cur]:
            if nxt in seen:
                continue
            parent[nxt] = (cur, rel, direction)
            if nxt == target:
                q.clear()
                break
            seen.add(nxt)
            q.append(nxt)
    if target not in parent:
        raise ValueError(f"No schema path from {source} to {target}")
    chain: list[tuple[str, str, str, int]] = []
    cur = target
    while cur != source:
        prev, rel, direction = parent[cur]
        chain.append((prev, rel, cur, direction))
        cur = prev
    chain.reverse()
    return tuple(PathStep(*x) for x in chain)


def minimal_tree(terminals: set[str]) -> tuple[tuple[str, ...], tuple[PathStep, ...]]:
    """Return nodes/edges of the unique minimal subtree connecting terminals.

    The persisted schema is a tree, so the minimal connecting subtree is unique.
    """
    if not terminals:
        raise ValueError("At least one terminal node type is required")
    if not terminals.issubset(set(NODE_TYPES)):
        raise ValueError(f"Unknown terminal types: {terminals - set(NODE_TYPES)}")
    if len(terminals) == 1:
        return (next(iter(terminals)),), ()

    edges: set[tuple[str, str, str, int]] = set()
    nodes = set(terminals)
    root = next(iter(terminals))
    for other in terminals:
        if other == root:
            continue
        for step in shortest_path(root, other):
            key = (step.source, step.relationship, step.target, step.direction)
            rev = (step.target, step.relationship, step.source, -step.direction)
            if rev in edges:
                edges.remove(rev)
            else:
                edges.add(key)
            nodes.add(step.source); nodes.add(step.target)
    return tuple(sorted(nodes)), tuple(sorted(edges))
