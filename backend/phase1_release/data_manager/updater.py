from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from .identity import bioactivity_signature, node_key, identifier
from .store import ALL_GRAPH_FILES, NODE_FILES, REL_FILES, CanonicalStore, utc_stamp

REL_REQUIRED = ["Source Id", "Destination Id"]


def _read_csv(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns or [])
    return pd.read_csv(path, dtype=object, keep_default_na=False)


def _write_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".csv", dir=path.parent, delete=False, encoding="utf-8", newline="") as tmp:
        tmp_path = Path(tmp.name)
        df.to_csv(tmp, index=False)
    os.replace(tmp_path, path)


def _assert_columns(df: pd.DataFrame, path: Path, required: list[str]) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} missing required columns: {missing}")


def _row_dicts(df: pd.DataFrame):
    for row in df.to_dict(orient="records"):
        yield row


def _next_ba_id(existing: set[str]) -> str:
    highest = 0
    for value in existing:
        m = re.fullmatch(r"BA_(\d+)", str(value).strip())
        if m:
            highest = max(highest, int(m.group(1)))
    width = max(6, len(str(highest + 1)))
    return f"BA_{highest + 1:0{width}d}"


def _merge_generic_nodes(current: pd.DataFrame, incoming: pd.DataFrame, label: str, key_column: str) -> tuple[pd.DataFrame, int, int, dict[str, str]]:
    if incoming.empty:
        return current, 0, 0, {}
    _assert_columns(incoming, Path(label), [key_column])
    if current.empty and len(current.columns) == 0:
        current = pd.DataFrame(columns=incoming.columns)
    else:
        # Canonical schema is authoritative. Missing incoming columns are blank; extra incoming columns are ignored.
        for col in current.columns:
            if col not in incoming.columns:
                incoming[col] = ""
        incoming = incoming[current.columns]
    existing_by_identity: dict[str, str] = {}
    for row in _row_dicts(current):
        ident = node_key(label, row)
        if ident:
            existing_by_identity[ident] = str(row[key_column]).strip()
    added = 0
    rows = []
    incoming_to_canonical: dict[str, str] = {}
    for row in _row_dicts(incoming):
        raw_key = str(row[key_column]).strip()
        ident = node_key(label, row)
        if not ident:
            raise ValueError(f"Blank identity key in {label}: {row}")
        canonical_key = existing_by_identity.get(ident)
        if canonical_key is None:
            canonical_key = raw_key
            existing_by_identity[ident] = canonical_key
            rows.append(row)
            added += 1
        incoming_to_canonical[raw_key] = canonical_key
    if rows:
        current = pd.concat([current, pd.DataFrame(rows, columns=current.columns)], ignore_index=True)
    return current, added, 0, incoming_to_canonical


def update_canonical_store(input_dir: Path, store_root: Path, dry_run: bool = False) -> dict[str, Any]:
    """Merge one master output folder into the canonical CSV store.

    Conservative Phase-1 policy:
    - existing canonical node rows win; no field overwrites
    - node identity uses stable schema keys
    - Bioactivity identity uses exact measurement + target + document
    - relationships are unioned, with BA IDs remapped to canonical IDs
    - no collector/checkpoint files are touched
    """
    input_dir = Path(input_dir)
    store = CanonicalStore(Path(store_root))
    source = {name: input_dir / name for name in ALL_GRAPH_FILES}
    current = {name: _read_csv(store.current / name) for name in ALL_GRAPH_FILES}
    incoming = {name: _read_csv(source[name]) for name in ALL_GRAPH_FILES}

    # First merge non-bioactivity nodes.
    node_stats: dict[str, dict[str, int]] = {}
    generic = [
        ("Plant", "Plant.csv", "scientific_name"),
        ("PlantPart", "PlantPart.csv", "plant_part_id"),
        ("Phytochemicals", "Phytochemicals.csv", "inchi_key"),
        ("TherapeuticUses", "TherapeuticUses.csv", "mesh_id"),
        ("Targets", "Targets.csv", "target_chembl_id"),
        ("Documents", "Documents.csv", "document_chembl_id"),
    ]
    id_maps: dict[str, dict[str, str]] = {}
    for label, filename, key_col in generic:
        if incoming[filename].empty:
            id_maps[label] = {}
            continue
        merged, added, _, id_map = _merge_generic_nodes(current[filename], incoming[filename], label, key_col)
        current[filename] = merged
        id_maps[label] = id_map
        node_stats[label] = {"added": added, "total": len(merged)}

    # Build incoming BA -> target/document mapping before BA canonicalization.
    measured = incoming["MEASURED_ON.csv"]
    reported = incoming["REPORTED_IN.csv"]
    _assert_columns(measured, Path("MEASURED_ON.csv"), REL_REQUIRED)
    _assert_columns(reported, Path("REPORTED_IN.csv"), REL_REQUIRED)
    target_of = {identifier(r["Source Id"]): r["Destination Id"] for r in _row_dicts(measured)}
    document_of = {identifier(r["Source Id"]): r["Destination Id"] for r in _row_dicts(reported)}

    # Canonical Bioactivity lookup by semantic identity.
    bio = current["Bioactivities.csv"].copy()
    incoming_bio = incoming["Bioactivities.csv"].copy()
    if bio.empty and len(bio.columns) == 0 and not incoming_bio.empty:
        bio = pd.DataFrame(columns=incoming_bio.columns)
    _assert_columns(incoming_bio, Path("Bioactivities.csv"), [
        "bioactivity_id", "standard_type", "standard_relation", "standard_value", "standard_units"
    ])
    measured_current = current["MEASURED_ON.csv"]
    reported_current = current["REPORTED_IN.csv"]
    current_target_of = {identifier(r["Source Id"]): r["Destination Id"] for r in _row_dicts(measured_current)}
    current_document_of = {identifier(r["Source Id"]): r["Destination Id"] for r in _row_dicts(reported_current)}
    canonical_by_sig: dict[tuple[str, ...], str] = {}
    for row in _row_dicts(bio):
        # Existing canonical rows require matching relationships if they are to participate in semantic matching.
        bid = identifier(row["bioactivity_id"])
        target = current_target_of.get(bid, "")
        document = current_document_of.get(bid, "")
        sig = bioactivity_signature(row, target, document)
        if sig is not None:
            canonical_by_sig[sig] = str(row["bioactivity_id"]).strip()

    incoming_ba_map: dict[str, str] = {}
    new_bio_rows = []
    existing_ids = {str(x).strip() for x in bio["bioactivity_id"].tolist()} if not bio.empty else set()
    added_bio = 0
    for row in _row_dicts(incoming_bio):
        incoming_id = str(row["bioactivity_id"]).strip()
        sig = bioactivity_signature(row, target_of.get(identifier(incoming_id), ""), document_of.get(identifier(incoming_id), ""))
        if sig is None:
            raise ValueError(f"Bioactivity {incoming_id} cannot be canonicalized: target/document missing")
        canonical_id = canonical_by_sig.get(sig)
        if canonical_id is None:
            canonical_id = _next_ba_id(existing_ids)
            existing_ids.add(canonical_id)
            canonical_by_sig[sig] = canonical_id
            new_row = dict(row)
            new_row["bioactivity_id"] = canonical_id
            new_bio_rows.append(new_row)
            added_bio += 1
        incoming_ba_map[incoming_id] = canonical_id
    if new_bio_rows:
        bio = pd.concat([bio, pd.DataFrame(new_bio_rows, columns=bio.columns)], ignore_index=True)
    current["Bioactivities.csv"] = bio
    node_stats["Bioactivities"] = {"added": added_bio, "total": len(bio), "source_rows": len(incoming_bio)}

    # Remap all relationship endpoints to canonical stored identifiers, then union exact rows.
    endpoint_maps = {
        "Plant": id_maps.get("Plant", {}),
        "PlantPart": id_maps.get("PlantPart", {}),
        "Phytochemicals": id_maps.get("Phytochemicals", {}),
        "TherapeuticUses": id_maps.get("TherapeuticUses", {}),
        "Targets": id_maps.get("Targets", {}),
        "Documents": id_maps.get("Documents", {}),
    }
    relationship_endpoints = {
        "HAS_PART.csv": ("Plant", "PlantPart"),
        "CONTAINS.csv": ("PlantPart", "Phytochemicals"),
        "USED_FOR.csv": ("PlantPart", "TherapeuticUses"),
        "HAS_BIOACTIVITY.csv": ("Phytochemicals", "Bioactivities"),
        "MEASURED_ON.csv": ("Bioactivities", "Targets"),
        "REPORTED_IN.csv": ("Bioactivities", "Documents"),
    }
    rel_stats: dict[str, dict[str, int]] = {}
    for filename in REL_FILES:
        inc = incoming[filename].copy()
        cur = current[filename].copy()
        if inc.empty:
            rel_stats[filename] = {"added": 0, "total": len(cur), "source_rows": 0}
            continue
        _assert_columns(inc, Path(filename), REL_REQUIRED)
        if cur.empty and len(cur.columns) == 0:
            cur = pd.DataFrame(columns=inc.columns)
        else:
            # Canonical relationship schema is authoritative. Missing incoming columns are blank; extras are ignored.
            for col in cur.columns:
                if col not in inc.columns:
                    inc[col] = ""
            inc = inc[cur.columns]
        src_entity, dst_entity = relationship_endpoints[filename]
        src_map = incoming_ba_map if src_entity == "Bioactivities" else endpoint_maps[src_entity]
        dst_map = incoming_ba_map if dst_entity == "Bioactivities" else endpoint_maps[dst_entity]
        inc["Source Id"] = inc["Source Id"].map(lambda x: src_map.get(str(x).strip(), str(x).strip()))
        inc["Destination Id"] = inc["Destination Id"].map(lambda x: dst_map.get(str(x).strip(), str(x).strip()))
        before = len(cur)
        combined = pd.concat([cur, inc], ignore_index=True).drop_duplicates().reset_index(drop=True)
        added = len(combined) - before
        current[filename] = combined
        rel_stats[filename] = {"added": added, "total": len(combined), "source_rows": len(inc)}

    # Mandatory invariants for relationships.
    for filename in ["HAS_BIOACTIVITY.csv", "MEASURED_ON.csv", "REPORTED_IN.csv"]:
        frame = current[filename]
        if frame.empty:
            continue
    # Validate every relationship endpoint against its canonical node table.
    node_keys = {
        "Plant": {str(x).strip() for x in current["Plant.csv"]["scientific_name"].tolist()} if "scientific_name" in current["Plant.csv"].columns else set(),
        "PlantPart": {str(x).strip() for x in current["PlantPart.csv"]["plant_part_id"].tolist()} if "plant_part_id" in current["PlantPart.csv"].columns else set(),
        "Phytochemicals": {str(x).strip() for x in current["Phytochemicals.csv"]["inchi_key"].tolist()} if "inchi_key" in current["Phytochemicals.csv"].columns else set(),
        "TherapeuticUses": {str(x).strip() for x in current["TherapeuticUses.csv"]["mesh_id"].tolist()} if "mesh_id" in current["TherapeuticUses.csv"].columns else set(),
        "Bioactivities": {str(x).strip() for x in current["Bioactivities.csv"]["bioactivity_id"].tolist()} if "bioactivity_id" in current["Bioactivities.csv"].columns else set(),
        "Targets": {str(x).strip() for x in current["Targets.csv"]["target_chembl_id"].tolist()} if "target_chembl_id" in current["Targets.csv"].columns else set(),
        "Documents": {str(x).strip() for x in current["Documents.csv"]["document_chembl_id"].tolist()} if "document_chembl_id" in current["Documents.csv"].columns else set(),
    }
    for filename, (src_entity, dst_entity) in relationship_endpoints.items():
        frame = current[filename]
        if frame.empty:
            continue
        bad_src = [x for x in frame["Source Id"] if str(x).strip() not in node_keys[src_entity]]
        bad_dst = [x for x in frame["Destination Id"] if str(x).strip() not in node_keys[dst_entity]]
        if bad_src or bad_dst:
            raise AssertionError(f"{filename} endpoint validation failed: bad source={bad_src[:5]}, bad destination={bad_dst[:5]}")

    # Every BA referenced must exist.
    known_ba = node_keys["Bioactivities"]
    for filename in ["HAS_BIOACTIVITY.csv", "MEASURED_ON.csv", "REPORTED_IN.csv"]:
        frame = current[filename]
        if frame.empty:
            continue
        refs = frame["Destination Id"] if filename == "HAS_BIOACTIVITY.csv" else frame["Source Id"]
        missing = [x for x in refs if str(x).strip() not in known_ba]
        if missing:
            raise AssertionError(f"{filename} contains unknown Bioactivity IDs: {missing[:5]}")

    source_has = incoming["HAS_BIOACTIVITY.csv"]
    if not source_has.empty:
        # Count distinct source-side associations in the incoming batch; all must survive in canonical union.
        source_pairs = {(str(r["Source Id"]).strip(), incoming_ba_map.get(str(r["Destination Id"]).strip(), str(r["Destination Id"]).strip())) for r in _row_dicts(source_has)}
        canonical_pairs = {(str(r["Source Id"]).strip(), str(r["Destination Id"]).strip()) for r in _row_dicts(current["HAS_BIOACTIVITY.csv"])}
        missing_pairs = sorted(source_pairs - canonical_pairs)
        if missing_pairs:
            raise AssertionError(f"HAS_BIOACTIVITY association preservation failed: {missing_pairs[:5]}")

    changed_files = []
    if not dry_run:
        snapshot_path = store.snapshot()
        for filename in ALL_GRAPH_FILES:
            _write_atomic(current[filename], store.current / filename)
            changed_files.append(filename)
    else:
        snapshot_path = None

    report = {
        "run_id": utc_stamp(),
        "input_dir": str(input_dir),
        "store_root": str(store.root),
        "dry_run": dry_run,
        "node_stats": node_stats,
        "relationship_stats": rel_stats,
        "changed_files": changed_files,
        "snapshot": str(snapshot_path) if snapshot_path else None,
    }
    if not dry_run:
        manifest = Path(store.root).parent / "manifests" / f"{report['run_id']}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Merge one collector output directory into the canonical store.")
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("--store", type=Path, default=Path("data/canonical"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = update_canonical_store(args.input_dir, args.store, dry_run=args.dry_run)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
