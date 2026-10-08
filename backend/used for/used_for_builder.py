#!/usr/bin/env python3

import argparse
import re
from pathlib import Path

import pandas as pd


# ============================================================
# CONFIG
# ============================================================

OUTPUT_COLUMNS = [
    "Source",
    "Source Id",
    "Type",
    "Destination",
    "Destination Id",
    "Label",
    "Description",
]

AUDIT_COLUMNS = [
    "plant_part_id",
    "plant_name",
    "plant_part",
    "therapeutic_use",
    "reason",
]


# ============================================================
# HELPERS
# ============================================================

def clean(value):
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    return str(value).strip()


def clean_id(value):
    return clean(value).upper()


def normalize(value):
    """
    Normalize text only for matching.
    Does not change the actual stored value.
    """
    value = clean(value)

    if not value:
        return ""

    value = value.lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def require_columns(
    dataframe,
    required,
    filename,
):
    missing = [
        column
        for column in required
        if column not in dataframe.columns
    ]

    if missing:
        raise ValueError(
            f"{filename} is missing required columns:\n"
            + "\n".join(
                f"  {column}"
                for column in missing
            )
            + "\n\nAvailable columns:\n"
            + "\n".join(
                f"  {column}"
                for column in dataframe.columns
            )
        )


# ============================================================
# LOAD THERAPEUTIC RAW DATA
# ============================================================

def load_fi3(path):
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(
            f"Therapeutic raw file not found:\n{path}"
        )

    df = pd.read_excel(path)

    df.columns = [
        str(column).strip()
        for column in df.columns
    ]

    required = [
        "plant_part_id",
        "plant_name",
        "plant_part",
        "therapeutic_use",
    ]

    require_columns(
        df,
        required,
        path.name,
    )

    df = df[
        [
            "plant_part_id",
            "plant_name",
            "plant_part",
            "therapeutic_use",
        ]
    ].copy()

    for column in df.columns:
        df[column] = df[column].map(clean)

    df = df[
        (df["plant_part_id"] != "")
        & (df["plant_name"] != "")
        & (df["plant_part"] != "")
        & (df["therapeutic_use"] != "")
    ].copy()

    return df.reset_index(drop=True)


# ============================================================
# LOAD THERAPEUTIC MAPPING
# ============================================================

def load_fi5(path):
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(
            f"Therapeutic mapping file not found:\n{path}"
        )

    df = pd.read_excel(path)

    df.columns = [
        str(column).strip()
        for column in df.columns
    ]

    required = [
        "plant_part_id",
        "therapeutic_use",
        "mesh_id",
    ]

    require_columns(
        df,
        required,
        path.name,
    )

    df = df[
        [
            "plant_part_id",
            "therapeutic_use",
            "mesh_id",
        ]
    ].copy()

    for column in df.columns:
        df[column] = df[column].map(clean)

    df = df[
        (df["plant_part_id"] != "")
        & (df["therapeutic_use"] != "")
        & (df["mesh_id"] != "")
    ].copy()

    df["plant_part_id_key"] = (
        df["plant_part_id"].map(clean_id)
    )

    df["mesh_id_key"] = (
        df["mesh_id"].map(clean_id)
    )

    return df.reset_index(drop=True)


# ============================================================
# BUILD LOOKUP
# ============================================================

def build_therapeutic_lookup(fi5):
    """
    Build the authoritative lookup using the actual relationship
    identity:

        PlantPart ID + MeSH ID -> resolved therapeutic use

    We intentionally DO NOT match on therapeutic_use text because
    the raw collector stores identifier bundles such as:

        MESH:D002492 , UMLS:C0007681 , ICD-11:XM6QP4

    while the resolved mapping stores the human-readable MeSH name.
    """

    lookup = {}

    duplicate_keys = 0

    for _, row in fi5.iterrows():

        plant_part_id = clean_id(
            row["plant_part_id"]
        )

        mesh_id = clean_id(
            row["mesh_id"]
        )

        therapeutic_use = clean(
            row["therapeutic_use"]
        )

        if not plant_part_id:
            continue

        if not mesh_id:
            continue

        key = (
            plant_part_id,
            mesh_id,
        )

        if key in lookup:

            existing = lookup[key]

            if existing != therapeutic_use:
                print(
                    "\nWARNING:"
                )

                print(
                    f"Multiple therapeutic names found "
                    f"for PlantPart={plant_part_id}, "
                    f"MeSH={mesh_id}"
                )

                print(
                    f"Existing: {existing}"
                )

                print(
                    f"New:      {therapeutic_use}"
                )

                print(
                    "Keeping the first name."
                )

            duplicate_keys += 1
            continue

        lookup[key] = therapeutic_use

    print()
    print(
        f"Resolved PlantPart/MeSH mappings: "
        f"{len(lookup)}"
    )

    if duplicate_keys:
        print(
            f"Duplicate mapping rows ignored: "
            f"{duplicate_keys}"
        )

    return lookup


# ============================================================
# BUILD USED_FOR RELATIONSHIPS
# ============================================================

def build_relationships(
    fi3,
    therapeutic_lookup,
):
    relationships = []
    unresolved = []

    seen = set()

    print()
    print(
        "Building PlantPart -> TherapeuticUse "
        "relationships..."
    )

    for _, row in fi3.iterrows():

        plant_part_id = clean(
            row["plant_part_id"]
        )

        plant_name = clean(
            row["plant_name"]
        )

        plant_part = clean(
            row["plant_part"]
        )

        raw_therapeutic_use = clean(
            row["therapeutic_use"]
        )

        # ----------------------------------------------------
        # Invalid row checks
        # ----------------------------------------------------

        if not plant_part_id:
            unresolved.append({
                "plant_part_id": "",
                "plant_name": plant_name,
                "plant_part": plant_part,
                "therapeutic_use": raw_therapeutic_use,
                "reason": "Missing plant_part_id",
            })
            continue

        if not raw_therapeutic_use:
            unresolved.append({
                "plant_part_id": plant_part_id,
                "plant_name": plant_name,
                "plant_part": plant_part,
                "therapeutic_use": "",
                "reason": "Missing therapeutic_use",
            })
            continue

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # The raw collector stores the MeSH identifier inside
        # therapeutic_use, e.g.:
        #
        #   MESH:D002492 , UMLS:C0007681 , ICD-11:XM6QP4
        #
        # The resolved mapping stores:
        #
        #   plant_part_id | mesh_id | human-readable name
        #
        # Therefore the relationship key is:
        #
        #   plant_part_id + extracted mesh_id
        #
        # NOT raw therapeutic_use text.
        # ----------------------------------------------------

        mesh_match = re.search(
            r"\b(?:MESH\s*:\s*)?(D\d{6})\b",
            raw_therapeutic_use,
            flags=re.IGNORECASE,
        )

        if not mesh_match:

            unresolved.append({
                "plant_part_id":
                    plant_part_id,
                "plant_name":
                    plant_name,
                "plant_part":
                    plant_part,
                "therapeutic_use":
                    raw_therapeutic_use,
                "reason":
                    "No MeSH ID found in raw therapeutic_use",
            })

            continue

        mesh_id = clean_id(
            mesh_match.group(1)
        )

        key = (
            clean_id(plant_part_id),
            mesh_id,
        )

        therapeutic_use = (
            therapeutic_lookup.get(key)
        )

        if not therapeutic_use:

            unresolved.append({
                "plant_part_id":
                    plant_part_id,
                "plant_name":
                    plant_name,
                "plant_part":
                    plant_part,
                "therapeutic_use":
                    raw_therapeutic_use,
                "reason":
                    "No matching resolved "
                    "PlantPart + MeSH mapping",
            })

            continue

        # ----------------------------------------------------
        # Relationship identity
        # ----------------------------------------------------

        relationship_key = (
            clean_id(plant_part_id),
            mesh_id,
        )

        if relationship_key in seen:
            continue

        seen.add(
            relationship_key
        )

        relationships.append({
            "Source":
                "PlantPart",

            "Source Id":
                plant_part_id,

            "Type":
                "USED_FOR",

            "Destination":
                "TherapeuticUse",

            "Destination Id":
                mesh_id,

            "Label":
                "used_for",

            "Description":
                "Plant part is associated with the "
                "respective therapeutic use",
        })

    result = pd.DataFrame(
        relationships,
        columns=OUTPUT_COLUMNS,
    )

    audit = pd.DataFrame(
        unresolved,
        columns=AUDIT_COLUMNS,
    )

    return result, audit


# ============================================================
# FINAL VALIDATION
# ============================================================

def final_validation(
    result,
    expected_minimum=0,
):
    if result.empty:
        if expected_minimum > 0:
            raise ValueError(
                "No USED_FOR relationships were generated "
                "even though resolved PlantPart/MeSH mappings "
                "were available."
            )

        print(
            "\nWARNING: No USED_FOR relationships "
            "were generated."
        )
        return

    # --------------------------------------------------------
    # Source must always be PlantPart
    # --------------------------------------------------------

    invalid_sources = result[
        result["Source"] != "PlantPart"
    ]

    if not invalid_sources.empty:
        raise ValueError(
            "USED_FOR contains invalid Source values."
        )

    # --------------------------------------------------------
    # Destination must always be TherapeuticUse
    # --------------------------------------------------------

    invalid_destinations = result[
        result["Destination"]
        != "TherapeuticUse"
    ]

    if not invalid_destinations.empty:
        raise ValueError(
            "USED_FOR contains invalid Destination "
            "values."
        )

    # --------------------------------------------------------
    # Type must always be USED_FOR
    # --------------------------------------------------------

    invalid_types = result[
        result["Type"] != "USED_FOR"
    ]

    if not invalid_types.empty:
        raise ValueError(
            "USED_FOR contains invalid Type values."
        )

    # --------------------------------------------------------
    # Destination ID must be a MeSH ID
    #
    # We do not invent a new therapeutic ID.
    # --------------------------------------------------------

    if result["Destination Id"].isna().any():
        raise ValueError(
            "USED_FOR contains empty Destination Id."
        )

    # --------------------------------------------------------
    # No duplicate relationships
    # --------------------------------------------------------

    duplicates = result[
        result.duplicated(
            subset=[
                "Source Id",
                "Destination Id",
                "Type",
            ],
            keep=False,
        )
    ]

    if not duplicates.empty:
        raise ValueError(
            "Duplicate USED_FOR relationships found."
        )

    print()
    print(
        "USED_FOR validation: PASSED"
    )


# ============================================================
# SAVE EXCEL
# ============================================================

def save_excel(
    dataframe,
    output_file,
):
    output_file = Path(
        output_file
    )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    dataframe.to_excel(
        output_file,
        index=False,
        engine="openpyxl",
    )


# ============================================================
# MAIN BUILD
# ============================================================

def build(
    input_file,
    mapping_file,
    output_file,
    audit_file=None,
):

    input_file = Path(
        input_file
    )

    mapping_file = Path(
        mapping_file
    )

    output_file = Path(
        output_file
    )

    if audit_file:
        audit_file = Path(
            audit_file
        )
    else:
        audit_file = (
            output_file.parent
            / "UsedFor_unresolved.xlsx"
        )

    print(
        "=" * 72
    )

    print(
        "USED_FOR RELATIONSHIP BUILDER"
    )

    print(
        "=" * 72
    )

    print()
    print(
        f"Raw therapeutic input:\n"
        f"  {input_file}"
    )

    print(
        f"Therapeutic mapping:\n"
        f"  {mapping_file}"
    )

    print(
        f"Output:\n"
        f"  {output_file}"
    )

    fi3 = load_fi3(
        input_file
    )

    fi5 = load_fi5(
        mapping_file
    )

    print()
    print(
        f"Raw rows:     {len(fi3)}"
    )

    print(
        f"Mapping rows: {len(fi5)}"
    )

    therapeutic_lookup = (
        build_therapeutic_lookup(
            fi5
        )
    )

    result, unresolved = (
        build_relationships(
            fi3,
            therapeutic_lookup,
        )
    )

    final_validation(
        result,
        expected_minimum=len(
            therapeutic_lookup
        ),
    )

    # --------------------------------------------------------
    # SAVE MAIN OUTPUT
    # --------------------------------------------------------

    save_excel(
        result,
        output_file,
    )

    # --------------------------------------------------------
    # SAVE AUDIT
    #
    # BUG FIX:
    # Do NOT use the list/dataframe variable `audit`
    # as the file path.
    #
    # The unresolved rows are `audit`.
    # The output filename is `audit_file`.
    # --------------------------------------------------------

    if not unresolved.empty:

        audit_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        save_excel(
            unresolved,
            audit_file,
        )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print()
    print(
        "=" * 72
    )

    print(
        "FINAL RESULT"
    )

    print(
        "=" * 72
    )

    print(
        f"USED_FOR relationships: "
        f"{len(result)}"
    )

    print(
        f"Unresolved rows:         "
        f"{len(unresolved)}"
    )

    print(
        f"Output:                  "
        f"{output_file}"
    )

    if not unresolved.empty:

        print(
            f"Audit:                   "
            f"{audit_file}"
        )

    else:

        print(
            "Audit:                   none"
        )

    print()
    print(
        "Done."
    )


# ============================================================
# CLI
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Build PlantPart -> TherapeuticUse "
            "USED_FOR relationships."
        )
    )

    parser.add_argument(
        "--file",
        required=True,
        help=(
            "Therapeutic_uses_raw.xlsx"
        ),
    )

    parser.add_argument(
        "--mapping",
        required=True,
        help=(
            "Therapeutic_Use_Mapping.xlsx"
        ),
    )

    parser.add_argument(
        "--output",
        required=True,
        help=(
            "Used_For.xlsx"
        ),
    )

    parser.add_argument(
        "--audit",
        default=None,
        help=(
            "Optional unresolved audit Excel file. "
            "Defaults to UsedFor_unresolved.xlsx"
        ),
    )

    args = parser.parse_args()

    build(
        args.file,
        args.mapping,
        args.output,
        args.audit,
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print(
            "\nStopped by user."
        )

        raise SystemExit(130)

    except Exception as exc:

        print()
        print(
            "=" * 72
        )

        print(
            "USED_FOR BUILDER FAILED"
        )

        print(
            "=" * 72
        )

        print(
            exc
        )

        raise SystemExit(1)