import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests


# ======================================================================
# CONFIG
# ======================================================================

CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data"
UNIPROT_API = "https://rest.uniprot.org/uniprotkb"

HUMAN_ORGANISM = "Homo sapiens"

MAX_WORKERS = 12

ALLOWED_TARGET_TYPES = {
    "SINGLE PROTEIN",
    "PROTEIN COMPLEX",
    "PROTEIN FAMILY",
}

OUTPUT_COLUMNS = [
    "target_chembl_id",
    "uniprot_id",
    "target_name",
    "target_organism",
    "target_type",
]


# ======================================================================
# SESSION
# ======================================================================

session = requests.Session()
session.headers.update({
    "User-Agent": "Ayurvedic-Phytochemical-Target-Collector/1.0",
    "Accept": "application/json",
})


# ======================================================================
# HELPERS
# ======================================================================

def clean(value):
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    value = str(value).strip()

    if not value:
        return None

    return value


def clean_id(value):
    value = clean(value)
    if not value:
        return None
    return value.upper()


def request_json(url, params=None, retries=5):
    for attempt in range(retries):
        try:
            response = session.get(
                url,
                params=params,
                timeout=30,
            )

            if response.status_code == 200:
                return response.json()

            if response.status_code == 404:
                return None

            if response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }:
                wait = min(2 ** attempt, 30)
                print(
                    f"      HTTP {response.status_code}; "
                    f"retrying in {wait}s..."
                )
                time.sleep(wait)
                continue

            print(
                f"      HTTP {response.status_code}: {url}"
            )
            return None

        except requests.RequestException as exc:
            wait = min(2 ** attempt, 30)
            print(f"      Request error: {exc}")

            if attempt < retries - 1:
                print(f"      Retrying in {wait}s...")
                time.sleep(wait)

    return None


# ======================================================================
# TARGET / COMPONENT / UNIPROT LOOKUPS
# ======================================================================

def get_target(target_id):
    target_id = clean_id(target_id)
    if not target_id:
        return None

    data = request_json(
        f"{CHEMBL_API}/target/{target_id}.json"
    )
    if not data:
        return None

    target_type = clean(data.get("target_type"))
    if target_type not in ALLOWED_TARGET_TYPES:
        return None

    return {
        "target_chembl_id": target_id,
        "target_name": clean(data.get("pref_name")),
        "target_type": target_type,
        "target_components": data.get("target_components") or [],
    }


def get_component(component):
    component_id = component.get("component_id")
    if component_id is None:
        component_id = component.get("component_chembl_id")
    if component_id is None:
        return None

    try:
        component_id = int(component_id)
    except (TypeError, ValueError):
        return None

    data = request_json(
        f"{CHEMBL_API}/target_component/{component_id}.json"
    )
    if not data:
        return None

    accession = clean_id(data.get("accession"))
    if not accession:
        for synonym in data.get("target_component_synonyms", []) or []:
            if not isinstance(synonym, dict):
                continue
            candidate = clean_id(synonym.get("component_synonym"))
            if candidate and (
                candidate.startswith("P")
                or candidate.startswith("Q")
                or candidate.startswith("O")
                or candidate.startswith("A")
            ):
                accession = candidate
                break

    if not accession:
        return None

    return {
        "component_id": component_id,
        "accession": accession,
    }


def get_uniprot(accession):
    accession = clean_id(accession)
    if not accession:
        return None

    data = request_json(
        f"{UNIPROT_API}/{accession}.json"
    )
    if not data:
        return None

    organism = data.get("organism") or {}
    return {
        "accession": accession,
        "organism": clean(organism.get("scientificName")),
    }


def parallel_map(items, fn, workers=MAX_WORKERS):
    """Run independent HTTP lookups concurrently, preserving input order."""
    if not items:
        return []

    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        futures = {
            pool.submit(fn, item): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = None
                print(f"      Lookup error: {exc}")
    return results


def resolve_target_data(target_ids, cache):
    """
    Fast two-stage resolution:
      1. ChEMBL target records in parallel
      2. ChEMBL target components in parallel
      3. UniProt records in parallel

    Results are cached by target/component/accession so reruns avoid
    already-resolved API records.
    """
    target_cache = cache.setdefault("targets", {})
    component_cache = cache.setdefault("components", {})
    uniprot_cache = cache.setdefault("uniprot", {})

    # --------------------------------------------------------------
    # 1. Targets
    # --------------------------------------------------------------
    missing_targets = [
        tid for tid in target_ids
        if tid not in target_cache
    ]

    fetched = parallel_map(
        missing_targets,
        get_target,
    )
    for tid, result in zip(missing_targets, fetched):
        target_cache[tid] = result

    valid_targets = {
        tid: target_cache.get(tid)
        for tid in target_ids
        if target_cache.get(tid)
    }

    # --------------------------------------------------------------
    # 2. Components
    # --------------------------------------------------------------
    components_to_fetch = {}
    for target in valid_targets.values():
        for component in target.get("target_components", []) or []:
            cid = component.get("component_id")
            if cid is None:
                cid = component.get("component_chembl_id")
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                continue
            key = str(cid)
            if key not in component_cache:
                components_to_fetch[key] = component

    missing_components = list(components_to_fetch.items())
    component_results = parallel_map(
        [component for _, component in missing_components],
        get_component,
    )
    for (key, _), result in zip(missing_components, component_results):
        component_cache[key] = result

    # --------------------------------------------------------------
    # 3. UniProt
    # --------------------------------------------------------------
    accessions = set()
    for target in valid_targets.values():
        for component in target.get("target_components", []) or []:
            cid = component.get("component_id")
            if cid is None:
                cid = component.get("component_chembl_id")
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                continue
            component_data = component_cache.get(str(cid))
            if component_data:
                accession = component_data.get("accession")
                if accession:
                    accessions.add(accession)

    missing_accessions = [
        accession for accession in accessions
        if accession not in uniprot_cache
    ]

    uniprot_results = parallel_map(
        missing_accessions,
        get_uniprot,
    )
    for accession, result in zip(missing_accessions, uniprot_results):
        uniprot_cache[accession] = result

    return valid_targets, target_cache, component_cache, uniprot_cache


def build_target_row(target, component_cache, uniprot_cache):
    """Create one final target row while enforcing Homo sapiens only."""
    accessions = []
    components = target.get("target_components", []) or []

    for component in components:
        cid = component.get("component_id")
        if cid is None:
            cid = component.get("component_chembl_id")
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue

        component_data = component_cache.get(str(cid))
        if not component_data:
            continue

        accession = clean_id(component_data.get("accession"))
        if not accession:
            continue

        verified = uniprot_cache.get(accession)
        if verified:
            accessions.append(verified)

    unique = {}
    for item in accessions:
        unique[item["accession"]] = item
    accessions = list(unique.values())

    # Strict Homo sapiens rule.
    if not accessions:
        return None

    for item in accessions:
        if clean(item.get("organism")) != HUMAN_ORGANISM:
            return None

    return {
        "target_chembl_id": target.get("target_chembl_id"),
        "uniprot_id": ";".join(item["accession"] for item in accessions),
        "target_name": clean(target.get("target_name")),
        "target_organism": HUMAN_ORGANISM,
        "target_type": clean(target.get("target_type")),
    }

# ======================================================================
# BIOACTIVITY -> TARGET MAPPING
# ======================================================================

def checkpoint_activity_key(record):
    """
    MUST match the Bioactivity collector's current deduplication logic.

    The current Bioactivity collector prefers the exact ChEMBL activity
    identifier. This lets us reconstruct the exact BA_XXXXXX numbering
    without putting target_id back into Bioactivities.xlsx.
    """

    activity_id = clean_id(
        record.get(
            "chembl_activity_id"
        )
    )

    if activity_id:
        return (
            "CHEMBL_ACTIVITY",
            activity_id,
        )

    return (
        clean_id(
            record.get(
                "phytochemical_chembl_id"
            )
        ),
        clean(
            record.get(
                "standard_type"
            )
        ),
        clean(
            record.get(
                "standard_relation"
            )
        ),
        clean(
            record.get(
                "standard_value"
            )
        ),
        clean(
            record.get(
                "standard_units"
            )
        ),
        clean_id(
            record.get(
                "target_id"
            )
        ),
    )


def load_checkpoint_records(
    checkpoint_file,
):
    path = Path(
        checkpoint_file
    )

    if not path.exists():
        raise ValueError(
            f"Bioactivity checkpoint not found: "
            f"{path}"
        )

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:
            payload = json.load(f)
    except Exception as exc:
        raise ValueError(
            f"Could not read bioactivity checkpoint: "
            f"{exc}"
        )

    records = payload.get(
        "records",
        [],
    )

    if not isinstance(records, list):
        raise ValueError(
            "Bioactivity checkpoint has invalid "
            "'records' data."
        )

    unique = {}

    for record in records:
        if not isinstance(record, dict):
            continue

        key = checkpoint_activity_key(
            record
        )

        if key not in unique:
            unique[key] = record

    return list(
        unique.values()
    )


def build_bioactivity_target_map(
    bioactivity_file,
    checkpoint_file,
):
    bio = pd.read_excel(
        bioactivity_file
    )

    bio.columns = [
        str(c).strip().lower()
        for c in bio.columns
    ]

    required = {
        "bioactivity_id",
    }

    missing = (
        required
        - set(bio.columns)
    )

    if missing:
        raise ValueError(
            "Missing required columns: "
            + ", ".join(
                sorted(missing)
            )
        )

    records = load_checkpoint_records(
        checkpoint_file
    )

    if len(records) != len(bio):
        raise ValueError(
            "Bioactivity/checkpoint mismatch: "
            f"Bioactivities.xlsx has {len(bio)} rows, "
            f"but the checkpoint reconstructs "
            f"{len(records)} unique activities. "
            "Use the checkpoint generated for this "
            "Bioactivities.xlsx."
        )

    mapping = {}

    for number, record in enumerate(
        records,
        start=1,
    ):
        bioactivity_id = (
            f"BA_{number:06d}"
        )

        # The Excel file is the canonical list of
        # Bioactivity IDs. Verify numbering rather
        # than silently assuming it.
        if number - 1 >= len(bio):
            raise ValueError(
                "Bioactivity ID reconstruction "
                "exceeded Bioactivities.xlsx rows."
            )

        excel_id = clean_id(
            bio.iloc[
                number - 1
            ]["bioactivity_id"]
        )

        if excel_id != bioactivity_id:
            raise ValueError(
                "Bioactivity ID/checkpoint ordering "
                "mismatch at row "
                f"{number}: Excel has "
                f"{excel_id}, expected "
                f"{bioactivity_id}."
            )

        target_id = clean_id(
            record.get(
                "target_id"
            )
        )

        if not target_id:
            raise ValueError(
                f"{bioactivity_id} has no target_id "
                "in the bioactivity checkpoint."
            )

        mapping[
            bioactivity_id
        ] = target_id

    return bio, mapping


# ======================================================================
# BUILD TARGETS + MEASURED_ON
# ======================================================================

def build(
    bioactivity_file,
    targets_file,
    relationships_file,
    checkpoint_file,
):
    bio, bio_target_map = (
        build_bioactivity_target_map(
            bioactivity_file,
            checkpoint_file,
        )
    )

    target_ids = []
    seen = set()

    for bioactivity_id in bio[
        "bioactivity_id"
    ]:
        bioactivity_id = clean_id(
            bioactivity_id
        )

        target_id = bio_target_map.get(
            bioactivity_id
        )

        if not target_id:
            continue

        if target_id in seen:
            continue

        seen.add(
            target_id
        )
        target_ids.append(
            target_id
        )

    print("=" * 72)
    print(
        "CHEMBL + UNIPROT TARGET COLLECTOR"
    )
    print("=" * 72)

    print(
        f"\nLoading: {bioactivity_file}"
    )

    print(
        f"Bioactivity rows: {len(bio)}"
    )

    print(
        f"Unique targets: {len(target_ids)}"
    )

    # Persistent target/component/UniProt cache lives beside the output.
    cache_file = Path(targets_file).with_name("target_uniprot_cache.json")
    try:
        if cache_file.exists():
            with open(cache_file, "r", encoding="utf-8") as f:
                lookup_cache = json.load(f)
        else:
            lookup_cache = {}
    except Exception:
        lookup_cache = {}

    valid_targets, target_cache, component_cache, uniprot_cache = resolve_target_data(
        target_ids,
        lookup_cache,
    )

    target_rows = []
    skipped_nonhuman = 0
    skipped_unresolved = 0

    for index, target_id in enumerate(target_ids, start=1):
        print(f"[{index}/{len(target_ids)}] {target_id}")
        target = valid_targets.get(target_id)
        if not target:
            print("    Target could not be resolved")
            skipped_unresolved += 1
            continue

        row = build_target_row(
            target,
            component_cache,
            uniprot_cache,
        )
        if not row:
            print("    SKIPPED: target is not Homo sapiens")
            skipped_nonhuman += 1
            continue

        print(f"    {row['target_name'] or 'NA'} | UniProt: {row['uniprot_id']}")
        target_rows.append(row)

    lookup_cache["targets"] = target_cache
    lookup_cache["components"] = component_cache
    lookup_cache["uniprot"] = uniprot_cache
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(lookup_cache, f, indent=2)
    except Exception as exc:
        print(f"Warning: could not save target cache: {exc}")

    targets_df = pd.DataFrame(
        target_rows,
        columns=OUTPUT_COLUMNS,
    )

    targets_df = (
        targets_df
        .drop_duplicates(
            subset=[
                "target_chembl_id"
            ]
        )
        .reset_index(
            drop=True
        )
    )

    # ------------------------------------------------------------
    # MEASURED_ON
    # ------------------------------------------------------------

    print()
    print("=" * 72)
    print(
        "BUILDING MEASURED_ON RELATIONSHIPS"
    )
    print("=" * 72)

    valid_targets = set(
        targets_df[
            "target_chembl_id"
        ].map(clean_id)
    )

    relationship_rows = []

    for bioactivity_id, target_id in (
        bio_target_map.items()
    ):
        target_id = clean_id(
            target_id
        )

        if target_id not in valid_targets:
            continue

        relationship_rows.append({
            "Source":
                "Bioactivity",
            "Source Id":
                bioactivity_id,
            "Type":
                "MEASURED_ON",
            "Destination":
                "Target",
            "Destination Id":
                target_id,
            "Label":
                "measured_on",
            "Description":
                "Associated bioactivity was measured "
                "on respective target",
        })

    relationships_df = pd.DataFrame(
        relationship_rows,
        columns=[
            "Source",
            "Source Id",
            "Type",
            "Destination",
            "Destination Id",
            "Label",
            "Description",
        ],
    ).drop_duplicates(
        subset=[
            "Source Id",
            "Destination Id",
            "Type",
        ]
    ).reset_index(
        drop=True
    )

    targets_df.to_excel(
        targets_file,
        index=False,
    )

    relationships_df.to_excel(
        relationships_file,
        index=False,
    )

    targets_with_uniprot = (
        targets_df[
            "uniprot_id"
        ]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
        .sum()
    )

    targets_with_name = (
        targets_df[
            "target_name"
        ]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
        .sum()
    )

    print()
    print("=" * 72)
    print("FINAL RESULT")
    print("=" * 72)

    print(
        f"Bioactivities: {len(bio)}"
    )

    print(
        f"Unique targets: "
        f"{len(targets_df)}"
    )

    print(
        f"Targets with UniProt: "
        f"{targets_with_uniprot}/"
        f"{len(targets_df)}"
    )

    print(
        f"Targets with name: "
        f"{targets_with_name}/"
        f"{len(targets_df)}"
    )

    print(
        f"MEASURED_ON relationships: "
        f"{len(relationships_df)}"
    )

    print(f"Skipped non-human targets: {skipped_nonhuman}")
    print(f"Skipped unresolved targets: {skipped_unresolved}")

    print()
    print(
        f"Target file:\n  "
        f"{targets_file}"
    )

    print(
        f"Relationship file:\n  "
        f"{relationships_file}"
    )

    if not targets_df.empty:
        print()
        print("=" * 72)
        print("TARGET PREVIEW")
        print("=" * 72)
        print(
            targets_df.head(10).to_string(
                index=False
            )
        )

    if not relationships_df.empty:
        print()
        print("=" * 72)
        print("RELATIONSHIP PREVIEW")
        print("=" * 72)
        print(
            relationships_df.head(10).to_string(
                index=False
            )
        )

    print("\nDone.")


# ======================================================================
# CLI
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Build Target nodes and MEASURED_ON "
            "relationships without requiring "
            "target_id in Bioactivities.xlsx."
        )
    )

    parser.add_argument(
        "--file",
        required=True,
        help="Bioactivities.xlsx",
    )

    parser.add_argument(
        "--targets",
        required=True,
        help="Targets.xlsx",
    )

    parser.add_argument(
        "--relationships",
        required=True,
        help=(
            "Bioactivity_Target_Relationships.xlsx"
        ),
    )

    parser.add_argument(
        "--checkpoint",
        required=True,
        help=(
            "bioactivity_checkpoint.json generated "
            "for the same Bioactivities.xlsx"
        ),
    )

    args = parser.parse_args()

    build(
        args.file,
        args.targets,
        args.relationships,
        args.checkpoint,
    )


if __name__ == "__main__":
    main()