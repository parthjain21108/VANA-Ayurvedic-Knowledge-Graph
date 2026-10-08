import time
import requests
import pandas as pd
from collections import defaultdict


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"

BIOACTIVITY_FILE = "Bioactivities.xlsx"
DOCUMENT_FILE = "Documents.xlsx"

OUTPUT_RELATIONSHIP = "Bioactivity_Document_Relationships.xlsx"

# Optional: also create a copy of Bioactivities with the
# recovered document ChEMBL ID.
OUTPUT_BIOACTIVITY = "Bioactivities_with_Document.xlsx"

REQUEST_TIMEOUT = 30
RETRIES = 4


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "Bioactivity-Document-Mapper/1.0",
    "Accept": "application/json",
})


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


def clean_type(value):
    return clean(value).upper()


def normalize_number(value):
    value = clean(value)

    if not value:
        return ""

    try:
        number = float(value)

        if number.is_integer():
            return str(int(number))

        return format(number, ".15g")

    except Exception:
        return value.upper()


def normalize_relation(value):
    value = clean(value)

    if not value:
        return ""

    return value.upper()


# ============================================================
# BUILD ACTIVITY MATCH KEY
# ============================================================

def make_key(
    phytochemical_id,
    standard_type,
    standard_relation,
    standard_value,
    target_id,
):
    return (
        clean_id(phytochemical_id),
        clean_type(standard_type),
        normalize_relation(standard_relation),
        normalize_number(standard_value),
        clean_id(target_id),
    )


# ============================================================
# CHOOSE DOCUMENT
# ============================================================

def document_sort_key(row):
    """
    Prefer newest document year.

    If year is unavailable, put it last.
    """

    year = clean(row.get("document_year"))

    try:
        return int(float(year))
    except Exception:
        return -1


# ============================================================
# GET CHemBL ACTIVITIES
# ============================================================

def get_activities(chembl_id):

    url = f"{BASE_URL}/activity.json"

    params = {
        "molecule_chembl_id": chembl_id,
        "limit": 1000,
    }

    for attempt in range(1, RETRIES + 1):

        try:

            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 200:

                data = response.json()

                return data.get(
                    "activities",
                    []
                )

            if response.status_code == 429:

                wait = min(
                    2 ** attempt,
                    20
                )

                print(
                    f"      HTTP 429 - "
                    f"waiting {wait}s"
                )

                time.sleep(wait)

                continue

            if response.status_code in {
                500,
                502,
                503,
                504,
            }:

                wait = min(
                    2 ** attempt,
                    20
                )

                print(
                    f"      HTTP "
                    f"{response.status_code} - "
                    f"retrying in {wait}s"
                )

                time.sleep(wait)

                continue

            print(
                f"      HTTP "
                f"{response.status_code}"
            )

            return []

        except requests.RequestException as exc:

            print(
                f"      Request error: {exc}"
            )

            if attempt < RETRIES:

                wait = min(
                    2 ** attempt,
                    20
                )

                time.sleep(wait)

    return []


# ============================================================
# LOAD BIOACTIVITIES
# ============================================================

def load_bioactivities():

    print("\n" + "=" * 70)
    print("LOADING BIOACTIVITIES")
    print("=" * 70)

    bio = pd.read_excel(
        BIOACTIVITY_FILE
    )

    bio.columns = [
        str(c).strip().lower()
        for c in bio.columns
    ]

    required = {
        "bioactivity_id",
        "phytochemical_chembl_id",
        "standard_type",
        "standard_value",
        "standard_units",
        "target_id",
    }

    missing = (
        required
        - set(bio.columns)
    )

    if missing:

        raise ValueError(
            "Bioactivities.xlsx is missing:\n"
            + "\n".join(
                sorted(missing)
            )
        )

    # standard_relation is optional because your current
    # final sheet may or may not contain it.

    if "standard_relation" not in bio.columns:

        bio["standard_relation"] = ""

    print(
        f"Rows: {len(bio)}"
    )

    print(
        "Columns:"
    )

    for col in bio.columns:
        print(
            f"  {col}"
        )

    return bio


# ============================================================
# LOAD DOCUMENTS
# ============================================================

def load_documents():

    print("\n" + "=" * 70)
    print("LOADING EXISTING DOCUMENTS")
    print("=" * 70)

    documents = pd.read_excel(
        DOCUMENT_FILE
    )

    documents.columns = [
        str(c).strip().lower()
        for c in documents.columns
    ]

    required = {
        "document_id",
        "document_chembl_id",
        "document_year",
    }

    missing = (
        required
        - set(documents.columns)
    )

    if missing:

        raise ValueError(
            "Documents.xlsx is missing:\n"
            + "\n".join(
                sorted(missing)
            )
        )

    documents["document_chembl_id"] = (
        documents["document_chembl_id"]
        .apply(clean_id)
    )

    print(
        f"Rows: {len(documents)}"
    )

    return documents


# ============================================================
# BUILD DOCUMENT LOOKUP
# ============================================================

def build_document_lookup(documents):

    lookup = {}

    for _, row in documents.iterrows():

        chembl_id = clean_id(
            row["document_chembl_id"]
        )

        if not chembl_id:
            continue

        lookup[chembl_id] = row.to_dict()

    return lookup


# ============================================================
# FIND DOCUMENTS
# ============================================================

def build_activity_document_index(
    bio,
    document_lookup,
):

    print("\n" + "=" * 70)
    print("RECOVERING ACTIVITY → DOCUMENT MAPPINGS")
    print("=" * 70)

    # --------------------------------------------------------
    # Unique phytochemicals
    # --------------------------------------------------------

    compounds = sorted(
        set(
            clean_id(x)
            for x in bio[
                "phytochemical_chembl_id"
            ]
            if clean_id(x)
        )
    )

    print(
        f"Unique phytochemicals: "
        f"{len(compounds)}"
    )

    # --------------------------------------------------------
    # Activity key → document IDs
    # --------------------------------------------------------

    activity_documents = defaultdict(set)

    for number, compound in enumerate(
        compounds,
        1,
    ):

        print(
            f"\n[{number}/{len(compounds)}] "
            f"{compound}"
        )

        activities = get_activities(
            compound
        )

        print(
            f"    ChEMBL activities: "
            f"{len(activities)}"
        )

        for activity in activities:

            activity_type = clean_type(
                activity.get(
                    "standard_type"
                )
            )

            activity_relation = normalize_relation(
                activity.get(
                    "standard_relation"
                )
            )

            activity_value = normalize_number(
                activity.get(
                    "standard_value"
                )
            )

            activity_target = clean_id(
                activity.get(
                    "target_chembl_id"
                )
            )

            document_chembl_id = clean_id(
                activity.get(
                    "document_chembl_id"
                )
            )

            if not activity_type:
                continue

            if not activity_value:
                continue

            if not activity_target:
                continue

            if not document_chembl_id:
                continue

            # ----------------------------------------------
            # IMPORTANT
            #
            # We use the same activity identity that was
            # used when collecting the documents.
            # ----------------------------------------------

            key = make_key(
                compound,
                activity_type,
                activity_relation,
                activity_value,
                activity_target,
            )

            # Only keep documents that actually exist
            # in your existing Documents.xlsx.
            if document_chembl_id in document_lookup:

                activity_documents[
                    key
                ].add(
                    document_chembl_id
                )

        # Small delay to avoid hammering ChEMBL.
        time.sleep(0.10)

    return activity_documents


# ============================================================
# MAP BIOACTIVITY → DOCUMENT
# ============================================================

def map_bioactivities(
    bio,
    documents,
    activity_documents,
):

    print("\n" + "=" * 70)
    print("MAPPING BIOACTIVITIES → DOCUMENTS")
    print("=" * 70)

    document_lookup = build_document_lookup(
        documents
    )

    relationship_rows = []

    mapped_document_chembl = []

    matched = 0
    unmatched = 0
    multiple_candidates = 0

    for _, row in bio.iterrows():

        bio_id = clean(
            row["bioactivity_id"]
        )

        key = make_key(
            row["phytochemical_chembl_id"],
            row["standard_type"],
            row["standard_relation"],
            row["standard_value"],
            row["target_id"],
        )

        candidate_ids = activity_documents.get(
            key,
            set()
        )

        if not candidate_ids:

            unmatched += 1

            mapped_document_chembl.append(
                ""
            )

            continue

        if len(candidate_ids) > 1:

            multiple_candidates += 1

        # ----------------------------------------------------
        # Only candidates that exist in our Documents sheet.
        # ----------------------------------------------------

        candidates = []

        for document_chembl_id in candidate_ids:

            document = document_lookup.get(
                document_chembl_id
            )

            if document is None:
                continue

            candidates.append(
                document
            )

        if not candidates:

            unmatched += 1

            mapped_document_chembl.append(
                ""
            )

            continue

        # ----------------------------------------------------
        # SELECT LATEST DOCUMENT
        #
        # This replaces the old script's expensive:
        #
        # get_document()
        #
        # because the year is already in Documents.xlsx.
        # ----------------------------------------------------

        candidates.sort(
            key=document_sort_key,
            reverse=True,
        )

        chosen = candidates[0]

        document_chembl_id = clean_id(
            chosen[
                "document_chembl_id"
            ]
        )

        internal_document_id = clean(
            chosen[
                "document_id"
            ]
        )

        matched += 1

        mapped_document_chembl.append(
            document_chembl_id
        )

        relationship_rows.append({

            "Source":
                "Bioactivity",

            "Source Id":
                bio_id,

            "Type":
                "REPORTED_IN",

            "Destination":
                "Document",

            "Destination Id":
                internal_document_id,

            "Label":
                "reported_in",

            "Description":
                "Associated bioactivity is reported in respective document",

        })

    # --------------------------------------------------------
    # Add recovered document ChEMBL ID to a COPY
    # --------------------------------------------------------

    bio_output = bio.copy()

    bio_output[
        "document_chembl_id"
    ] = mapped_document_chembl

    # --------------------------------------------------------
    # Relationship dataframe
    # --------------------------------------------------------

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
    )

    relationships_df = (
        relationships_df
        .drop_duplicates()
        .reset_index(drop=True)
    )

    return (
        bio_output,
        relationships_df,
        matched,
        unmatched,
        multiple_candidates,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("BIOACTIVITY → DOCUMENT FINAL MAPPER")
    print("=" * 70)

    # --------------------------------------------------------
    # Load existing sheets
    # --------------------------------------------------------

    bio = load_bioactivities()

    documents = load_documents()

    # --------------------------------------------------------
    # Build lookup
    # --------------------------------------------------------

    document_lookup = build_document_lookup(
        documents
    )

    print(
        f"\nExisting document IDs available: "
        f"{len(document_lookup)}"
    )

    # --------------------------------------------------------
    # Recover document IDs from ChEMBL activities
    # --------------------------------------------------------

    activity_documents = (
        build_activity_document_index(
            bio,
            document_lookup,
        )
    )

    print(
        "\nUnique activity keys with "
        "document mappings: "
        f"{len(activity_documents)}"
    )

    # --------------------------------------------------------
    # Map
    # --------------------------------------------------------

    (
        bio_output,
        relationships_df,
        matched,
        unmatched,
        multiple_candidates,
    ) = map_bioactivities(
        bio,
        documents,
        activity_documents,
    )

    # --------------------------------------------------------
    # Save relationship sheet
    # --------------------------------------------------------

    relationships_df.to_excel(
        OUTPUT_RELATIONSHIP,
        index=False,
    )

    # --------------------------------------------------------
    # Save optional Bioactivity copy
    # --------------------------------------------------------

    bio_output.to_excel(
        OUTPUT_BIOACTIVITY,
        index=False,
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("FINAL RESULT")
    print("=" * 70)

    print(
        f"Bioactivities: "
        f"{len(bio)}"
    )

    print(
        f"Matched: "
        f"{matched}"
    )

    print(
        f"Unmatched: "
        f"{unmatched}"
    )

    print(
        f"Multiple document candidates: "
        f"{multiple_candidates}"
    )

    print(
        f"REPORTED_IN relationships: "
        f"{len(relationships_df)}"
    )

    print(
        f"Existing Documents used: "
        f"{len(documents)}"
    )

    print("\nCreated:")

    print(
        f"  {OUTPUT_RELATIONSHIP}"
    )

    print(
        f"  {OUTPUT_BIOACTIVITY}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()