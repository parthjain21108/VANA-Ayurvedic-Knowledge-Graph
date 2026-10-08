#!/usr/bin/env python3
"""
Build the Plant_part -> Phytochemicals CONTAINS relationship sheet.

Input:
    Phytochemical_Mapping.xlsx

Expected input columns:
    plant_part_id
    plant_name
    plant_part
    imppat_id
    phytochemical_name

Output:
    Contains.xlsx

Output columns:
    Source
    Source Id
    Type
    Destination
    Destination Id
    Label
    Description

Relationship:
    Plant_part --CONTAINS--> Phytochemicals

IMPORTANT:
    Source Id = plant_part_id
    Destination Id = inchi_key

    The input mapping still carries imppat_id. It is used only as
    an intermediate lookup into the final Phytochemicals node file,
    where inchi_key is the canonical Phytochemical node ID.

Example:
    Plant_part | AC-LE-BA | CONTAINS | Phytochemicals | IMPHY007273
"""

import argparse
import sys
from pathlib import Path

import pandas as pd


OUTPUT_COLUMNS = [
    "Source",
    "Source Id",
    "Type",
    "Destination",
    "Destination Id",
    "Label",
    "Description",
]


def clean(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def find_column(df, candidates):
    normalized = {str(c).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        if candidate.lower() in normalized:
            return normalized[candidate.lower()]
    return None


def load_mapping(path):
    # Read first sheet by default.
    df = pd.read_excel(path)

    plant_part_col = find_column(
        df,
        ["plant_part_id", "plant part id", "plantpart_id"],
    )
    imppat_col = find_column(
        df,
        ["imppat_id", "imppat id", "imppat"],
    )

    if not plant_part_col or not imppat_col:
        raise ValueError(
            "Input must contain plant_part_id and imppat_id columns.\n"
            f"Found columns: {list(df.columns)}"
        )

    return df, plant_part_col, imppat_col


def build(
    df,
    plant_part_col,
    imppat_col,
    phytochemical_file,
):
    rows = []
    seen = set()

    phytochemical_file = Path(
        phytochemical_file
    )

    if not phytochemical_file.exists():
        raise FileNotFoundError(
            f"Phytochemicals.xlsx not found: {phytochemical_file}"
        )

    phyto = pd.read_excel(
        phytochemical_file
    )

    phyto.columns = [
        str(column).strip()
        for column in phyto.columns
    ]

    required = [
        "imppat_id",
        "inchi_key",
    ]

    missing = [
        column
        for column in required
        if column not in phyto.columns
    ]

    if missing:
        raise ValueError(
            "Phytochemicals.xlsx is missing required columns: "
            + ", ".join(missing)
            + f"\nFound columns: {list(phyto.columns)}"
        )

    # The raw mapping uses IMPPAT IDs. Resolve each one to the
    # canonical Phytochemical node primary key: InChI Key.
    imppat_to_inchi = {}

    for _, phyto_row in phyto.iterrows():

        imppat_id = clean(
            phyto_row["imppat_id"]
        )

        inchi_key = clean(
            phyto_row["inchi_key"]
        )

        if not imppat_id or not inchi_key:
            continue

        imppat_to_inchi.setdefault(
            imppat_id,
            inchi_key,
        )

    unresolved = 0

    for _, row in df.iterrows():

        plant_part_id = clean(
            row[plant_part_col]
        )

        imppat_id = clean(
            row[imppat_col]
        )

        if not plant_part_id or not imppat_id:
            continue

        inchi_key = imppat_to_inchi.get(
            imppat_id
        )

        if not inchi_key:
            unresolved += 1
            continue

        key = (
            plant_part_id,
            inchi_key,
        )

        if key in seen:
            continue

        seen.add(key)

        rows.append(
            {
                "Source": "Plant_part",
                "Source Id": plant_part_id,
                "Type": "CONTAINS",
                "Destination": "Phytochemicals",
                "Destination Id": inchi_key,
                "Label": "contains",
                "Description": (
                    "Associated plant part contains respective phytochemicals"
                ),
            }
        )

    if unresolved:
        print(
            f"Unresolved IMPPAT -> InChI Key mappings: "
            f"{unresolved}"
        )

    return pd.DataFrame(
        rows,
        columns=OUTPUT_COLUMNS,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Build Plant_part -> Phytochemicals CONTAINS relationships."
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Input Phytochemical_Mapping.xlsx",
    )
    parser.add_argument(
        "--phytochemicals",
        required=True,
        help="Final Phytochemicals.xlsx node file",
    )
    parser.add_argument(
        "--output",
        default="Contains.xlsx",
        help="Output Excel filename",
    )

    args = parser.parse_args()

    input_path = Path(args.file)
    phytochemical_path = Path(
        args.phytochemicals
    )
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}")
        sys.exit(1)

    print("=" * 70)
    print("PLANT-PART -> PHYTOCHEMICAL CONTAINS BUILDER")
    print("=" * 70)

    try:
        df, plant_part_col, imppat_col = load_mapping(input_path)
        print(f"Mapping rows: {len(df)}")
        print(f"Using plant-part ID column: {plant_part_col}")
        print(
            f"Using IMPPAT ID column for lookup: "
            f"{imppat_col}"
        )
        print(
            f"Resolving canonical Phytochemical IDs "
            f"from: {phytochemical_path}"
        )

        output_df = build(
            df,
            plant_part_col,
            imppat_col,
            phytochemical_path,
        )

        output_df.to_excel(output_path, index=False)

        print(f"Unique CONTAINS relationships: {len(output_df)}")
        print(f"Output: {output_path}")

        if len(output_df):
            print("\nSample:")
            print(output_df.head(10).to_string(index=False))

    except Exception as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()