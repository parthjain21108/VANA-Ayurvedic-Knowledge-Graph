
#!/usr/bin/env python3
"""
MASTER AYURVEDIC KNOWLEDGE-GRAPH COLLECTOR

This is an ORCHESTRATOR. It does not replace the collection logic in
the project. It runs the existing finalized collectors/builders in
their dependency order and converts their outputs into the final
Neo4j node/relationship sheets.

INPUT:
    A TXT file containing one plant scientific name per line.

FULL RUN:
    python master_collector.py --file test_plants.txt

ONE-PLANT TEST:
    python master_collector.py --plant "Acacia leucophloea"

OPTIONAL OUTPUT DIRECTORY:
    python master_collector.py --plant "Acacia leucophloea" --output test_output

FINAL NODE FILES:
    Plant.xlsx
    PlantPart.xlsx
    Phytochemicals.xlsx
    Bioactivities.xlsx
    Documents.xlsx
    Targets.xlsx
    Therapeutic_uses.xlsx

FINAL RELATIONSHIP FILES:
    HAS_PART.xlsx
    CONTAINS.xlsx
    HAS_BIOACTIVITY.xlsx
    REPORTED_IN.xlsx
    MEASURED_ON.xlsx
    USED_FOR.xlsx

IMPORTANT:
    The document stage deliberately runs:
        1. documents/document_collector.py
        2. doc2rel/bioactivity_document_mapper.py

    The document collector creates/updates Documents.xlsx and the
    Bioactivity_Document_Relationships.xlsx relationship directly.
    That relationship is copied to the final REPORTED_IN.xlsx file.

The script uses the actual CLI interfaces present in the project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------
# PROJECT ROOT
# ---------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------
# EXISTING PROJECT SCRIPTS
# ---------------------------------------------------------------------

# ONLY CHANGE:
# main.py -> plant_collector.py
PLANT_MAIN = ROOT / "plant detail" / "plant_collector.py"

PLANT_PART = ROOT / "plant part" / "plant_part_collector.py"
HAS_PART = ROOT / "haspart" / "haspart.py"

PHYTO_COLLECTOR = (
    ROOT / "phyto plus contains" / "phytochemical_collector.py"
)

PHYTO_BUILDER = ROOT / "phyto plus contains" / "builder.py"
CONTAINS_BUILDER = ROOT / "phyto plus contains" / "contains.py"

BIO_BUILDER = ROOT / "bioactivity" / "bioactivity_builder.py"

BIO_REL_BUILDER = (
    ROOT / "bioactivity" / "bioactivity_relationship_builder.py"
)

DOCUMENT_COLLECTOR = ROOT / "documents" / "document_collector.py"

TARGET_COLLECTOR = ROOT / "t.py"

THERAPEUTIC_COLLECTOR = (
    ROOT / "therapeutic use" / "therapeutic_use_collector.py"
)

THERAPEUTIC_BUILDER = (
    ROOT / "therapeutic use" / "therapeutic_use_builder.py"
)

USED_FOR_BUILDER = (
    ROOT / "used for" / "used_for_builder.py"
)


# ---------------------------------------------------------------------
# OUTPUT NAMES
# ---------------------------------------------------------------------

PLANT_FILE = "Plant.xlsx"
PLANT_PART_FILE = "PlantPart.xlsx"
HAS_PART_FILE = "HAS_PART.xlsx"

MAPPING_FILE = "Phytochemical_Mapping.xlsx"
PHYTO_FILE = "Phytochemicals.xlsx"
CONTAINS_FILE = "CONTAINS.xlsx"

BIO_FILE = "Bioactivities.xlsx"
BIO_REL_FILE = "Bioactivity_Relationships.xlsx"
BIO_CHECKPOINT_FILE = "bioactivity_checkpoint.json"
HAS_BIO_FILE = "HAS_BIOACTIVITY.xlsx"

DOCUMENT_FILE = "Documents.xlsx"
DOC_REL_FILE = "Bioactivity_Document_Relationships.xlsx"
REPORTED_IN_FILE = "REPORTED_IN.xlsx"

TARGET_FILE = "Targets.xlsx"
TARGET_REL_FILE = "Bioactivity_Target_Relationships.xlsx"
MEASURED_ON_FILE = "MEASURED_ON.xlsx"

THERAPEUTIC_MAPPING_FILE = "Therapeutic_Use_Mapping.xlsx"
THERAPEUTIC_MAPPING_RAW_FILE = "Therapeutic_Use_Mapping_raw.xlsx"
THERAPEUTIC_FILE = "Therapeutic_uses.xlsx"
THERAPEUTIC_RAW_FILE = "Therapeutic_uses_raw.xlsx"
THERAPEUTIC_COLLECTED_NODE_FILE = "Therapeutic_uses_collected.xlsx"
USED_FOR_FILE = "Used_For.xlsx"

CACHE_ENABLED = True
CACHE_DIR_NAME = ".master_cache"


# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------

def fail(message: str) -> None:
    raise RuntimeError(message)


def require_script(path: Path) -> None:
    if not path.exists():
        fail(f"Required project script was not found:\n{path}")


def require_file(path: Path) -> None:
    if not path.exists():
        fail(f"Expected output was not created:\n{path}")

    if path.stat().st_size == 0:
        fail(f"Expected output is empty:\n{path}")



def _fingerprint(path: Path) -> str:
    """Return a SHA-256 fingerprint for a file."""
    if not path.exists():
        return "MISSING"

    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def _flag_value(
    args: list[str],
    flag: str,
) -> Path | None:
    """Return the Path value following a CLI flag."""
    try:
        index = args.index(flag)
    except ValueError:
        return None

    if index + 1 >= len(args):
        return None

    return Path(args[index + 1])


def _cache_io(
    title: str,
    args: list[str],
) -> tuple[list[Path], list[Path]]:
    """Describe input/output files for a subprocess stage."""

    def one(flag: str) -> Path | None:
        return _flag_value(args, flag)

    if title == "STEP 1/10 - PLANT":
        return [one("--file")], [one("--output")]

    if title == "STEP 2/10 - PLANT PART":
        return [one("--file")], [one("--output")]

    if title == "STEP 3/10 - HAS_PART":
        return [
            one("--plant"),
            one("--plantpart"),
        ], [
            one("--output"),
        ]

    if title == "STEP 4/10 - PHYTOCHEMICAL MAPPING":
        return [one("--file")], [one("--output")]

    if title == "STEP 5A/10 - PHYTOCHEMICALS":
        return [one("--file")], [
            one("--output"),
            one("--checkpoint"),
        ]

    if title == "STEP 5B/10 - CONTAINS":
        return [
            one("--file"),
            one("--phytochemicals"),
        ], [
            one("--output"),
        ]

    if title == "STEP 6/10 - BIOACTIVITIES":
        return [one("--file")], [
            one("--output"),
            one("--checkpoint"),
        ]

    if title == "STEP 8/10 - DOCUMENTS + REPORTED_IN":
        return [
            one("--file"),
            one("--checkpoint"),
        ], [
            one("--documents"),
            one("--relationships"),
        ]

    if title == "STEP 9/10 - TARGETS + UNIPROT + MEASURED_ON":
        return [
            one("--file"),
            one("--checkpoint"),
        ], [
            one("--targets"),
            one("--relationships"),
        ]

    if title == "STEP 10/12 - THERAPEUTIC USE COLLECTION":
        return [one("--file")], [
            one("--output"),
            one("--mapping-output"),
        ]

    if title == "STEP 11/12 - THERAPEUTIC USE BUILD":
        return [one("--file")], [
            one("--output"),
            one("--mapping-output"),
        ]

    if title == "STEP 12/12 - USED_FOR":
        return [
            one("--file"),
            one("--mapping"),
        ], [
            one("--output"),
        ]

    return [], []


def _cache_marker(
    cache_root: Path,
    title: str,
) -> Path:
    safe = "".join(
        character
        if character.isalnum() or character in "-_"
        else "_"
        for character in title.lower()
    )

    directory = cache_root / CACHE_DIR_NAME
    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return directory / f"{safe}.json"


def _stage_signature(
    script: Path,
    args: list[str],
    inputs: list[Path],
) -> dict:
    stat = script.stat()

    return {
        "kind": "subprocess",
        "script": str(script.resolve()),
        "script_size": stat.st_size,
        "script_mtime_ns": stat.st_mtime_ns,
        "args": list(args),
        "inputs": {
            str(path.resolve()): _fingerprint(path)
            for path in inputs
            if path is not None
        },
    }


def _cache_hit(
    cache_root: Path,
    title: str,
    signature: dict,
    outputs: list[Path],
) -> bool:
    if not CACHE_ENABLED:
        return False

    if not outputs:
        return False

    if not all(
        path.exists() and path.stat().st_size > 0
        for path in outputs
    ):
        return False

    marker = _cache_marker(
        cache_root,
        title,
    )

    if not marker.exists():
        return False

    try:
        with marker.open(
            "r",
            encoding="utf-8",
        ) as handle:
            cached = json.load(handle)

    except Exception:
        return False

    # Compare the stage definition and input fingerprints.
    cached_base = {
        key: value
        for key, value in cached.items()
        if key != "outputs"
    }

    if cached_base != signature:
        return False

    # Also ensure the generated outputs have not been modified.
    cached_outputs = cached.get(
        "outputs",
        {},
    )

    current_outputs = {
        str(path.resolve()):
            _fingerprint(path)
        for path in outputs
    }

    return current_outputs == cached_outputs


def _save_cache(
    cache_root: Path,
    title: str,
    signature: dict,
    outputs: list[Path] | None = None,
) -> None:
    record = dict(signature)

    if outputs:
        record["outputs"] = {
            str(path.resolve()):
                _fingerprint(path)
            for path in outputs
            if path is not None
        }

    with _cache_marker(
        cache_root,
        title,
    ).open(
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(
            record,
            handle,
            indent=2,
            sort_keys=True,
        )


def _internal_cache_hit(
    cache_root: Path,
    title: str,
    signature: dict,
) -> bool:
    if not CACHE_ENABLED:
        return False

    marker = _cache_marker(
        cache_root,
        title,
    )

    if not marker.exists():
        return False

    try:
        with marker.open(
            "r",
            encoding="utf-8",
        ) as handle:
            return json.load(handle) == signature

    except Exception:
        return False


def run_internal(
    title: str,
    inputs: list[Path],
    outputs: list[Path],
    function,
    cache_root: Path,
    version: str = "1",
) -> None:
    """
    Run an internal master operation with stage caching.
    """

    inputs = [
        Path(x)
        for x in inputs
        if x is not None
    ]

    outputs = [
        Path(x)
        for x in outputs
        if x is not None
    ]

    output_paths = {
        str(x.resolve())
        for x in outputs
    }

    # Do not use an output file as an input fingerprint for the same
    # internal operation. Such a file is intentionally rewritten by
    # the operation and would invalidate its own cache immediately.
    signature_inputs = [
        x
        for x in inputs
        if str(x.resolve()) not in output_paths
    ]

    signature = {
        "kind": "internal",
        "title": title,
        "version": version,
        "inputs": {
            str(x.resolve()): _fingerprint(x)
            for x in signature_inputs
        },
    }

    if outputs:
        if _cache_hit(
            cache_root,
            title,
            signature,
            outputs,
        ):
            print(
                "CACHE HIT - skipping completed stage."
            )
            return
    else:
        if _internal_cache_hit(
            cache_root,
            title,
            signature,
        ):
            print(
                "CACHE HIT - skipping completed stage."
            )
            return

    function()

    for output in outputs:
        require_file(output)

    _save_cache(
        cache_root,
        title,
        signature,
        outputs,
    )


def run(
    title: str,
    script: Path,
    args: list[str],
    cwd: Path,
) -> None:
    """Run an external collector/builder with stage caching."""

    require_script(script)

    print()
    print("=" * 78)
    print(title)
    print("=" * 78)

    inputs, outputs = _cache_io(
        title,
        args,
    )

    inputs = [
        path for path in inputs
        if path is not None
    ]

    outputs = [
        path for path in outputs
        if path is not None
    ]

    cache_root = (
        outputs[0].parent
        if outputs
        else cwd
    )

    signature = _stage_signature(
        script,
        args,
        inputs,
    )

    if _cache_hit(
        cache_root,
        title,
        signature,
        outputs,
    ):
        print(
            "CACHE HIT - skipping completed stage."
        )
        print(
            "Use --no-cache to force this stage to run."
        )
        return

    command = [
        sys.executable,
        str(script),
        *args,
    ]

    print("Running:")
    print(
        " ".join(
            f'"{x}"' if " " in x else x
            for x in command
        )
    )
    print()

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        env=env,
    )

    if result.returncode != 0:
        fail(
            f"{title} failed with exit code "
            f"{result.returncode}."
        )

    for output in outputs:
        require_file(output)

    _save_cache(
        cache_root,
        title,
        signature,
        outputs,
    )


def read_excel(path: Path) -> pd.DataFrame:

    require_file(path)

    df = pd.read_excel(path)

    df.columns = [
        str(c).strip()
        for c in df.columns
    ]

    return df


def require_columns(
    path: Path,
    columns: list[str],
) -> pd.DataFrame:

    df = read_excel(path)

    missing = [
        column
        for column in columns
        if column not in df.columns
    ]

    if missing:
        fail(
            f"{path.name} is missing required columns:\n"
            + "\n".join(f"  - {x}" for x in missing)
            + "\n\nActual columns:\n"
            + "\n".join(f"  - {x}" for x in df.columns)
        )

    return df


def clean_id(value) -> str:

    if pd.isna(value):
        return ""

    return str(value).strip().upper()


def check_unique(
    df: pd.DataFrame,
    column: str,
    name: str,
) -> None:

    values = (
        df[column]
        .dropna()
        .astype(str)
        .str.strip()
    )

    duplicates = values[
        values.duplicated(keep=False)
    ]

    if not duplicates.empty:
        fail(
            f"{name}: duplicate values found in "
            f"'{column}': {duplicates.nunique()}."
        )


def validate_relationship(
    relationship_file: Path,
    source_file: Path,
    source_column: str,
    destination_file: Path,
    destination_column: str,
    relationship_name: str,
) -> None:

    rel = require_columns(
        relationship_file,
        [
            "Source",
            "Source Id",
            "Type",
            "Destination",
            "Destination Id",
            "Label",
            "Description",
        ],
    )

    source = require_columns(
        source_file,
        [source_column],
    )

    destination = require_columns(
        destination_file,
        [destination_column],
    )

    source_ids = {
        clean_id(value)
        for value in source[source_column]
        if clean_id(value)
    }

    destination_ids = {
        clean_id(value)
        for value in destination[destination_column]
        if clean_id(value)
    }

    rel_source_ids = {
        clean_id(value)
        for value in rel["Source Id"]
        if clean_id(value)
    }

    rel_destination_ids = {
        clean_id(value)
        for value in rel["Destination Id"]
        if clean_id(value)
    }

    missing_sources = rel_source_ids - source_ids

    missing_destinations = (
        rel_destination_ids - destination_ids
    )

    if missing_sources:
        fail(
            f"{relationship_name}: "
            f"{len(missing_sources)} source IDs "
            f"do not exist in the source node sheet."
        )

    if missing_destinations:
        fail(
            f"{relationship_name}: "
            f"{len(missing_destinations)} destination IDs "
            f"do not exist in the destination node sheet."
        )

    print(
        f"{relationship_name:<18} "
        f"{len(rel):>6} relationships | endpoints valid"
    )


def bioactivity_checkpoint_map(
    checkpoint_file: Path,
) -> dict[str, dict[str, str]]:
    """
    Recover the internal Bioactivity -> ChEMBL metadata from the
    Bioactivity checkpoint.

    Bioactivities.xlsx intentionally exposes only the final
    Bioactivity node fields, so relationship builders must use
    this internal metadata rather than expecting compound IDs
    inside the node table.
    """

    checkpoint_file = Path(checkpoint_file)

    if not checkpoint_file.exists():
        fail(
            "Bioactivity checkpoint was not found:\n"
            f"{checkpoint_file}"
        )

    import json

    with checkpoint_file.open(
        "r",
        encoding="utf-8",
    ) as f:
        checkpoint = json.load(f)

    records = checkpoint.get("records", [])

    def activity_key(record):
        activity_id = clean_id(
            record.get("chembl_activity_id")
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
            str(
                record.get("standard_type", "")
            ).strip(),
            str(
                record.get("standard_relation", "")
            ).strip(),
            str(
                record.get("standard_value", "")
            ).strip(),
            str(
                record.get("standard_units", "")
            ).strip(),
            clean_id(
                record.get("target_id")
            ),
        )

    unique = {}

    for record in records:
        key = activity_key(record)

        if key not in unique:
            unique[key] = record

    mapping = {}

    for number, record in enumerate(
        unique.values(),
        start=1,
    ):
        mapping[
            f"BA_{number:06d}"
        ] = {
            "phytochemical_chembl_id":
                clean_id(
                    record.get(
                        "phytochemical_chembl_id"
                    )
                ),
        }

    return mapping


def build_has_bioactivity(
    phyto_file: Path,
    bio_file: Path,
    checkpoint_file: Path,
    output_dir: Path,
) -> None:

    """
    Build HAS_BIOACTIVITY using the actual Neo4j Phytochemical ID.

    Phytochemicals are uniquely identified by InChI Key, while ChEMBL ID
    is not unique in the IMPPAT table. Therefore the relationship cannot
    use ChEMBL as the node ID.

    Each bioactivity is linked to every IMPPAT phytochemical row carrying
    the same ChEMBL ID, then writes the canonical InChI Key as the
    Phytochemical relationship endpoint.
    """

    phyto = require_columns(
        phyto_file,
        [
            "inchi_key",
            "chembl_id",
        ],
    )

    bio = require_columns(
        bio_file,
        [
            "bioactivity_id",
        ],
    )

    internal_map = bioactivity_checkpoint_map(
        checkpoint_file
    )

    bio["bio_id_match"] = (
        bio["bioactivity_id"]
        .map(clean_id)
    )

    bio["chembl_match"] = (
        bio["bio_id_match"]
        .map(
            lambda bio_id:
                clean_id(
                    internal_map.get(
                        bio_id,
                        {}
                    ).get(
                        "phytochemical_chembl_id"
                    )
                )
        )
    )

    phyto["inchi_match"] = (
        phyto["inchi_key"]
        .map(clean_id)
    )

    phyto["chembl_match"] = (
        phyto["chembl_id"]
        .map(clean_id)
    )

    phyto = phyto[
        (phyto["inchi_match"] != "")
        & (phyto["chembl_match"] != "")
    ]

    bio = bio[
        (bio["bio_id_match"] != "")
        & (bio["chembl_match"] != "")
    ]

    merged = phyto[
        [
            "inchi_match",
            "chembl_match",
        ]
    ].merge(
        bio[
            [
                "bio_id_match",
                "chembl_match",
            ]
        ],
        on="chembl_match",
        how="inner",
    )

    out = pd.DataFrame({
        "Source": "Phytochemical",
        "Source Id": merged["inchi_match"],
        "Type": "HAS_BIOACTIVITY",
        "Destination": "Bioactivity",
        "Destination Id": merged["bio_id_match"],
        "Label": "has_bioactivity",
        "Description":
            "Associated phytochemical has respective bioactivity",
    })

    out = (
        out
        .drop_duplicates()
        .reset_index(drop=True)
    )

    out.to_excel(
        output_dir / HAS_BIO_FILE,
        index=False,
    )

    print(
        f"HAS_BIOACTIVITY: {len(out)} relationships"
    )


def split_bioactivity_relationships(
    combined_file: Path,
    output_dir: Path,
) -> None:

    """
    Split the existing bioactivity relationship workbook.

    bioactivity_relationship_builder.py produces one workbook containing
    the two relationship tables used by the graph:

      * Phyto_Bioactivity
      * Bioactivity_Target

    Keep the existing builder unchanged; this function only separates
    its sheets into the final relationship workbooks.
    """

    require_file(combined_file)

    xls = pd.ExcelFile(combined_file)

    def find_sheet(*names: str) -> str:

        lookup = {
            name.strip().lower(): name
            for name in xls.sheet_names
        }

        for name in names:

            if name.strip().lower() in lookup:
                return lookup[
                    name.strip().lower()
                ]

        fail(
            f"{combined_file.name} does not contain "
            f"the expected relationship sheet. "
            f"Expected one of: {names}. "
            f"Actual sheets: {xls.sheet_names}"
        )

    phyto_sheet = find_sheet(
        "Phyto_Bioactivity",
        "Phytochemical_Bioactivity",
        "Phytochemicals_Bioactivity",
    )

    target_sheet = find_sheet(
        "Bioactivity_Target",
        "Bioactivity_Targets",
    )

    phyto_rel = pd.read_excel(
        combined_file,
        sheet_name=phyto_sheet,
    )

    target_rel = pd.read_excel(
        combined_file,
        sheet_name=target_sheet,
    )

    phyto_rel.to_excel(
        output_dir / HAS_BIO_FILE,
        index=False,
    )

    target_rel.to_excel(
        output_dir / MEASURED_ON_FILE,
        index=False,
    )

    print(
        f"Split bioactivity relationships: "
        f"HAS_BIOACTIVITY={len(phyto_rel)}, "
        f"MEASURED_ON={len(target_rel)}"
    )


def normalize_relationship_file(
    path: Path,
    output: Path,
    source_label: str,
    destination_label: str,
) -> None:

    df = require_columns(
        path,
        [
            "Source",
            "Source Id",
            "Type",
            "Destination",
            "Destination Id",
            "Label",
            "Description",
        ],
    ).copy()

    df["Source"] = source_label
    df["Destination"] = destination_label

    df["Source Id"] = (
        df["Source Id"]
        .map(clean_id)
    )

    df["Destination Id"] = (
        df["Destination Id"]
        .map(clean_id)
    )

    df = df[
        (df["Source Id"] != "")
        & (df["Destination Id"] != "")
    ]

    df = (
        df
        .drop_duplicates()
        .reset_index(drop=True)
    )

    df.to_excel(
        output,
        index=False,
    )


def fix_has_part_ids(
    output_dir: Path,
) -> None:

    rel = require_columns(
        output_dir / HAS_PART_FILE,
        [
            "Source Id",
            "Destination Id",
            "Type",
            "Label",
            "Description",
            "Source",
            "Destination",
        ],
    ).copy()

    plant = require_columns(
        output_dir / PLANT_FILE,
        [
            "scientific_name",
        ],
    )

    # -----------------------------------------------------------
    # IMPORTANT:
    #
    # Plant node primary key = scientific_name.
    #
    # HAS_PART already receives the scientific name as Source Id
    # from haspart.py. Do NOT translate it through taxonomy_id.
    #
    # This normalization only enforces the final relationship
    # endpoint labels and removes empty/invalid source names.
    # -----------------------------------------------------------

    valid_plant_names = {
        str(value).strip()
        for value in plant["scientific_name"]
        if str(value).strip()
        and str(value).strip().lower() != "nan"
    }

    rel["Source Id"] = (
        rel["Source Id"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    rel = rel[
        rel["Source Id"].isin(
            valid_plant_names
        )
    ].copy()

    rel["Source"] = "Plant"
    rel["Destination"] = "PlantPart"

    rel.to_excel(
        output_dir / HAS_PART_FILE,
        index=False,
    )


def fix_used_for_ids(
    output_dir: Path,
) -> None:

    """
    Convert Used_For destination identifiers
    (MESH/UMLS/etc.) to TU IDs.
    """

    rel = require_columns(
        output_dir / USED_FOR_FILE,
        [
            "Source",
            "Source Id",
            "Type",
            "Destination",
            "Destination Id",
            "Label",
            "Description",
        ],
    ).copy()

    therapeutic = require_columns(
        output_dir / THERAPEUTIC_FILE,
        [
            "therapeutic_use_id",
            "identifier_type",
            "identifier",
        ],
    ).copy()

    lookup = {}

    for r in therapeutic.itertuples(index=False):

        ident = clean_id(r.identifier)
        tu = clean_id(r.therapeutic_use_id)

        if ident and tu:
            lookup.setdefault(
                ident,
                set(),
            ).add(tu)

    rows = []

    for _, r in rel.iterrows():

        sid = clean_id(r["Source Id"])
        did = clean_id(r["Destination Id"])

        matches = lookup.get(
            did,
            set(),
        )

        for tu in sorted(matches):

            row = r.to_dict()

            row["Source"] = "PlantPart"
            row["Source Id"] = sid
            row["Destination"] = "TherapeuticUse"
            row["Destination Id"] = tu
            row["Type"] = "USED_FOR"
            row["Label"] = "used_for"

            rows.append(row)

    out = pd.DataFrame(
        rows,
        columns=rel.columns,
    )

    out = (
        out
        .drop_duplicates()
        .reset_index(drop=True)
    )

    out.to_excel(
        output_dir / USED_FOR_FILE,
        index=False,
    )

    print(
        f"USED_FOR: {len(out)} relationships"
    )


def normalize_therapeutic_nodes(
    output_dir: Path,
) -> None:

    """
    Collapse repeated identifier rows into one Neo4j node row per TU ID.
    """

    df = require_columns(
        output_dir / THERAPEUTIC_FILE,
        [
            "therapeutic_use_id",
            "therapeutic_use",
            "identifier_type",
            "identifier",
            "category",
            "category_name",
            "sub_category",
        ],
    ).copy()

    df["therapeutic_use_id"] = (
        df["therapeutic_use_id"]
        .map(clean_id)
    )

    df["identifier_type"] = (
        df["identifier_type"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    df["identifier"] = (
        df["identifier"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    rows = []

    for tu_id, group in df.groupby(
        "therapeutic_use_id",
        sort=True,
    ):

        if not tu_id:
            continue

        def first_value(col):

            vals = [
                str(v).strip()
                for v in group[col].tolist()
                if (
                    str(v).strip()
                    and str(v).strip().lower()
                    != "nan"
                )
            ]

            return vals[0] if vals else ""

        ordered = [
            "MESH",
            "UMLS",
            "DOID",
            "ICD-11",
            "SNOMED",
        ]

        chosen_type = ""
        chosen_id = ""

        for typ in ordered:

            vals = group.loc[
                group["identifier_type"]
                .str.upper()
                == typ,
                "identifier",
            ].tolist()

            vals = [
                str(v).strip()
                for v in vals
                if (
                    str(v).strip()
                    and str(v).strip().lower()
                    != "nan"
                )
            ]

            if vals:

                chosen_type = typ
                chosen_id = vals[0]

                break

        if not chosen_id:

            chosen_type = first_value(
                "identifier_type"
            )

            chosen_id = first_value(
                "identifier"
            )

        rows.append({
            "therapeutic_use_id": tu_id,
            "therapeutic_use":
                first_value("therapeutic_use"),
            "identifier_type": chosen_type,
            "identifier": chosen_id,
            "category":
                first_value("category"),
            "category_name":
                first_value("category_name"),
            "sub_category":
                first_value("sub_category"),
        })

    out = pd.DataFrame(
        rows,
        columns=df.columns,
    )

    out.to_excel(
        output_dir / THERAPEUTIC_FILE,
        index=False,
    )

    print(
        f"TherapeuticUse nodes: {len(out)} unique IDs"
    )


def export_final_csvs(
    output_dir: Path,
) -> None:

    """
    Export canonical Neo4j CSVs with ID columns
    aligned to relationships.
    """

    node_specs = {
        PLANT_FILE: "Plant.csv",
        PLANT_PART_FILE: "PlantPart.csv",
        PHYTO_FILE: "Phytochemicals.csv",
        BIO_FILE: "Bioactivities.csv",
        DOCUMENT_FILE: "Documents.csv",
        TARGET_FILE: "Targets.csv",
        THERAPEUTIC_FILE: "TherapeuticUses.csv",
    }

    rel_specs = {
        HAS_PART_FILE: "HAS_PART.csv",
        CONTAINS_FILE: "CONTAINS.csv",
        HAS_BIO_FILE: "HAS_BIOACTIVITY.csv",
        MEASURED_ON_FILE: "MEASURED_ON.csv",
        REPORTED_IN_FILE: "REPORTED_IN.csv",
        USED_FOR_FILE: "USED_FOR.csv",
    }

    for xlsx, csv_name in {
        **node_specs,
        **rel_specs,
    }.items():

        df = pd.read_excel(
            output_dir / xlsx
        )

        df.to_csv(
            output_dir / csv_name,
            index=False,
        )


def copy_relationship(
    source: Path,
    destination: Path,
) -> None:

    require_file(source)

    df = pd.read_excel(source)

    df.to_excel(
        destination,
        index=False,
    )


def normalize_bioactivity_nodes(
    output_dir: Path,
) -> None:
    """
    Canonicalize semantically identical Bioactivity nodes after all
    existing Bioactivity collectors/builders have completed.

    This is deliberately an output-only normalization pass. It does not
    modify any collector, checkpoint, field definition, node schema, or
    upstream relationship source. Only these final exported files are
    rewritten:

        * Bioactivities.xlsx
        * HAS_BIOACTIVITY.xlsx
        * MEASURED_ON.xlsx
        * REPORTED_IN.xlsx

    Two Bioactivities are merge-eligible only when their exact:

        standard_type + standard_relation + standard_value +
        standard_units + target + document

    values match. The canonical ID is the lowest existing BA_XXXXXX ID.
    No new IDs are created.

    Every phytochemical -> Bioactivity association is preserved. The
    HAS_BIOACTIVITY row count must remain exactly unchanged; if remapping
    would create duplicate relationships, the stage fails instead of
    silently dropping anything.
    """

    bio_path = output_dir / BIO_FILE
    has_bio_path = output_dir / HAS_BIO_FILE
    measured_path = output_dir / MEASURED_ON_FILE
    reported_path = output_dir / REPORTED_IN_FILE

    bio = require_columns(
        bio_path,
        [
            "bioactivity_id",
            "standard_type",
            "standard_relation",
            "standard_value",
            "standard_units",
            "normalized_value_nm",
            "high_confidence_tier",
        ],
    ).copy()

    if bio.empty:
        print(
            "Bioactivity normalization: no Bioactivities found; "
            "nothing to normalize."
        )
        return

    has_bio = require_columns(
        has_bio_path,
        [
            "Source",
            "Source Id",
            "Type",
            "Destination",
            "Destination Id",
            "Label",
            "Description",
        ],
    ).copy()

    measured_on = require_columns(
        measured_path,
        [
            "Source",
            "Source Id",
            "Type",
            "Destination",
            "Destination Id",
            "Label",
            "Description",
        ],
    ).copy()

    reported_in = require_columns(
        reported_path,
        [
            "Source",
            "Source Id",
            "Type",
            "Destination",
            "Destination Id",
            "Label",
            "Description",
        ],
    ).copy()

    before_bio_count = len(bio)
    before_has_count = len(has_bio)
    before_measured_count = len(measured_on)
    before_reported_count = len(reported_in)

    def relationship_lookup(
        rel: pd.DataFrame,
        relationship_name: str,
    ) -> dict[str, str]:
        grouped = {}

        for source_id, group in rel.groupby(
            rel["Source Id"].map(clean_id),
            dropna=False,
        ):
            if not source_id:
                fail(
                    f"Bioactivity normalization: {relationship_name} "
                    "contains an empty Bioactivity Source Id."
                )

            destinations = {
                clean_id(value)
                for value in group["Destination Id"]
                if clean_id(value)
            }

            if len(destinations) != 1:
                fail(
                    f"Bioactivity normalization: {relationship_name} "
                    f"must have exactly one destination for {source_id}; "
                    f"found {len(destinations)}."
                )

            grouped[source_id] = next(iter(destinations))

        return grouped

    target_of = relationship_lookup(
        measured_on,
        "MEASURED_ON",
    )
    document_of = relationship_lookup(
        reported_in,
        "REPORTED_IN",
    )

    bio_ids = bio["bioactivity_id"].map(clean_id)

    if bio_ids.eq("").any():
        fail(
            "Bioactivity normalization: empty bioactivity_id found."
        )

    if bio_ids.duplicated().any():
        fail(
            "Bioactivity normalization: duplicate bioactivity_id values "
            "already exist before normalization."
        )

    bio["_normalized_bio_id"] = bio_ids

    missing_targets = [
        bid for bid in bio_ids if bid not in target_of
    ]
    missing_documents = [
        bid for bid in bio_ids if bid not in document_of
    ]

    if missing_targets:
        fail(
            "Bioactivity normalization: "
            f"{len(missing_targets)} Bioactivities have no MEASURED_ON "
            "target."
        )

    if missing_documents:
        fail(
            "Bioactivity normalization: "
            f"{len(missing_documents)} Bioactivities have no REPORTED_IN "
            "document."
        )

    def scalar(value) -> str:
        if pd.isna(value):
            return ""
        return str(value).strip()

    bio["_merge_key"] = bio.apply(
        lambda row: (
            scalar(row["standard_type"]),
            scalar(row["standard_relation"]) or "NA",
            scalar(row["standard_value"]),
            scalar(row["standard_units"]),
            target_of[row["_normalized_bio_id"]],
            document_of[row["_normalized_bio_id"]],
        ),
        axis=1,
    )

    remap: dict[str, str] = {}
    audit_rows: list[dict[str, object]] = []

    for key, group in bio.groupby(
        "_merge_key",
        sort=True,
    ):
        ids = sorted(group["_normalized_bio_id"].tolist())

        if len(ids) <= 1:
            continue

        canonical = ids[0]

        # Required merge fields are already identical by construction.
        # The remaining Bioactivity node fields must also agree so no field
        # value is silently discarded when the duplicate rows are removed.
        for column in (
            "normalized_value_nm",
            "high_confidence_tier",
        ):
            values = {scalar(value) for value in group[column]}
            if len(values) > 1:
                fail(
                    "Bioactivity normalization: merge conflict in "
                    f"{column} for candidate key {key}."
                )

        canonical_row = group.loc[
            group["_normalized_bio_id"] == canonical
        ].iloc[0]

        for duplicate in ids[1:]:
            remap[duplicate] = canonical
            audit_rows.append({
                "merged_id": duplicate,
                "canonical_id": canonical,
                "standard_type": canonical_row["standard_type"],
                "standard_relation": canonical_row["standard_relation"],
                "standard_value": canonical_row["standard_value"],
                "standard_units": canonical_row["standard_units"],
                "target_id": target_of[canonical],
                "document_id": document_of[canonical],
            })

    if not remap:
        print("Bioactivity normalization: no eligible duplicate nodes found.")
        return

    bio_columns = [
        column
        for column in bio.columns
        if column not in {"_normalized_bio_id", "_merge_key"}
    ]

    normalized_bio = bio.loc[
        ~bio["_normalized_bio_id"].isin(remap),
        bio_columns,
    ].copy()

    normalized_has_bio = has_bio.copy()
    normalized_has_bio["Destination Id"] = (
        normalized_has_bio["Destination Id"]
        .map(clean_id)
        .map(lambda value: remap.get(value, value))
    )

    normalized_measured = measured_on.loc[
        ~measured_on["Source Id"].map(clean_id).isin(remap)
    ].copy()

    normalized_reported = reported_in.loc[
        ~reported_in["Source Id"].map(clean_id).isin(remap)
    ].copy()

    # ---------------------------------------------------------------
    # Mandatory invariants.
    # ---------------------------------------------------------------

    if len(normalized_has_bio) != before_has_count:
        fail(
            "Bioactivity normalization: HAS_BIOACTIVITY relationship count "
            f"changed from {before_has_count} to "
            f"{len(normalized_has_bio)}."
        )

    before_source_counts = (
        has_bio["Source Id"].map(clean_id).value_counts().sort_index()
    )
    after_source_counts = (
        normalized_has_bio["Source Id"].map(clean_id).value_counts().sort_index()
    )

    if not before_source_counts.equals(after_source_counts):
        fail(
            "Bioactivity normalization: HAS_BIOACTIVITY source association "
            "counts changed; at least one phytochemical lost or gained an "
            "association."
        )

    if normalized_has_bio.duplicated().any():
        fail(
            "Bioactivity normalization: remapping would create duplicate "
            "HAS_BIOACTIVITY relationship rows; no rows were removed."
        )

    canonical_ids = set(normalized_bio["bioactivity_id"].map(clean_id))

    if not set(normalized_has_bio["Destination Id"].map(clean_id)).issubset(canonical_ids):
        fail(
            "Bioactivity normalization: HAS_BIOACTIVITY contains a "
            "destination Bioactivity ID that does not exist after "
            "normalization."
        )

    if not set(normalized_measured["Source Id"].map(clean_id)).issubset(canonical_ids):
        fail(
            "Bioactivity normalization: MEASURED_ON contains a Bioactivity "
            "ID that does not exist after normalization."
        )

    if not set(normalized_reported["Source Id"].map(clean_id)).issubset(canonical_ids):
        fail(
            "Bioactivity normalization: REPORTED_IN contains a Bioactivity "
            "ID that does not exist after normalization."
        )

    if list(normalized_bio.columns) != bio_columns:
        fail("Bioactivity normalization: Bioactivities column schema changed.")

    if list(normalized_has_bio.columns) != list(has_bio.columns):
        fail("Bioactivity normalization: HAS_BIOACTIVITY column schema changed.")

    if list(normalized_measured.columns) != list(measured_on.columns):
        fail("Bioactivity normalization: MEASURED_ON column schema changed.")

    if list(normalized_reported.columns) != list(reported_in.columns):
        fail("Bioactivity normalization: REPORTED_IN column schema changed.")

    if len(normalized_bio) != before_bio_count - len(remap):
        fail(
            "Bioactivity normalization: unexpected Bioactivities row count "
            "after canonicalization."
        )

    if len(normalized_measured) != before_measured_count - len(remap):
        fail(
            "Bioactivity normalization: unexpected MEASURED_ON row count "
            "after canonicalization."
        )

    if len(normalized_reported) != before_reported_count - len(remap):
        fail(
            "Bioactivity normalization: unexpected REPORTED_IN row count "
            "after canonicalization."
        )

    # ---------------------------------------------------------------
    # Commit only after all validation has passed.
    # ---------------------------------------------------------------

    import os
    import uuid

    temp_paths = {
        bio_path: output_dir / f".{bio_path.name}.{uuid.uuid4().hex}.tmp.xlsx",
        has_bio_path: output_dir / f".{has_bio_path.name}.{uuid.uuid4().hex}.tmp.xlsx",
        measured_path: output_dir / f".{measured_path.name}.{uuid.uuid4().hex}.tmp.xlsx",
        reported_path: output_dir / f".{reported_path.name}.{uuid.uuid4().hex}.tmp.xlsx",
    }

    try:
        normalized_bio.to_excel(temp_paths[bio_path], index=False)
        normalized_has_bio.to_excel(temp_paths[has_bio_path], index=False)
        normalized_measured.to_excel(temp_paths[measured_path], index=False)
        normalized_reported.to_excel(temp_paths[reported_path], index=False)

        for temp_path in temp_paths.values():
            require_file(temp_path)

        for destination, temp_path in temp_paths.items():
            os.replace(temp_path, destination)

    finally:
        for temp_path in temp_paths.values():
            if temp_path.exists():
                temp_path.unlink()

    pd.DataFrame(
        audit_rows,
        columns=[
            "merged_id",
            "canonical_id",
            "standard_type",
            "standard_relation",
            "standard_value",
            "standard_units",
            "target_id",
            "document_id",
        ],
    ).to_excel(
        output_dir / "Bioactivity_Merge_Log.xlsx",
        index=False,
    )

    print(
        "Bioactivity normalization: "
        f"{before_bio_count} -> {len(normalized_bio)} Bioactivities; "
        f"{len(remap)} duplicate nodes merged; "
        f"HAS_BIOACTIVITY={len(normalized_has_bio)} (unchanged); "
        f"MEASURED_ON={len(normalized_measured)}; "
        f"REPORTED_IN={len(normalized_reported)}."
    )
    print(
        "Bioactivity normalization: all association and schema invariants passed."
    )


# ---------------------------------------------------------------------
# INPUT
# ---------------------------------------------------------------------

def canonicalize_plant_name(
    input_file: Path,
    plant_file: Path,
) -> str:

    """
    Return the plant name exactly as supplied by the user.

    For a one-plant run this is the first non-empty line in the input file.
    For a multi-plant run the downstream collectors handle all plants.
    """

    text = input_file.read_text(
        encoding="utf-8",
        errors="ignore",
    )

    names = [
        x.strip()
        for x in text.splitlines()
        if x.strip()
    ]

    if not names:
        fail(
            f"No plant name found in input file: {input_file}"
        )

    return names[0]


def force_input_names(
    input_file: Path,
    plant_file: Path,
    plant_part_file: Path,
) -> None:

    """
    Make the user's supplied plant name canonical.

    Taxonomy APIs may return a synonym/current accepted botanical
    name such as Vachellia leucophloea for an input of
    Acacia leucophloea.

    That must not change the graph identity used by this project.

    PlantPart.xlsx already carries the source/input plant name in
    plant_name, so it is preserved.

    Plant.xlsx is normalized to the same input name.
    """

    names = [
        x.strip()
        for x in input_file.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines()
        if x.strip()
    ]

    if len(names) != 1:

        # Full runs can contain many plants.
        # Do not blindly replace all names.
        return

    canonical_name = names[0]

    plant = read_excel(
        plant_file
    )

    plant_part = read_excel(
        plant_part_file
    )

    if "scientific_name" not in plant.columns:

        fail(
            f"{plant_file.name} does not contain "
            f"'scientific_name'. "
            f"Columns: {list(plant.columns)}"
        )

    if "plant_name" not in plant_part.columns:

        fail(
            f"{plant_part_file.name} does not contain "
            f"'plant_name'. "
            f"Columns: {list(plant_part.columns)}"
        )

    plant["scientific_name"] = canonical_name

    plant_part["plant_name"] = canonical_name

    plant.to_excel(
        plant_file,
        index=False,
    )

    plant_part.to_excel(
        plant_part_file,
        index=False,
    )

    print(
        f"\nCanonical input plant name enforced: "
        f"{canonical_name}"
    )


def make_input_file(
    args,
    output_dir: Path,
) -> Path:

    if args.plant and args.file:

        fail(
            "Use either --plant or --file, not both."
        )

    if not args.plant and not args.file:

        fail(
            "Provide either --plant or --file."
        )

    if args.plant:

        text_file = (
            output_dir / "_test_plant.txt"
        )

        text_file.write_text(
            args.plant.strip() + "\n",
            encoding="utf-8",
        )

        return text_file

    source = Path(
        args.file
    ).resolve()

    if not source.exists():

        fail(
            f"Input file does not exist:\n{source}"
        )

    return source


# ---------------------------------------------------------------------
# FINAL VALIDATION
# ---------------------------------------------------------------------

def validate_final_graph(
    output_dir: Path,
) -> None:

    print()
    print("=" * 78)
    print("FINAL GRAPH VALIDATION")
    print("=" * 78)

    plant = require_columns(
        output_dir / PLANT_FILE,
        [
            "taxonomy_id",
            "scientific_name",
            "global_status",
            "regional_status",
            "iucn_year",
            "source",
        ],
    )

    plant_part = require_columns(
        output_dir / PLANT_PART_FILE,
        [
            "plant_part_id",
            "plant_name",
            "plant_part",
            "source_name",
        ],
    )

    phyto = require_columns(
        output_dir / PHYTO_FILE,
        [
            "imppat_id",
            "phytochemical_name",
            "inchi_key",
            "smiles",
            "chembl_id",
            "classyfire_kingdom",
            "classyfire_superclass",
            "classyfire_class",
            "classyfire_subclass",
            "source_url",
        ],
    )

    bio = require_columns(
        output_dir / BIO_FILE,
        [
            "bioactivity_id",
            "standard_type",
            "standard_relation",
            "standard_value",
            "standard_units",
            "normalized_value_nm",
            "high_confidence_tier",
        ],
    )

    documents = require_columns(
        output_dir / DOCUMENT_FILE,
        [
            "document_chembl_id",
            "source_id",
            "document_title",
            "document_author",
            "document_journal",
            "document_year",
        ],
    )

    targets = require_columns(
        output_dir / TARGET_FILE,
        [
            "target_chembl_id",
            "uniprot_id",
            "target_name",
            "target_organism",
            "target_type",
        ],
    )

    #check_unique(
    #    plant,
    #    "taxonomy_id",
    #    "Plant",
    #)

    check_unique(
        plant_part,
        "plant_part_id",
        "PlantPart",
    )

    check_unique(
        phyto,
        "inchi_key",
        "Phytochemicals",
    )

    check_unique(
        bio,
        "bioactivity_id",
        "Bioactivities",
    )

    check_unique(
        documents,
        "document_chembl_id",
        "Documents",
    )

    check_unique(
        targets,
        "target_chembl_id",
        "Targets",
    )

    therapeutic = require_columns(
        output_dir / THERAPEUTIC_FILE,
        [
            "therapeutic_use",
            "mesh_id",
            "mesh_category",
            "mesh_category_name",
            "mesh_sub_category_name",
        ],
    )

    check_unique(
        therapeutic,
        "mesh_id",
        "TherapeuticUse",
    )

    validate_relationship(
        output_dir / HAS_PART_FILE,
        output_dir / PLANT_FILE,
        "scientific_name",
        output_dir / PLANT_PART_FILE,
        "plant_part_id",
        "HAS_PART",
    )

    validate_relationship(
        output_dir / CONTAINS_FILE,
        output_dir / PLANT_PART_FILE,
        "plant_part_id",
        output_dir / PHYTO_FILE,
        "inchi_key",
        "CONTAINS",
    )

    validate_relationship(
        output_dir / HAS_BIO_FILE,
        output_dir / PHYTO_FILE,
        "inchi_key",
        output_dir / BIO_FILE,
        "bioactivity_id",
        "HAS_BIOACTIVITY",
    )

    validate_relationship(
        output_dir / USED_FOR_FILE,
        output_dir / PLANT_PART_FILE,
        "plant_part_id",
        output_dir / THERAPEUTIC_FILE,
        "mesh_id",
        "USED_FOR",
    )

    validate_relationship(
        output_dir / REPORTED_IN_FILE,
        output_dir / BIO_FILE,
        "bioactivity_id",
        output_dir / DOCUMENT_FILE,
        "document_chembl_id",
        "REPORTED_IN",
    )

    validate_relationship(
        output_dir / MEASURED_ON_FILE,
        output_dir / BIO_FILE,
        "bioactivity_id",
        output_dir / TARGET_FILE,
        "target_chembl_id",
        "MEASURED_ON",
    )

    print()

    print(
        f"Nodes: "
        f"Plant={len(plant)}, "
        f"PlantPart={len(plant_part)}, "
        f"Phytochemicals={len(phyto)}, "
        f"Bioactivities={len(bio)}, "
        f"Documents={len(documents)}, "
        f"Targets={len(targets)}, "
        f"TherapeuticUse="
        f"{therapeutic['mesh_id'].nunique()}"
    )

    print(
        "Final graph validation: PASSED"
    )


# ---------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Master Ayurvedic knowledge-graph collector. "
            "Use --plant for a one-plant test or --file "
            "for the complete plant list."
        )
    )

    parser.add_argument(
        "--plant",
        help='One plant, e.g. "Acacia leucophloea"',
    )

    parser.add_argument(
        "--file",
        required=False,
        help="TXT file containing one plant name per line.",
    )

    parser.add_argument(
        "--output",
        default="master_output",
        help="Directory for generated files.",
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force every master stage to run again.",
    )

    args = parser.parse_args()

    global CACHE_ENABLED
    CACHE_ENABLED = not args.no_cache

    output_dir = Path(
        args.output
    ).resolve()

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    input_file = make_input_file(
        args,
        output_dir,
    )

    print("=" * 78)
    print(
        "MASTER AYURVEDIC KNOWLEDGE-GRAPH COLLECTOR"
    )
    print("=" * 78)

    if args.plant:

        print(
            "Mode  : ONE-PLANT TEST"
        )

        print(
            f"Plant : {args.plant}"
        )

    else:

        print(
            "Mode  : TXT INPUT"
        )

    print(
        f"Input : {input_file}"
    )

    print(
        f"Output: {output_dir}"
    )

    # ---------------------------------------------------------------
    # 1. PLANT
    # ---------------------------------------------------------------

    run(
        "STEP 1/10 - PLANT",
        PLANT_MAIN,
        [
            "--file",
            str(input_file),
            "--output",
            str(output_dir / PLANT_FILE),
        ],
        ROOT,
    )

    # ---------------------------------------------------------------
    # 2. PLANT PART
    # ---------------------------------------------------------------

    run(
        "STEP 2/10 - PLANT PART",
        PLANT_PART,
        [
            "--file",
            str(input_file),
            "--output",
            str(output_dir / PLANT_PART_FILE),
        ],
        ROOT,
    )

    # ---------------------------------------------------------------
    # 3. NORMALIZE USER INPUT NAME
    # ---------------------------------------------------------------

    run_internal(
        "STEP 2B/10 - NORMALIZE USER INPUT NAME",
        [
            input_file,
            output_dir / PLANT_FILE,
            output_dir / PLANT_PART_FILE,
        ],
        [
            output_dir / PLANT_FILE,
            output_dir / PLANT_PART_FILE,
        ],
        lambda: force_input_names(
            input_file,
            output_dir / PLANT_FILE,
            output_dir / PLANT_PART_FILE,
        ),
        output_dir,
        version="1",
    )

    # ---------------------------------------------------------------
    # 4. HAS_PART
    # ---------------------------------------------------------------

    run(
        "STEP 3/10 - HAS_PART",
        HAS_PART,
        [
            "--plant",
            str(output_dir / PLANT_FILE),
            "--plantpart",
            str(output_dir / PLANT_PART_FILE),
            "--output",
            str(output_dir / HAS_PART_FILE),
        ],
        ROOT,
    )

    # ---------------------------------------------------------------
    # 5. IMPPAT MAPPING
    # ---------------------------------------------------------------

    run(
        "STEP 4/10 - PHYTOCHEMICAL MAPPING",
        PHYTO_COLLECTOR,
        [
            "--file",
            str(output_dir / PLANT_PART_FILE),
            "--output",
            str(output_dir / MAPPING_FILE),
        ],
        ROOT,
    )

    # ---------------------------------------------------------------
    # 6. PHYTOCHEMICALS + CONTAINS
    # ---------------------------------------------------------------

    run(
        "STEP 5A/10 - PHYTOCHEMICALS",
        PHYTO_BUILDER,
        [
            "--file",
            str(output_dir / MAPPING_FILE),
            "--output",
            str(output_dir / PHYTO_FILE),
            "--cache",
            str(
                output_dir
                / "imppat_phytochemical_cache.json"
            ),
            "--checkpoint",
            str(
                output_dir
                / "Phytochemicals_checkpoint.xlsx"
            ),
        ],
        ROOT,
    )

    run(
        "STEP 5B/10 - CONTAINS",
        CONTAINS_BUILDER,
        [
            "--file",
            str(output_dir / MAPPING_FILE),
            "--phytochemicals",
            str(output_dir / PHYTO_FILE),
            "--output",
            str(output_dir / CONTAINS_FILE),
        ],
        ROOT,
    )

    # ---------------------------------------------------------------
    # 7. BIOACTIVITIES
    # ---------------------------------------------------------------

    # If the plant has no phytochemicals, the Bioactivity builder has
    # nothing to process and does not create its normal artifacts.
    # Create valid empty artifacts only for this edge case so all downstream
    # stages can continue unchanged.
    phyto_df = read_excel(output_dir / PHYTO_FILE)
    if phyto_df.empty:
        print(
            "No phytochemicals found for this plant. "
            "Creating empty Bioactivity artifacts and continuing."
        )

        pd.DataFrame(
            columns=[
                "bioactivity_id",
                "standard_type",
                "standard_relation",
                "standard_value",
                "standard_units",
                "normalized_value_nm",
                "high_confidence_tier",
            ]
        ).to_excel(
            output_dir / BIO_FILE,
            index=False,
        )

        import json

        with (output_dir / BIO_CHECKPOINT_FILE).open(
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                {"records": []},
                handle,
                indent=2,
            )

        require_file(output_dir / BIO_FILE)
        require_file(output_dir / BIO_CHECKPOINT_FILE)

        print(
            "BIOACTIVITIES: no phytochemicals found; "
            "created valid empty Bioactivities.xlsx and checkpoint."
        )
    else:
        run(
            "STEP 6/10 - BIOACTIVITIES",
            BIO_BUILDER,
            [
                "--file",
                str(output_dir / PHYTO_FILE),
                "--output",
                str(output_dir / BIO_FILE),
                "--checkpoint",
                str(
                    output_dir
                    / BIO_CHECKPOINT_FILE
                ),
            ],
            ROOT,
        )

    # ---------------------------------------------------------------
    # 8. BIOACTIVITY RELATIONSHIPS
    # ---------------------------------------------------------------

    run_internal(
        "STEP 7/10 - HAS_BIOACTIVITY",
        [
            output_dir / PHYTO_FILE,
            output_dir / BIO_FILE,
            output_dir / BIO_CHECKPOINT_FILE,
        ],
        [
            output_dir / HAS_BIO_FILE,
        ],
        lambda: build_has_bioactivity(
            output_dir / PHYTO_FILE,
            output_dir / BIO_FILE,
            output_dir / BIO_CHECKPOINT_FILE,
            output_dir,
        ),
        output_dir,
        version="3",
    )

    # ---------------------------------------------------------------
    # 9. DOCUMENTS
    # ---------------------------------------------------------------

    run(
        "STEP 8/10 - DOCUMENTS + REPORTED_IN",
        DOCUMENT_COLLECTOR,
        [
            "--file",
            str(output_dir / BIO_FILE),
            "--checkpoint",
            str(
                output_dir
                / BIO_CHECKPOINT_FILE
            ),
            "--documents",
            str(output_dir / DOCUMENT_FILE),
            "--relationships",
            str(output_dir / DOC_REL_FILE),
            "--cache",
            str(
                output_dir
                / "document_cache.json"
            ),
        ],
        ROOT,
    )

    run_internal(
        "STEP 8B/10 - REPORTED_IN COPY",
        [
            output_dir / DOC_REL_FILE,
        ],
        [
            output_dir / REPORTED_IN_FILE,
        ],
        lambda: copy_relationship(
            output_dir / DOC_REL_FILE,
            output_dir / REPORTED_IN_FILE,
        ),
        output_dir,
        version="3",
    )

    # ---------------------------------------------------------------
    # 9. TARGETS + UNIPROT + MEASURED_ON
    # ---------------------------------------------------------------

    run(
        "STEP 9/10 - TARGETS + UNIPROT + MEASURED_ON",
        TARGET_COLLECTOR,
        [
            "--file",
            str(output_dir / BIO_FILE),

            "--targets",
            str(output_dir / TARGET_FILE),

            "--relationships",
            str(output_dir / TARGET_REL_FILE),

            # Required because Bioactivities.xlsx intentionally
            # does not contain target_id.
            "--checkpoint",
            str(output_dir / BIO_CHECKPOINT_FILE),
        ],
        ROOT,
    )

    run_internal(
        "STEP 9B/10 - MEASURED_ON COPY",
        [
            output_dir / TARGET_REL_FILE,
        ],
        [
            output_dir / MEASURED_ON_FILE,
        ],
        lambda: copy_relationship(
            output_dir / TARGET_REL_FILE,
            output_dir / MEASURED_ON_FILE,
        ),
        output_dir,
        version="3",
    )

    # ---------------------------------------------------------------
    # 9C. BIOACTIVITY NODE NORMALIZATION
    #
    # Strict post-collector normalization. No existing collector, builder,
    # checkpoint, schema, or field population logic is changed.
    # ---------------------------------------------------------------

    run_internal(
        "STEP 9C/12 - NORMALIZE BIOACTIVITY NODES",
        [
            output_dir / BIO_FILE,
            output_dir / HAS_BIO_FILE,
            output_dir / MEASURED_ON_FILE,
            output_dir / REPORTED_IN_FILE,
        ],
        [
            output_dir / BIO_FILE,
            output_dir / HAS_BIO_FILE,
            output_dir / MEASURED_ON_FILE,
            output_dir / REPORTED_IN_FILE,
        ],
        lambda: normalize_bioactivity_nodes(
            output_dir,
        ),
        output_dir,
        version="1",
    )
    # ---------------------------------------------------------------
    # 10B. THERAPEUTIC USES + USED_FOR
    # ---------------------------------------------------------------

    run(
        "STEP 10/12 - THERAPEUTIC USE COLLECTION",
        THERAPEUTIC_COLLECTOR,
        [
            "--file",
            str(output_dir / PLANT_PART_FILE),

            # Final TherapeuticUse node file
            "--output",
            str(output_dir / THERAPEUTIC_COLLECTED_NODE_FILE),

            # PlantPart -> TherapeuticUse mapping
            "--mapping-output",
            str(output_dir / THERAPEUTIC_MAPPING_RAW_FILE),
        ],
        ROOT,
    )

    # ---------------------------------------------------------------
    # STEP 11/12 - THERAPEUTIC USE BUILD
    #
    # The collector preserves the PlantPart -> therapeutic-use mapping,
    # but IMPPAT may not provide a MeSH ID for every therapeutic-use row.
    # The dedicated builder performs exact MeSH descriptor/entry-term
    # resolution, fills mesh_id, and enriches the final node fields.
    # ---------------------------------------------------------------

    run(
        "STEP 11/12 - THERAPEUTIC USE BUILD",
        THERAPEUTIC_BUILDER,
        [
            "--file",
            str(output_dir / THERAPEUTIC_MAPPING_RAW_FILE),

            "--output",
            str(output_dir / THERAPEUTIC_FILE),

            "--mapping-output",
            str(output_dir / THERAPEUTIC_MAPPING_FILE),

            "--cache",
            str(output_dir / "mesh_cache.json"),
        ],
        ROOT,
    )

    print()
    print(
        f"Created therapeutic-use node file:\n  {output_dir / THERAPEUTIC_FILE}"
    )
    print(
        f"Resolved therapeutic-use mapping:\n  {output_dir / THERAPEUTIC_MAPPING_FILE}"
    )

    # ---------------------------------------------------------------
    # STEP 12/12 - USED_FOR
    #
    # The Therapeutic_Use_Mapping.xlsx file already contains:
    #
    #   plant_part_id
    #   plant_name
    #   plant_part
    #   mesh_id
    #   therapeutic_use
    #   source
    #
    # Final relationship:
    #
    #   PlantPart
    #       |
    #       | USED_FOR
    #       v
    #   TherapeuticUse
    #
    # Destination Id = mesh_id
    # ---------------------------------------------------------------

    run(
        "STEP 12/12 - USED_FOR",
        USED_FOR_BUILDER,
        [
            # Raw PlantPart -> therapeutic-use associations
            "--file",
            str(
                output_dir
                / THERAPEUTIC_MAPPING_RAW_FILE
            ),

            # Resolved mapping containing mesh_id
            "--mapping",
            str(
                output_dir
                / THERAPEUTIC_MAPPING_FILE
            ),

            "--output",
            str(
                output_dir
                / USED_FOR_FILE
            ),
        ],
        ROOT,
    )

    # ---------------------------------------------------------------
    # IMPORTANT:
    #
    # Do NOT call:
    #
    #   normalize_therapeutic_nodes()
    #
    # That belongs to the old therapeutic_use_id schema.
    #
    # The current TherapeuticUse node ID is mesh_id.
    # ---------------------------------------------------------------

    # ---------------------------------------------------------------
    # FINAL - HAS_PART NORMALIZE
    # ---------------------------------------------------------------

    run_internal(
        "FINAL - HAS_PART NORMALIZE",
        [
            output_dir / HAS_PART_FILE,
            output_dir / PLANT_FILE,
        ],
        [
            output_dir / HAS_PART_FILE,
        ],
        lambda: fix_has_part_ids(
            output_dir
        ),
        output_dir,
        version="4",
    )

    # ---------------------------------------------------------------
    # FINAL - CSV EXPORT
    # ---------------------------------------------------------------

    run_internal(
        "FINAL - CSV EXPORT",
        [
            output_dir / PLANT_FILE,
            output_dir / PLANT_PART_FILE,
            output_dir / PHYTO_FILE,
            output_dir / BIO_FILE,
            output_dir / DOCUMENT_FILE,
            output_dir / TARGET_FILE,
            output_dir / THERAPEUTIC_FILE,
        ],
        [
            output_dir / "Plant.csv",
            output_dir / "PlantPart.csv",
            output_dir / "Phytochemicals.csv",
            output_dir / "Bioactivities.csv",
            output_dir / "Documents.csv",
            output_dir / "Targets.csv",
            output_dir / "TherapeuticUses.csv",
            output_dir / "HAS_PART.csv",
            output_dir / "CONTAINS.csv",
            output_dir / "HAS_BIOACTIVITY.csv",
            output_dir / "MEASURED_ON.csv",
            output_dir / "REPORTED_IN.csv",
            output_dir / "USED_FOR.csv",
        ],
        lambda: export_final_csvs(
            output_dir
        ),
        output_dir,
        version="3",
    )

    # ---------------------------------------------------------------
    # FINAL - GRAPH VALIDATION
    # ---------------------------------------------------------------

    run_internal(
        "FINAL - GRAPH VALIDATION",
        [
            output_dir / PLANT_FILE,
            output_dir / PLANT_PART_FILE,
            output_dir / PHYTO_FILE,
            output_dir / BIO_FILE,
            output_dir / DOCUMENT_FILE,
            output_dir / TARGET_FILE,
            output_dir / THERAPEUTIC_FILE,
            output_dir / HAS_PART_FILE,
            output_dir / CONTAINS_FILE,
            output_dir / HAS_BIO_FILE,
            output_dir / REPORTED_IN_FILE,
            output_dir / MEASURED_ON_FILE,
            output_dir / USED_FOR_FILE,
        ],
        [],
        lambda: validate_final_graph(
            output_dir
        ),
        output_dir,
        version="3",
    )

    # ---------------------------------------------------------------
    # PIPELINE COMPLETE
    # ---------------------------------------------------------------

    print()
    print(
        "=" * 78
    )
    print(
        "MASTER PIPELINE COMPLETE"
    )
    print(
        "=" * 78
    )
    print()

    print(
        "FINAL FILES:"
    )

    for filename in [
        PLANT_FILE,
        PLANT_PART_FILE,
        HAS_PART_FILE,
        PHYTO_FILE,
        CONTAINS_FILE,
        BIO_FILE,
        HAS_BIO_FILE,
        DOCUMENT_FILE,
        REPORTED_IN_FILE,
        TARGET_FILE,
        MEASURED_ON_FILE,
        THERAPEUTIC_FILE,
        USED_FOR_FILE,
    ]:

        print(
            f"  {filename}"
        )

    print()

    print(
        f"Output directory:\n"
        f"  {output_dir}"
    )

    print()


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print(
            "\nStopped by user."
        )

        sys.exit(130)

    except Exception as exc:

        print()
        print("=" * 78)
        print("MASTER PIPELINE FAILED")
        print("=" * 78)
        print(exc)