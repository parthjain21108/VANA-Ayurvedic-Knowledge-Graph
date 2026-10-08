import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests


# Allow direct execution from backend\bioactivity to import the
# project-wide operational cache without changing collector behavior.
_PROJECT_ROOT_FOR_SHARED_CACHE = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT_FOR_SHARED_CACHE) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT_FOR_SHARED_CACHE))

from shared_ingestion_cache import (  # noqa: E402
    http_cache_enabled,
    http_cache_get,
    http_cache_put,
    http_cache_put_negative,
)


_SHARED_CACHE_HITS = 0
_SHARED_CACHE_MISSES = 0


# ============================================================
# CONFIG
# ============================================================

CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data"

# CLEAN DATASET:
# Only requested quantitative affinity/potency measurements.
ALLOWED_TYPES = {
    "IC50",
    "KI",
    "KD",
    "EC50",
    "POTENCY",
}

REQUIRED_TARGET_TYPE = "SINGLE PROTEIN"
REQUIRED_TARGET_ORGANISM = "Homo sapiens"

# Only nM and μM are accepted.
ALLOWED_UNITS = {
    "NM",
    "UM",
    "ΜM",
}

HIGH_CONFIDENCE_CUTOFF_NM = 1_000.0
MAX_POTENCY_NM = 10_000.0

# Force an old checkpoint to be regenerated under the new rules.
CHECKPOINT_VERSION = 2


OUTPUT_COLUMNS = [
    "bioactivity_id",
    "standard_type",
    "standard_relation",
    "standard_value",
    "standard_units",
    "normalized_value_nm",
    "high_confidence_tier",
]


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent":
        "Ayurvedic-Phytochemical-Bioactivity-Collector/1.0",
    "Accept":
        "application/json",
})


# ============================================================
# BASIC HELPERS
# ============================================================

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


# ============================================================
# HTTP
# ============================================================

def request_json(url, params=None, retries=5):

    global _SHARED_CACHE_HITS, _SHARED_CACHE_MISSES

    project_root = _PROJECT_ROOT_FOR_SHARED_CACHE
    if http_cache_enabled():
        hit, cached = http_cache_get(
            project_root,
            url,
            params,
        )
        if hit:
            _SHARED_CACHE_HITS += 1
            return cached
        _SHARED_CACHE_MISSES += 1

    for attempt in range(retries):

        try:

            response = session.get(
                url,
                params=params,
                timeout=30,
            )

            if response.status_code == 200:
                data = response.json()
                if http_cache_enabled():
                    http_cache_put(
                        project_root,
                        url,
                        params,
                        data,
                    )
                return data

            if response.status_code == 404:
                if http_cache_enabled():
                    http_cache_put_negative(
                        project_root,
                        url,
                        params,
                        response.status_code,
                    )
                return None

            if response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }:

                wait = min(
                    2 ** attempt,
                    30,
                )

                print(
                    f"      HTTP "
                    f"{response.status_code}; "
                    f"retrying in {wait}s..."
                )

                time.sleep(wait)

                continue

            print(
                f"      HTTP "
                f"{response.status_code}"
            )

            return None

        except requests.RequestException as exc:

            wait = min(
                2 ** attempt,
                30,
            )

            print(
                f"      Request error: {exc}"
            )

            if attempt < retries - 1:

                print(
                    f"      Retrying in "
                    f"{wait}s..."
                )

                time.sleep(wait)

    return None


# ============================================================
# UNIT NORMALIZATION
# ============================================================

UNIT_TO_NM = {
    "nM": 1.0,
    "uM": 1_000.0,
    "µM": 1_000.0,
    "μM": 1_000.0,
    "UM": 1_000.0,
}


def normalize_to_nm(value, units):

    if value is None:
        return None

    units = clean(units)

    if not units:
        return None

    try:
        value = float(value)

    except (
        TypeError,
        ValueError,
    ):
        return None

    multiplier = UNIT_TO_NM.get(units)

    if multiplier is None:

        lookup = {
            str(k).lower(): v
            for k, v in UNIT_TO_NM.items()
        }

        multiplier = lookup.get(
            units.lower()
        )

    if multiplier is None:
        return None

    return value * multiplier


def normalize_unit_name(units):

    units = clean(units)

    if not units:
        return None

    normalized = (
        units
        .replace(" ", "")
        .upper()
    )

    if normalized == "NM":
        return "nM"

    if normalized in {"UM", "ΜM"}:
        return "μM"

    return None


# ============================================================
# TARGET LOOKUP
# ============================================================

def extract_uniprot_from_target_data(
    target_data,
    target_cache,
):

    if not isinstance(target_data, dict):
        return None

    components = (
        target_data.get("target_components")
        or target_data.get("components")
        or []
    )

    if isinstance(components, dict):
        components = [components]

    accessions = []

    for component in components:

        if not isinstance(component, dict):
            continue

        for field in [
            "accession",
            "accession_number",
            "uniprot_id",
            "uniprot_accession",
        ]:

            value = clean(
                component.get(field)
            )

            if value and value not in accessions:
                accessions.append(value)

        component_id = clean_id(
            component.get("component_id")
        )

        if component_id:

            component_cache_key = (
                "__component__"
                + component_id
            )

            if component_cache_key not in target_cache:

                component_url = (
                    f"{CHEMBL_API}/target_component/"
                    f"{component_id}.json"
                )

                target_cache[
                    component_cache_key
                ] = request_json(
                    component_url
                )

            component_data = target_cache.get(
                component_cache_key
            )

            if isinstance(component_data, dict):

                value = clean(
                    component_data.get("accession")
                )

                if value and value not in accessions:
                    accessions.append(value)

    return (
        accessions[0]
        if accessions
        else None
    )


def get_target(
    target_id,
    target_cache,
):

    target_id = clean_id(target_id)

    if not target_id:
        return None

    cache_key = "__target__" + target_id

    if cache_key in target_cache:
        return target_cache[cache_key]

    url = (
        f"{CHEMBL_API}/target/"
        f"{target_id}.json"
    )

    data = request_json(url)

    if not data:
        target_cache[cache_key] = None
        return None

    result = {
        "target_id": target_id,
        "target_name": clean(
            data.get("pref_name")
        ),
        "target_type": clean(
            data.get("target_type")
        ),
        "target_organism": clean(
            data.get("organism")
            or data.get("target_organism")
        ),
        "uniprot_id": extract_uniprot_from_target_data(
            data,
            target_cache,
        ),
    }

    target_cache[cache_key] = result
    return result


# ============================================================
# FETCH ACTIVITIES
# ============================================================

def get_activities_for_compound(
    chembl_id,
    limit=1000,
):

    print(
        f"      Querying ChEMBL "
        f"for {chembl_id}"
    )

    url = (
        f"{CHEMBL_API}/activity.json"
    )

    params = {
        "molecule_chembl_id":
            chembl_id,

        "limit":
            limit,
    }

    data = request_json(
        url,
        params=params,
    )

    if not data:
        return []

    return data.get(
        "activities",
        [],
    )


# ============================================================
# BUILD ACTIVITY RECORD
# ============================================================

def build_activity_record(
    activity,
    phytochemical_chembl_id,
    target_cache,
):

    standard_type = clean(
        activity.get("standard_type")
    )

    if not standard_type:
        return None

    standard_type = standard_type.upper()

    if standard_type not in ALLOWED_TYPES:
        return None

    display_type = {
        "IC50": "IC50",
        "KI": "Ki",
        "KD": "Kd",
        "EC50": "EC50",
        "POTENCY": "Potency",
    }[standard_type]

    standard_value = activity.get(
        "standard_value"
    )

    if standard_value is None:
        return None

    try:
        standard_value = float(
            standard_value
        )
    except (TypeError, ValueError):
        return None

    raw_units = clean(
        activity.get("standard_units")
    )

    normalized_unit = normalize_unit_name(
        raw_units
    )

    if normalized_unit is None:
        return None

    normalized_nm = normalize_to_nm(
        standard_value,
        raw_units,
    )

    if normalized_nm is None:
        return None

    standard_relation = clean(
        activity.get("standard_relation")
    )

    if standard_relation == ">":
        return None

    target_id = clean_id(
        activity.get("target_chembl_id")
    )

    if not target_id:
        return None

    target_cache_key = "__target__" + target_id
    target = target_cache.get(
        target_cache_key
    )

    if target is None:
        target = get_target(
            target_id,
            target_cache,
        )
        time.sleep(0.05)

    if not target:
        return None

    target_organism = clean(
        target.get("target_organism")
    )

    if target_organism != REQUIRED_TARGET_ORGANISM:
        return None

    target_type = clean(
        target.get("target_type")
    )

    if target_type != REQUIRED_TARGET_TYPE:
        return None

    uniprot_id = clean(
        target.get("uniprot_id")
    )

    if not uniprot_id:
        return None

    if normalized_nm > MAX_POTENCY_NM:
        return None

    assay_confidence = (
        activity.get("assay_confidence_score")
    )

    if assay_confidence is None:
        assay_confidence = activity.get(
            "confidence_score"
        )

    if assay_confidence is not None:
        try:
            assay_confidence = float(
                assay_confidence
            )
        except (TypeError, ValueError):
            assay_confidence = None

    if (
        assay_confidence is not None
        and assay_confidence < 6
    ):
        return None

    confidence = (
        "HIGH"
        if normalized_nm <= HIGH_CONFIDENCE_CUTOFF_NM
        else "MEDIUM"
    )

    return {
        "phytochemical_chembl_id":
            phytochemical_chembl_id,

        "chembl_activity_id":
            clean_id(
                activity.get(
                    "activity_chembl_id"
                )
            ),

        "assay_id":
            clean_id(
                activity.get(
                    "assay_chembl_id"
                )
            ),

        "document_chembl_id":
            clean_id(
                activity.get(
                    "document_chembl_id"
                )
            ),

        "standard_type":
            display_type,

        "standard_relation":
            standard_relation,

        "standard_value":
            standard_value,

        "standard_units":
            normalized_unit,

        "normalized_value_nm":
            normalized_nm,

        "target_name":
            clean(
                target.get(
                    "target_name"
                )
            ),

        "target_id":
            target_id,

        # Internal linkage/filter metadata. These are not added
        # to the existing Bioactivities node schema.
        "target_organism":
            target_organism,

        "target_type":
            target_type,

        "uniprot_id":
            uniprot_id,

        "assay_confidence_score":
            assay_confidence,

        "confidence_tier":
            confidence,

        "source":
            "ChEMBL",
    }


# ============================================================
# LOAD PHYTOCHEMICALS
# ============================================================

def load_phytochemicals(file):

    df = pd.read_excel(file)

    df.columns = [
        str(c).strip().lower()
        for c in df.columns
    ]

    required = {
        "chembl_id",
        "phytochemical_name",
    }

    missing = (
        required
        - set(df.columns)
    )

    if missing:

        raise ValueError(
            "Missing columns: "
            + ", ".join(missing)
        )

    compounds = []

    seen = set()

    for _, row in df.iterrows():

        chembl_id = clean_id(
            row[
                "chembl_id"
            ]
        )

        if not chembl_id:
            continue

        if chembl_id in seen:
            continue

        seen.add(
            chembl_id
        )

        compounds.append({
            "chembl_id":
                chembl_id,

            "name":
                clean(
                    row[
                        "phytochemical_name"
                    ]
                ),
        })

    return compounds


# ============================================================
# DEDUPLICATION
# ============================================================

def activity_key(row):

    return (

        clean_id(
            row[
                "phytochemical_chembl_id"
            ]
        ),

        clean(
            row[
                "standard_type"
            ]
        ),

        clean(
            row[
                "standard_relation"
            ]
        ),

        clean(
            row[
                "standard_value"
            ]
        ),

        clean(
            row[
                "standard_units"
            ]
        ),

        clean_id(
            row[
                "target_id"
            ]
        ),
    )


# ============================================================
# CHECKPOINT
# ============================================================

def save_checkpoint(
    path,
    processed,
    records,
    target_cache,
):

    payload = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "processed": processed,
        "records": records,
        "target_cache": target_cache,
    }

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            payload,
            f,
            indent=2,
        )

def load_checkpoint(path):

    if not Path(path).exists():
        return {
            "processed": [],
            "records": [],
            "target_cache": {},
        }

    try:

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:

            payload = json.load(f)

        if (
            payload.get(
                "checkpoint_version"
            )
            != CHECKPOINT_VERSION
        ):

            print(
                "      Checkpoint version mismatch. "
                "Starting fresh under the new bioactivity filters."
            )

            return {
                "processed": [],
                "records": [],
                "target_cache": {},
            }

        return payload

    except Exception:

        print(
            "      Checkpoint could not "
            "be read. Starting fresh."
        )

        return {
            "processed": [],
            "records": [],
            "target_cache": {},
        }

def build(
    phytochemical_file,
    output_file,
    checkpoint_file,
):

    compounds = load_phytochemicals(
        phytochemical_file
    )

    print("=" * 70)
    print(
        "CLEAN CHEMBL BIOACTIVITY COLLECTOR"
    )
    print("=" * 70)

    print(
        f"Unique phytochemicals: "
        f"{len(compounds)}"
    )

    print(
        "Allowed activity types: "
        "IC50, Ki, Kd, EC50, Potency"
    )

    print(
        "Required: numeric value + nM/μM + "
        "Homo sapiens SINGLE PROTEIN + valid UniProt"
    )

    print(
        "Potency cutoff: <= 10,000 nM; "
        "HIGH <= 1,000 nM; assay confidence >= 6 "
        "when available; relation '>' excluded"
    )

    checkpoint = load_checkpoint(
        checkpoint_file
    )

    processed = set(
        checkpoint.get(
            "processed",
            [],
        )
    )

    records = checkpoint.get(
        "records",
        [],
    )

    target_cache = checkpoint.get(
        "target_cache",
        {},
    )

    # --------------------------------------------------------
    # PROCESS COMPOUNDS
    # --------------------------------------------------------

    for index, compound in enumerate(
        compounds,
        start=1,
    ):

        chembl_id = compound[
            "chembl_id"
        ]

        name = compound[
            "name"
        ]

        if chembl_id in processed:

            print(
                f"[{index}/{len(compounds)}] "
                f"{chembl_id} "
                f"(cached)"
            )

            continue

        print(
            f"\n[{index}/{len(compounds)}] "
            f"{chembl_id}"
        )

        print(
            f"    {name}"
        )

        activities = (
            get_activities_for_compound(
                chembl_id
            )
        )

        print(
            f"    Raw ChEMBL activities: "
            f"{len(activities)}"
        )

        accepted = 0

        for activity in activities:

            record = (
                build_activity_record(
                    activity,
                    chembl_id,
                    target_cache,
                )
            )

            if record is None:
                continue

            records.append(
                record
            )

            accepted += 1

        print(
            f"    Accepted filtered bioactivities: "
            f"{accepted}"
        )

        processed.add(
            chembl_id
        )

        save_checkpoint(
            checkpoint_file,
            sorted(processed),
            records,
            target_cache,
        )

        time.sleep(0.15)

    # ========================================================
    # DEDUPLICATE
    # ========================================================

    print("\n" + "=" * 70)
    print("FINALIZING")
    print("=" * 70)

    unique = {}

    for record in records:

        key = activity_key(
            record
        )

        if key not in unique:

            unique[key] = record

    records = list(
        unique.values()
    )

    # ========================================================
    # CREATE IDs
    # ========================================================

    final_rows = []

    for number, record in enumerate(
        records,
        start=1,
    ):

        final_rows.append({

            "bioactivity_id":
                f"BA_{number:06d}",

            "standard_type":
                record[
                    "standard_type"
                ],

            "standard_relation":
                 record[
                     "standard_relation"
                ] or "NA",

            "standard_value":
                record[
                    "standard_value"
                ],

            "standard_units":
                record[
                    "standard_units"
                ],

            "normalized_value_nm":
                record[
                    "normalized_value_nm"
                ],

            "high_confidence_tier":
                record[
                    "confidence_tier"
                ],
        })

    df = pd.DataFrame(
        final_rows,
        columns=OUTPUT_COLUMNS,
    )

    # ========================================================
    # SAVE
    # ========================================================

    df.to_excel(
        output_file,
        index=False,
    )

    print(
        f"\nFinal bioactivities: "
        f"{len(df)}"
    )

    if http_cache_enabled():
        print(
            f"Shared ChEMBL HTTP cache: hits={_SHARED_CACHE_HITS}, "
            f"misses={_SHARED_CACHE_MISSES}"
        )

    unique_phytochemical_count = len(
        {
            clean_id(
                record.get(
                    "phytochemical_chembl_id"
                )
            )
            for record in records
            if clean_id(
                record.get(
                    "phytochemical_chembl_id"
                )
            )
        }
    )

    print(
        f"Unique phytochemicals with "
        f"activity: "
        f"{unique_phytochemical_count}"
    )

    if not df.empty:

        print(
            "\nActivity distribution:"
        )

        print(
            df[
                "standard_type"
            ]
            .value_counts()
            .to_string()
        )

        print(
            "\nTarget type restricted to:"
        )

        print(
            f"    {REQUIRED_TARGET_TYPE}"
        )

    print(
        f"\nSaved: {output_file}"
    )

    print(
        "\nDone."
    )


# ============================================================
# CLI
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Collect clean ChEMBL "
            "IC50/Ki bioactivities "
            "for phytochemicals."
        )
    )

    parser.add_argument(
        "--file",
        required=True,
        help="Phytochemicals.xlsx",
    )

    parser.add_argument(
        "--output",
        required=True,
        help="Bioactivities.xlsx",
    )

    parser.add_argument(
        "--checkpoint",
        default="bioactivity_checkpoint.json",
        help="Checkpoint JSON file",
    )

    args = parser.parse_args()

    build(
        phytochemical_file=args.file,
        output_file=args.output,
        checkpoint_file=args.checkpoint,
    )


if __name__ == "__main__":
    main()