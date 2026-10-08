import argparse
from pathlib import Path

import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_PHYTO_FILE = "Phytochemicals.xlsx"
DEFAULT_BIO_FILE = "Bioactivities.xlsx"
DEFAULT_OUTPUT_FILE = "Bioactivity_Relationships.xlsx"


# ============================================================
# HELPERS
# ============================================================

def clean_id(value):
    """
    Normalize identifiers only for matching.

    The original value is not modified in the source data.
    """

    if pd.isna(value):
        return ""

    return str(value).strip().upper()


def normalize_columns(df):
    """
    Normalize Excel column names.
    """

    df.columns = [
        str(column).strip().lower()
        for column in df.columns
    ]

    return df


def validate_columns(df, required_columns, filename):

    missing = [
        column
        for column in required_columns
        if column not in df.columns
    ]

    if missing:

        print()
        print(f"FATAL: {filename} is missing required columns:")

        for column in missing:
            print(f"  {column}")

        print()
        print("Available columns:")

        for column in df.columns:
            print(f"  {column}")

        raise SystemExit(1)


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="Build bioactivity relationship mappings."
    )

    parser.add_argument(
        "--phyto",
        default=DEFAULT_PHYTO_FILE,
        help="Phytochemicals Excel file"
    )

    parser.add_argument(
        "--bio",
        default=DEFAULT_BIO_FILE,
        help="Bioactivities Excel file"
    )

    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help="Output Excel file"
    )

    args = parser.parse_args()

    phyto_file = Path(args.phyto)
    bio_file = Path(args.bio)
    output_file = Path(args.output)

    print("=" * 70)
    print("BIOACTIVITY RELATIONSHIP BUILDER")
    print("=" * 70)

    # ========================================================
    # INPUT FILE CHECK
    # ========================================================

    if not phyto_file.exists():

        print()
        print("FATAL: Phytochemicals file not found:")
        print(f"  {phyto_file}")

        raise SystemExit(1)

    if not bio_file.exists():

        print()
        print("FATAL: Bioactivities file not found:")
        print(f"  {bio_file}")

        raise SystemExit(1)

    # ========================================================
    # LOAD FILES
    # ========================================================

    print("\nLoading files...")

    try:

        phyto = pd.read_excel(
            phyto_file
        )

        bio = pd.read_excel(
            bio_file
        )

    except Exception as exc:

        print()
        print("FATAL: Could not read input files.")
        print(exc)

        raise SystemExit(1)

    phyto = normalize_columns(phyto)
    bio = normalize_columns(bio)

    print(
        f"\nPhytochemical rows: {len(phyto)}"
    )

    print(
        f"Bioactivity rows:   {len(bio)}"
    )

    # ========================================================
    # REQUIRED COLUMNS
    # ========================================================

    validate_columns(
        phyto,
        {
            "imppat_id",
            "chembl_id",
        },
        "Phytochemicals.xlsx"
    )

    validate_columns(
        bio,
        {
            "bioactivity_id",
            "phytochemical_chembl_id",
            "target_id",
        },
        "Bioactivities.xlsx"
    )

    # ========================================================
    # NORMALIZE JOIN KEYS
    # ========================================================

    phyto["_chembl_match"] = (
        phyto["chembl_id"]
        .apply(clean_id)
    )

    phyto["_imppat_match"] = (
        phyto["imppat_id"]
        .apply(clean_id)
    )

    bio["_chembl_match"] = (
        bio["phytochemical_chembl_id"]
        .apply(clean_id)
    )

    bio["_bioactivity_id"] = (
        bio["bioactivity_id"]
        .astype(str)
        .str.strip()
    )

    bio["_target_match"] = (
        bio["target_id"]
        .apply(clean_id)
    )

    # ========================================================
    # PHYTOCHEMICAL -> BIOACTIVITY
    #
    # IMPORTANT:
    #
    # ChEMBL is ONLY the join key.
    #
    # Bioactivities:
    #
    #     phytochemical_chembl_id
    #
    # joins to:
    #
    #     Phytochemicals.chembl_id
    #
    # But the relationship Source Id is:
    #
    #     Phytochemicals.imppat_id
    #
    # We do NOT require ChEMBL -> IMPPAT to be one-to-one.
    #
    # Every valid matching IMPPAT relationship is retained.
    # ========================================================

    print(
        "\nBuilding Phytochemical -> Bioactivity relationships..."
    )

    valid_phyto = phyto[
        (phyto["_chembl_match"] != "") &
        (phyto["_imppat_match"] != "")
    ].copy()

    print(
        "Unique ChEMBL IDs in Phytochemicals: "
        f"{valid_phyto['_chembl_match'].nunique()}"
    )

    # --------------------------------------------------------
    # JOIN
    # --------------------------------------------------------

    matched_bio = bio[
        bio["_chembl_match"] != ""
    ].merge(
        valid_phyto[
            [
                "_chembl_match",
                "_imppat_match",
            ]
        ],
        on="_chembl_match",
        how="inner"
    )

    print(
        "Matched bioactivity rows: "
        f"{len(matched_bio)}"
    )

    # --------------------------------------------------------
    # BUILD RELATIONSHIPS
    # --------------------------------------------------------

    phyto_bio = pd.DataFrame({

        "Source":
            "Phytochemicals",

        "Source Id":
            matched_bio[
                "_imppat_match"
            ],

        "Type":
            "HAS_BIOACTIVITY",

        "Destination":
            "Bioactivity",

        "Destination Id":
            matched_bio[
                "_bioactivity_id"
            ],

        "Label":
            "has_bioactivity",

        "Description":
            "Associated phytochemical has respective bioactivity",
    })

    phyto_bio = (
        phyto_bio
        .drop_duplicates()
        .reset_index(drop=True)
    )

    # ========================================================
    # BIOACTIVITY -> TARGET
    #
    # ORIGINAL LOGIC PRESERVED
    # ========================================================

    print(
        "\nBuilding Bioactivity -> Target relationships..."
    )

    valid_target = bio[
        (bio["_bioactivity_id"] != "") &
        (bio["_target_match"] != "")
    ].copy()

    bio_target = pd.DataFrame({

        "Source":
            "Bioactivity",

        "Source Id":
            valid_target[
                "_bioactivity_id"
            ],

        "Type":
            "AGAINST_TARGET",

        "Destination":
            "Target",

        "Destination Id":
            valid_target[
                "_target_match"
            ],

        "Label":
            "against_target",

        "Description":
            "Bioactivity is associated with respective biological target",
    })

    bio_target = (
        bio_target
        .drop_duplicates()
        .reset_index(drop=True)
    )

    # ========================================================
    # OUTPUT DIRECTORY
    # ========================================================

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    # ========================================================
    # WRITE EXCEL
    # ========================================================

    print("\nWriting output...")

    try:

        with pd.ExcelWriter(
            output_file,
            engine="openpyxl",
            mode="w"
        ) as writer:

            phyto_bio.to_excel(
                writer,
                sheet_name="Phyto_Bioactivity",
                index=False
            )

            bio_target.to_excel(
                writer,
                sheet_name="Bioactivity_Target",
                index=False
            )

    except Exception as exc:

        print()
        print("FATAL: Could not write output file.")
        print(exc)

        raise SystemExit(1)

    # ========================================================
    # VERIFY OUTPUT
    # ========================================================

    if not output_file.exists():

        print()
        print("FATAL: Output file was not created:")
        print(f"  {output_file}")

        raise SystemExit(1)

    try:

        workbook = pd.ExcelFile(
            output_file,
            engine="openpyxl"
        )

        sheets = workbook.sheet_names

    except Exception as exc:

        print()
        print("FATAL: Output file could not be reopened.")
        print(exc)

        raise SystemExit(1)

    required_sheets = {
        "Phyto_Bioactivity",
        "Bioactivity_Target",
    }

    if not required_sheets.issubset(set(sheets)):

        print()
        print("FINAL CHECK: FAIL")
        print("Required output sheets are missing.")

        print()
        print("Found:")

        for sheet in sheets:
            print(f"  {sheet}")

        raise SystemExit(1)

    # ========================================================
    # VALIDATION
    # ========================================================

    # Every Source Id in the phytochemical relationship
    # must be an IMPPAT ID, NOT a ChEMBL ID.

    invalid_source_ids = phyto_bio[
        ~phyto_bio["Source Id"]
        .astype(str)
        .str.upper()
        .str.startswith("IMPHY")
    ]

    if len(invalid_source_ids) > 0:

        print()
        print("FINAL CHECK: FAIL")
        print(
            "Phytochemical relationship contains "
            "non-IMPPAT Source Id values."
        )

        print(
            invalid_source_ids.head(20)
            .to_string(index=False)
        )

        raise SystemExit(1)

    # Bioactivity IDs should remain BA_*.

    invalid_bioactivity_ids = phyto_bio[
        ~phyto_bio["Destination Id"]
        .astype(str)
        .str.upper()
        .str.startswith("BA_")
    ]

    if len(invalid_bioactivity_ids) > 0:

        print()
        print("FINAL CHECK: FAIL")
        print(
            "Invalid Bioactivity Destination Id detected."
        )

        raise SystemExit(1)

    # ========================================================
    # REPORT
    # ========================================================

    unique_imppat = (
        phyto_bio[
            "Source Id"
        ]
        .nunique()
    )

    unique_bioactivities = (
        phyto_bio[
            "Destination Id"
        ]
        .nunique()
    )

    unique_targets = (
        bio_target[
            "Destination Id"
        ]
        .nunique()
    )

    print()
    print("=" * 70)
    print("RESULT")
    print("=" * 70)

    print(
        f"Phytochemical rows: "
        f"{len(phyto)}"
    )

    print(
        f"Bioactivity rows:   "
        f"{len(bio)}"
    )

    print()

    print(
        f"Phytochemical -> Bioactivity relationships: "
        f"{len(phyto_bio)}"
    )

    print(
        f"Unique IMPPAT phytochemicals: "
        f"{unique_imppat}"
    )

    print(
        f"Unique bioactivities linked: "
        f"{unique_bioactivities}"
    )

    print()

    print(
        f"Bioactivity -> Target relationships: "
        f"{len(bio_target)}"
    )

    print(
        f"Unique targets: "
        f"{unique_targets}"
    )

    print()

    print(
        f"Output: "
        f"{output_file}"
    )

    print()

    print("Sheets:")

    for sheet in sheets:
        print(f"  {sheet}")

    # ========================================================
    # SAMPLE
    # ========================================================

    print()
    print("Example Phytochemical -> Bioactivity:")

    if len(phyto_bio):

        print(
            phyto_bio
            .head(5)
            .to_string(index=False)
        )

    else:

        print("  No relationships.")

    print()
    print("Example Bioactivity -> Target:")

    if len(bio_target):

        print(
            bio_target
            .head(5)
            .to_string(index=False)
        )

    else:

        print("  No relationships.")

    # ========================================================
    # FINAL
    # ========================================================

    print()
    print("=" * 70)
    print("FINAL CHECK: PASS")
    print("=" * 70)

    print(
        "ChEMBL used only as the join key."
    )

    print(
        "Phytochemical Source Id = IMPPAT ID."
    )

    print(
        "Bioactivity -> Target logic preserved."
    )

    print(
        "No ChEMBL -> IMPPAT one-to-one assumption."
    )

    print(
        "No relationship discarded because of duplicate "
        "ChEMBL mappings."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()