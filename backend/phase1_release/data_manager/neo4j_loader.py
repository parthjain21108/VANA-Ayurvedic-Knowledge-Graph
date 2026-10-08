from __future__ import annotations

"""Neo4j serving-layer loader.

This module deliberately does not alter the collector pipeline. It loads the
canonical CSV snapshot into an existing Neo4j schema using MERGE semantics.
Requires: pip install neo4j pandas
"""

import os
from pathlib import Path
from typing import Iterable

import pandas as pd

NODE_CONFIG = {
    "Plant.csv": ("Plant Name", "scientific_name", ["scientific_name", "taxonomy_id", "global_status", "regional_status", "iucn_year", "source", "common_names"]),
    "PlantPart.csv": ("Plant Part", "plant_part_id", ["plant_part_id", "plant_name", "plant_part", "source_name"]),
    "Phytochemicals.csv": ("Phytochemicals", "inchi_key", ["inchi_key", "phytochemical_name", "smiles", "imppat_id", "chembl_id", "classyfire_kingdom", "classyfire_superclass", "classyfire_class", "classyfire_subclass", "source_url"]),
    "TherapeuticUses.csv": ("Therapeutic Uses", "mesh_id", ["mesh_id", "therapeutic_use", "mesh_category", "mesh_category_name", "mesh_sub_category_name", "source"]),
    "Bioactivities.csv": ("Bio-Activity", "bioactivity_id", ["bioactivity_id", "standard_type", "standard_relation", "standard_value", "standard_units", "normalized_value_nm", "high_confidence_tier"]),
    "Targets.csv": ("Targets", "target_chembl_id", ["target_chembl_id", "uniprot_id", "target_name", "target_organism", "target_type"]),
    "Documents.csv": ("Documents", "document_chembl_id", ["document_chembl_id", "source_id", "document_title", "document_author", "document_journal", "document_year"]),
}

REL_CONFIG = {
    "HAS_PART.csv": ("Plant Name", "Plant Part", "Has Part"),
    "CONTAINS.csv": ("Plant Part", "Phytochemicals", "Contains"),
    "USED_FOR.csv": ("Plant Part", "Therapeutic Uses", "Used For"),
    "HAS_BIOACTIVITY.csv": ("Phytochemicals", "Bio-Activity", "Has Bioactivity"),
    "MEASURED_ON.csv": ("Bio-Activity", "Targets", "Measured On"),
    "REPORTED_IN.csv": ("Bio-Activity", "Documents", "Reported In"),
}


def load_canonical(store_root: Path, uri: str, user: str, password: str, database: str = "neo4j") -> None:
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("Neo4j loader requires the 'neo4j' Python package. Install requirements-phase1.txt first.") from exc
    current = Path(store_root) / "current"
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            _load_nodes(session, current)
            _load_relationships(session, current)
    finally:
        driver.close()


def _load_nodes(session, current: Path) -> None:
    for filename, (label, key, allowed_fields) in NODE_CONFIG.items():
        df = pd.read_csv(current / filename, dtype=object, keep_default_na=False)
        if df.empty:
            continue
        # Only fields defined by the existing Neo4j model are written.
        field_names = [f for f in allowed_fields if f in df.columns]
        rows = [{k: row[k] for k in field_names} for row in df.to_dict(orient="records")]
        query = f"""
        UNWIND $rows AS row
        MERGE (n:`{label}` {{{key}: row[{key!r}]}})
        SET n += row
        """
        session.run(query, rows=rows).consume()


def _load_relationships(session, current: Path) -> None:
    # Relationship row properties are intentionally loaded exactly as exported.
    for filename, (src_label, dst_label, rel_type) in REL_CONFIG.items():
        df = pd.read_csv(current / filename, dtype=object, keep_default_na=False)
        if df.empty:
            continue
        rows = df.to_dict(orient="records")
        query = f"""
        UNWIND $rows AS row
        MATCH (s:`{src_label}`), (d:`{dst_label}`)
        WHERE s[{_key_for_label(src_label)!r}] = row['Source Id']
          AND d[{_key_for_label(dst_label)!r}] = row['Destination Id']
        MERGE (s)-[r:`{rel_type}`]->(d)
        SET r += row
        """
        session.run(query, rows=rows).consume()


def _key_for_label(label: str) -> str:
    for _, (candidate_label, key, _) in NODE_CONFIG.items():
        if candidate_label == label:
            return key
    raise KeyError(label)


def load_from_env(store_root: Path) -> None:
    load_canonical(
        store_root,
        uri=os.environ["NEO4J_URI"],
        user=os.environ["NEO4J_USER"],
        password=os.environ["NEO4J_PASSWORD"],
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
    )
