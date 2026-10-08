import argparse
import json
import time
from pathlib import Path

import pandas as pd
import requests


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"

DEFAULT_INPUT = "Bioactivities.xlsx"
DEFAULT_CHECKPOINT = "bioactivity_checkpoint.json"
DEFAULT_DOCUMENT_OUTPUT = "Documents.xlsx"
DEFAULT_RELATIONSHIP_OUTPUT = "Bioactivity_Document_Relationships.xlsx"
DEFAULT_DOCUMENT_CACHE = "document_cache.json"
DOCUMENT_REQUEST_TIMEOUT = 15

session = requests.Session()

session.headers.update({
    "User-Agent": "Bioactivity-Document-Collector/5.0",
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


def load_document_cache(path):
    path = Path(path)

    if not path.exists():
        return {}

    try:

        with path.open(
            "r",
            encoding="utf-8",
        ) as handle:

            data = json.load(handle)

        return (
            data
            if isinstance(data, dict)
            else {}
        )

    except Exception as exc:

        print(
            f"      WARNING: could not load "
            f"document cache: {exc}"
        )

        return {}


def save_document_cache(
    cache,
    path,
):
    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = path.with_suffix(
        path.suffix + ".tmp"
    )

    try:

        with temp.open(
            "w",
            encoding="utf-8",
        ) as handle:

            json.dump(
                cache,
                handle,
                ensure_ascii=False,
                indent=2,
            )

        temp.replace(
            path
        )

    except Exception as exc:

        print(
            f"      WARNING: could not save "
            f"document cache: {exc}"
        )

        try:
            if temp.exists():
                temp.unlink()
        except Exception:
            pass


# ============================================================
# HTTP
# ============================================================

def get_json(
    url,
    cache=None,
    cache_key=None,
    retries=5,
):
    """
    Fetch JSON with persistent caching.

    Successful responses are cached immediately under cache_key.
    Existing cached responses are returned without an HTTP request.

    Retryable failures:
      - HTTP 429
      - HTTP 500/502/503/504
      - request timeouts / connection errors

    Non-retryable HTTP errors are returned as unresolved.
    """

    if cache is None:
        cache = {}

    if cache_key and cache_key in cache:

        cached = cache[
            cache_key
        ]

        if cached != "__NOT_FOUND__":

            return cached

        # A previous unresolved result is deliberately retried on the
        # next run rather than being treated as permanently missing.

    for attempt in range(retries):

        try:

            response = session.get(
                url,
                timeout=DOCUMENT_REQUEST_TIMEOUT,
            )

            if response.status_code == 200:

                data = response.json()

                if cache_key:

                    cache[
                        cache_key
                    ] = data

                return data

            if response.status_code == 404:

                print(
                    f"      404: {url}"
                )

                if cache_key:
                    cache[
                        cache_key
                    ] = "__NOT_FOUND__"

                return None

            if response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }:

                if attempt < retries - 1:

                    wait = min(
                        2 ** attempt,
                        8,
                    )

                    print(
                        f"      HTTP {response.status_code}; "
                        f"retrying in {wait}s"
                    )

                    time.sleep(
                        wait
                    )

                    continue

                print(
                    f"      HTTP {response.status_code}; "
                    f"retries exhausted"
                )

                return None

            print(
                f"      HTTP {response.status_code}: "
                f"{url}"
            )

            return None

        except requests.RequestException as exc:

            print(
                f"      Request error: {exc}"
            )

            if attempt < retries - 1:

                wait = min(
                    2 ** attempt,
                    8,
                )

                time.sleep(
                    wait
                )

                continue

    return None


def get_document(
    document_chembl_id,
    cache,
):
    url = (
        f"{BASE_URL}/document/"
        f"{document_chembl_id}.json"
    )

    return get_json(
        url,
        cache=cache,
        cache_key=(
            "document:"
            + clean_id(
                document_chembl_id
            )
        ),
    )


# ============================================================
# DOCUMENT FIELDS
# ============================================================

def extract_title(document):
    if not document:
        return ""

    for field in [
        "title",
        "document_title",
    ]:
        value = clean(document.get(field))

        if value:
            return value

    return ""


def extract_author(document):
    if not document:
        return ""

    for field in [
        "authors",
        "author",
        "document_author",
    ]:
        value = document.get(field)

        if isinstance(value, list):
            parts = [clean(item) for item in value]
            parts = [item for item in parts if item]
            if parts:
                return "; ".join(parts)
        else:
            value = clean(value)
            if value:
                return value

    return ""


def extract_year(document):
    if not document:
        return None

    for field in [
        "year",
        "publication_year",
    ]:
        value = document.get(field)

        if value is None:
            continue

        try:
            return int(float(value))
        except Exception:
            pass

    return None


def extract_journal(document):
    if not document:
        return ""

    for field in [
        "journal",
        "journal_full_title",
    ]:
        value = clean(document.get(field))

        if value:
            return value

    return ""


def extract_source_id(document):
    if not document:
        return ""

    # ChEMBL normally exposes this as src_id.
    for field in [
        "src_id",
        "source_id",
    ]:
        value = clean(document.get(field))

        if value:
            return value

    source = document.get("source")

    if isinstance(source, dict):
        for field in [
            "src_id",
            "source_id",
            "id",
        ]:
            value = clean(source.get(field))

            if value:
                return value

    return ""


# ============================================================
# BIOACTIVITY CHECKPOINT
# ============================================================

def activity_key(record):
    """
    Must stay aligned with the Bioactivity collector's
    deduplication rule.

    ChEMBL activity ID is the preferred exact measurement key.
    """

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
        clean(
            record.get("standard_type")
        ),
        clean(
            record.get("standard_relation")
        ),
        clean(
            record.get("standard_value")
        ),
        clean(
            record.get("standard_units")
        ),
        clean_id(
            record.get("target_id")
        ),
    )


def load_bioactivity_mapping(
    checkpoint_file,
    bio_file,
):
    """
    Recover the internal ChEMBL metadata that the Bioactivity
    node intentionally does NOT expose.

    Bioactivities.xlsx contains only the final node fields.
    The checkpoint retains the exact ChEMBL activity/document
    information used during collection.
    """

    bio = pd.read_excel(bio_file)

    bio.columns = [
        str(c).strip().lower()
        for c in bio.columns
    ]

    required_bio = {
        "bioactivity_id",
        "standard_type",
        "standard_relation",
        "standard_value",
        "standard_units",
        "normalized_value_nm",
        "high_confidence_tier",
    }

    missing = (
        required_bio
        - set(bio.columns)
    )

    if missing:
        raise ValueError(
            f"{bio_file} is missing required Bioactivity "
            f"node fields:\n"
            + "\n".join(sorted(missing))
        )

    checkpoint_path = Path(checkpoint_file)

    if not checkpoint_path.exists():
        raise FileNotFoundError(
            "Bioactivity checkpoint is required for exact "
            "Bioactivity → ChEMBL document mapping:\n"
            f"{checkpoint_path}"
        )

    with checkpoint_path.open(
        "r",
        encoding="utf-8",
    ) as f:
        checkpoint = json.load(f)

    records = checkpoint.get("records", [])

    if not isinstance(records, list):
        raise ValueError(
            "Bioactivity checkpoint 'records' is not a list."
        )

    # Reproduce the Bioactivity collector's exact deduplication.
    unique = {}

    for record in records:
        key = activity_key(record)

        if key not in unique:
            unique[key] = record

    records = list(unique.values())

    # Reproduce the Bioactivity collector's BA_XXXXXX assignment.
    internal_map = {}

    for number, record in enumerate(
        records,
        start=1,
    ):
        bioactivity_id = f"BA_{number:06d}"

        internal_map[bioactivity_id] = {
            "phytochemical_chembl_id":
                clean_id(
                    record.get(
                        "phytochemical_chembl_id"
                    )
                ),

            "chembl_activity_id":
                clean_id(
                    record.get(
                        "chembl_activity_id"
                    )
                ),

            "target_id":
                clean_id(
                    record.get("target_id")
                ),

            "document_chembl_id":
                clean_id(
                    record.get(
                        "document_chembl_id"
                    )
                ),
        }

    # Verify that the checkpoint-derived IDs line up with the
    # actual final Bioactivity node file.
    actual_ids = [
        clean_id(x)
        for x in bio["bioactivity_id"]
        if clean_id(x)
    ]

    if actual_ids != list(internal_map.keys()):
        raise ValueError(
            "Bioactivity checkpoint does not align with "
            "Bioactivities.xlsx.\n"
            "Do not use an old checkpoint with a newly generated "
            "Bioactivities.xlsx."
        )

    return internal_map


# ============================================================
# BUILD DOCUMENT NODE + RELATIONSHIP TABLES
# ============================================================

def build(
    input_file,
    checkpoint_file,
    document_output,
    relationship_output,
    cache_file=None,
):
    print("=" * 72)
    print(
        "CHEMBL BIOACTIVITY → DOCUMENT COLLECTOR"
    )
    print("=" * 72)

    print(f"\nLoading Bioactivities: {input_file}")
    print(f"Loading checkpoint:    {checkpoint_file}")

    bio = pd.read_excel(input_file)

    bio.columns = [
        str(c).strip().lower()
        for c in bio.columns
    ]

    required_bio = {
        "bioactivity_id",
        "standard_type",
        "standard_relation",
        "standard_value",
        "standard_units",
        "normalized_value_nm",
        "high_confidence_tier",
    }

    missing = (
        required_bio
        - set(bio.columns)
    )

    if missing:
        raise ValueError(
            "Bioactivities.xlsx is missing:\n"
            + "\n".join(sorted(missing))
        )

    bio_map = load_bioactivity_mapping(
        checkpoint_file,
        input_file,
    )

    print(
        f"Bioactivity rows: {len(bio)}"
    )

    # --------------------------------------------------------
    # Recover exact ChEMBL document IDs.
    # --------------------------------------------------------

    selected = {}

    unmatched = 0

    for _, row in bio.iterrows():
        bio_id = clean_id(
            row["bioactivity_id"]
        )

        metadata = bio_map.get(bio_id)

        if not metadata:
            unmatched += 1
            continue

        document_chembl_id = clean_id(
            metadata.get(
                "document_chembl_id"
            )
        )

        if not document_chembl_id:
            unmatched += 1
            continue

        selected[bio_id] = document_chembl_id

    print(
        f"Bioactivities with document mapping: "
        f"{len(selected)}"
    )

    print(
        f"Bioactivities without document mapping: "
        f"{unmatched}"
    )

    # --------------------------------------------------------
    # Unique Documents
    #
    # document_chembl_id IS the canonical Document node ID.
    # No synthetic D_000001 ID is created.
    # --------------------------------------------------------

    unique_document_ids = sorted(
        set(selected.values())
    )

    print(
        f"Unique documents: "
        f"{len(unique_document_ids)}"
    )

    if cache_file is None:

        cache_file = (
            Path(document_output).parent
            / DEFAULT_DOCUMENT_CACHE
        )

    document_cache = load_document_cache(
        cache_file
    )

    document_rows = []

    for number, document_chembl_id in enumerate(
        unique_document_ids,
        start=1,
    ):
        print(
            f"    [{number}/{len(unique_document_ids)}] "
            f"{document_chembl_id}"
        )

        document = get_document(
            document_chembl_id,
            document_cache,
        )

        if not document:
            print(
                f"      Could not retrieve "
                f"{document_chembl_id}"
            )
            continue

        # Persist successful documents immediately so a later
        # failure cannot discard the completed work.
        save_document_cache(
            document_cache,
            cache_file,
        )

        source_id = extract_source_id(
            document
        )

        title = extract_title(
            document
        )

        author = extract_author(
            document
        )

        journal = extract_journal(
            document
        )

        year = extract_year(
            document
        )

        document_rows.append({
            "document_chembl_id":
                document_chembl_id,

            "source_id":
                source_id,

            "document_title":
                title,

            "document_author":
                author,

            "document_journal":
                journal,

            "document_year":
                year if year is not None else "",
        })

        time.sleep(0.05)

    documents_df = pd.DataFrame(
        document_rows,
        columns=[
            "document_chembl_id",
            "source_id",
            "document_title",
            "document_author",
            "document_journal",
            "document_year",
        ],
    )

    # --------------------------------------------------------
    # Only relationships whose Document node was actually
    # created are emitted.
    # --------------------------------------------------------

    available_documents = set(
        documents_df[
            "document_chembl_id"
        ].map(clean_id)
    )

    relationship_rows = []

    for bio_id, document_chembl_id in selected.items():
        document_chembl_id = clean_id(
            document_chembl_id
        )

        if document_chembl_id not in available_documents:
            continue

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
                document_chembl_id,

            "Label":
                "reported_in",

            "Description":
                "Bioactivity is reported in respective document",
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
    ).reset_index(drop=True)

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    documents_df.to_excel(
        document_output,
        index=False,
    )

    relationships_df.to_excel(
        relationship_output,
        index=False,
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print("\n" + "=" * 72)
    print("FINAL RESULT")
    print("=" * 72)

    print(
        f"Bioactivities: "
        f"{len(bio)}"
    )

    print(
        f"Mapped Bioactivities: "
        f"{len(selected)}"
    )

    print(
        f"Documents generated: "
        f"{len(documents_df)}"
    )

    print(
        f"REPORTED_IN relationships: "
        f"{len(relationships_df)}"
    )

    print("\nDocument columns:")
    for column in documents_df.columns:
        print(f"  {column}")

    print("\nCreated:")
    print(f"  {document_output}")
    print(f"  {relationship_output}")

    print("\nDone.")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Build Document nodes and "
            "REPORTED_IN relationships from "
            "the Bioactivity checkpoint."
        )
    )

    parser.add_argument(
        "--file",
        default=DEFAULT_INPUT,
        help="Bioactivities.xlsx",
    )

    parser.add_argument(
        "--checkpoint",
        default=DEFAULT_CHECKPOINT,
        help="Bioactivity checkpoint JSON",
    )

    parser.add_argument(
        "--documents",
        default=DEFAULT_DOCUMENT_OUTPUT,
        help="Output Documents.xlsx",
    )

    parser.add_argument(
        "--relationships",
        default=DEFAULT_RELATIONSHIP_OUTPUT,
        help="Output relationship Excel file",
    )

    parser.add_argument(
        "--cache",
        default=DEFAULT_DOCUMENT_CACHE,
        help="Persistent document JSON cache",
    )

    args = parser.parse_args()

    build(
        input_file=args.file,
        checkpoint_file=args.checkpoint,
        document_output=args.documents,
        relationship_output=args.relationships,
        cache_file=args.cache,
    )


if __name__ == "__main__":
    main()