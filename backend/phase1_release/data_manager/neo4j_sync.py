from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd

from .store import ALL_GRAPH_FILES, NODE_FILES, REL_FILES

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

STATE_FILE_NAME = ".neo4j_sync_state.json"


def _canonical_root(store_root: Path) -> Path:
    return Path(store_root) / "current"


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_digest(store_root: Path) -> str:
    current = _canonical_root(store_root)
    h = hashlib.sha256()
    for name in ALL_GRAPH_FILES:
        path = current / name
        if not path.exists():
            h.update(name.encode())
            h.update(b"<missing>")
            continue
        h.update(name.encode())
        h.update(_file_digest(path).encode())
    return h.hexdigest()


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=object, keep_default_na=False)


def _rows(path: Path) -> list[dict[str, Any]]:
    df = _read_csv(path)
    if df.empty:
        return []
    return df.to_dict(orient="records")


def _key_for_label(label: str) -> str:
    for _, (candidate_label, key, _) in NODE_CONFIG.items():
        if candidate_label == label:
            return key
    raise KeyError(label)


def _validate_canonical(current: Path) -> None:
    missing_files = [name for name in ALL_GRAPH_FILES if not (current / name).exists()]
    if missing_files:
        raise RuntimeError(f"Canonical store is incomplete; missing files: {missing_files}")

    node_keys: dict[str, set[str]] = {}
    for filename, (label, key, allowed_fields) in NODE_CONFIG.items():
        df = _read_csv(current / filename)
        if key not in df.columns:
            raise RuntimeError(f"{filename} missing key column {key!r}")
        keys = {str(v).strip() for v in df[key].tolist() if str(v).strip()}
        if len(keys) != len(df):
            raise RuntimeError(f"{filename} contains blank or duplicate {key!r} values")
        node_keys[label] = keys

    for filename, (src_label, dst_label, _) in REL_CONFIG.items():
        df = _read_csv(current / filename)
        if df.empty:
            continue
        required = {"Source Id", "Destination Id"}
        if not required.issubset(df.columns):
            raise RuntimeError(f"{filename} missing relationship columns {sorted(required - set(df.columns))}")
        bad_src = [x for x in df["Source Id"] if str(x).strip() not in node_keys[src_label]]
        bad_dst = [x for x in df["Destination Id"] if str(x).strip() not in node_keys[dst_label]]
        if bad_src or bad_dst:
            raise RuntimeError(
                f"{filename} has invalid endpoints: "
                f"source={bad_src[:5]}, destination={bad_dst[:5]}"
            )


def _verify_existing_schema(session) -> None:
    """Verify the existing Neo4j model without creating or altering schema.

    The Neo4j instance is treated as pre-initialized. This function is
    deliberately read-only: it checks that each model key property is backed
    by an existing node-key/unique constraint on the expected label.
    """
    result = session.run("SHOW CONSTRAINTS")
    constraints = result.data()

    def normalized_type(value: object) -> str:
        return "_".join(str(value or "").upper().replace("-", "_").split())

    for filename, (label, key, _) in NODE_CONFIG.items():
        found = False
        for c in constraints:
            c_name = str(c.get("name") or "")
            c_type = normalized_type(c.get("type"))
            c_schema = c.get("properties") or c.get("propertyKeys") or c.get("property_key_names") or []
            c_schema = [str(x) for x in c_schema]
            labels = c.get("labelsOrTypes") or c.get("labels_or_types") or []
            labels = [str(x) for x in labels]
            if (label in labels and key in c_schema and c_type in {"NODE_KEY", "UNIQUENESS", "UNIQUE"}):
                found = True
                break
            # Neo4j versions may omit detailed schema fields from SHOW CONSTRAINTS.
            # Fall back to the model's canonical constraint name when present.
            expected_name = f"{_safe_constraint_name(key)}_{_safe_constraint_name(label)}_key"
            if c_name == expected_name and c_type in {"NODE_KEY", "UNIQUENESS", "UNIQUE"}:
                found = True
                break
        if not found:
            raise RuntimeError(
                f"Required existing Neo4j key constraint was not found for "
                f"{label}.{key}. No schema changes were attempted."
            )


def _safe_constraint_name(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value).strip("_")


def _load_nodes(session, current: Path) -> dict[str, int]:
    stats: dict[str, int] = {}
    for filename, (label, key, allowed_fields) in NODE_CONFIG.items():
        df = _read_csv(current / filename)
        if df.empty:
            stats[label] = 0
            continue
        field_names = [f for f in allowed_fields if f in df.columns]
        rows = []
        for row in df.to_dict(orient="records"):
            item = {k: row[k] for k in field_names}
            if not str(item.get(key, "")).strip():
                raise RuntimeError(f"Blank key in {filename}: {item}")
            rows.append(item)
        query = f"""
        UNWIND $rows AS row
        MERGE (n:`{label}` {{{key}: row[{key!r}]}})
        SET n += row
        """
        session.run(query, rows=rows).consume()
        stats[label] = len(rows)
    return stats


def _load_relationships(session, current: Path) -> dict[str, int]:
    stats: dict[str, int] = {}
    for filename, (src_label, dst_label, rel_type) in REL_CONFIG.items():
        df = _read_csv(current / filename)
        if df.empty:
            stats[filename] = 0
            continue
        rows = df.to_dict(orient="records")
        src_key = _key_for_label(src_label)
        dst_key = _key_for_label(dst_label)
        query = f"""
        UNWIND $rows AS row
        MATCH (s:`{src_label}` {{{src_key}: row['Source Id']}})
        MATCH (d:`{dst_label}` {{{dst_key}: row['Destination Id']}})
        MERGE (s)-[r:`{rel_type}`]->(d)
        SET r += row
        """
        session.run(query, rows=rows).consume()
        stats[filename] = len(rows)
    return stats


def sync_once(store_root: Path, uri: str, user: str, password: str, database: str = "neo4j") -> dict[str, Any]:
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("Install the neo4j package before syncing: pip install neo4j") from exc

    store_root = Path(store_root)
    current = _canonical_root(store_root)
    _validate_canonical(current)
    digest = canonical_digest(store_root)

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            _verify_existing_schema(session)
            node_stats = _load_nodes(session, current)
            rel_stats = _load_relationships(session, current)
    finally:
        driver.close()

    return {
        "digest": digest,
        "database": database,
        "nodes_loaded": node_stats,
        "relationships_loaded": rel_stats,
        "canonical_root": str(current),
    }


def _state_path(store_root: Path) -> Path:
    return _canonical_root(store_root) / STATE_FILE_NAME


def _read_state(store_root: Path) -> dict[str, Any]:
    path = _state_path(store_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_state(store_root: Path, payload: dict[str, Any]) -> None:
    _state_path(store_root).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def watch(store_root: Path, uri: str, user: str, password: str, database: str, interval: float) -> None:
    print(f"Watching {_canonical_root(store_root)} for canonical changes...")
    while True:
        try:
            digest = canonical_digest(store_root)
            state = _read_state(store_root)
            if state.get("digest") != digest:
                report = sync_once(store_root, uri, user, password, database)
                _write_state(store_root, {
                    "digest": report["digest"],
                    "last_sync": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                print(json.dumps(report, indent=2))
        except Exception as exc:
            print(f"SYNC ERROR: {exc}")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync canonical Phase-1 CSV data into Neo4j.")
    parser.add_argument("--store", type=Path, default=Path("data/canonical"))
    parser.add_argument("--once", action="store_true", help="Sync once and exit.")
    parser.add_argument("--watch", action="store_true", help="Watch canonical CSVs and sync whenever they change.")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI"))
    parser.add_argument("--user", default=os.getenv("NEO4J_USER"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    args = parser.parse_args()

    if not args.uri or not args.user or args.password is None:
        parser.error("Provide --uri/--user/--password or set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD.")
    if not args.once and not args.watch:
        args.once = True

    if args.watch:
        watch(args.store, args.uri, args.user, args.password, args.database, args.interval)
    else:
        report = sync_once(args.store, args.uri, args.user, args.password, args.database)
        _write_state(args.store, {
            "digest": report["digest"],
            "last_sync": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
