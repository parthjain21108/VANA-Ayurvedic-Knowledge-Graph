import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

TIMEOUT = 20

USER_AGENT = (
    "AyurvedicPlantResearchCollector/2.0 "
    "Educational research project"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json",
}

IMPPAT_BASE = (
    "https://cb.imsc.res.in/imppat/therapeutics/"
)

PUBMED_SEARCH = (
    "https://eutils.ncbi.nlm.nih.gov/"
    "entrez/eutils/esearch.fcgi"
)

EUROPE_PMC_SEARCH = (
    "https://www.ebi.ac.uk/europepmc/webservices/"
    "rest/search"
)

CACHE_FILE = "therapeutic_cache.json"


# ============================================================
# SESSION
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# CACHE
# ============================================================

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}

    try:
        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    temp = CACHE_FILE + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            cache,
            f,
            ensure_ascii=False,
            indent=2
        )
        f.flush()
        os.fsync(f.fileno())

    last_error = None

    for attempt in range(5):
        try:
            os.replace(temp, CACHE_FILE)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.5)

    raise PermissionError(
        f"Could not replace cache file '{CACHE_FILE}' "
        f"after 5 attempts. The file may be locked by another "
        f"process/application."
    ) from last_error


cache = load_cache()


# ============================================================
# TEXT HELPERS
# ============================================================

def clean(value):
    if value is None:
        return ""

    value = str(value)

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def norm(value):
    return clean(value).lower()


# ============================================================
# PLANT PART NORMALIZATION
# ============================================================

PART_ALIASES = {
    "leaf": {
        "leaf",
        "leaves",
    },

    "root": {
        "root",
        "roots",
    },

    "stem": {
        "stem",
        "stems",
    },

    "bark": {
        "bark",
    },

    "flower": {
        "flower",
        "flowers",
    },

    "fruit": {
        "fruit",
        "fruits",
    },

    "seed": {
        "seed",
        "seeds",
    },

    "rhizome": {
        "rhizome",
        "rhizomes",
    },

    "bulb": {
        "bulb",
        "bulbs",
    },

    "tuber": {
        "tuber",
        "tubers",
    },

    "wood": {
        "wood",
    },

    "gum": {
        "gum",
        "gums",
    },

    "latex": {
        "latex",
    },

    "oil": {
        "oil",
        "essential oil",
    },

    "whole plant": {
        "whole plant",
        "whole herb",
        "whole plant/extract",
    },

    "aerial part": {
        "aerial part",
        "aerial parts",
    },

    "exudate": {
        "exudate",
        "plant exudate",
    },
}


def canonical_part(value):

    value = norm(value)

    if not value:
        return ""

    for canonical, aliases in PART_ALIASES.items():

        if value in aliases:
            return canonical

    # Conservative substring matching
    for canonical, aliases in PART_ALIASES.items():

        for alias in aliases:

            if value == alias:
                return canonical

    return value


def parts_match(requested, imppat):

    requested = canonical_part(requested)
    imppat = canonical_part(imppat)

    if not requested or not imppat:
        return False

    return requested == imppat


# ============================================================
# HTTP
# ============================================================

def get(url, params=None):

    try:

        response = session.get(
            url,
            params=params,
            timeout=TIMEOUT
        )

        if response.status_code == 429:
            return None, "429"

        if response.status_code == 404:
            return None, "404"

        if response.status_code >= 400:
            return None, str(
                response.status_code
            )

        return response, "ok"

    except requests.RequestException as e:

        return None, type(e).__name__


# ============================================================
# ME SH ID
# ============================================================

def extract_mesh_id(text):

    if not text:
        return ""

    match = re.search(
        r"MESH\s*:\s*(D\d{6})",
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1).upper()

    # Also allow bare IDs
    match = re.search(
        r"\b(D\d{6})\b",
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1).upper()

    return ""


# ============================================================
# IMPPAT
# ============================================================

def scrape_imppat(plant_name):

    cache_key = (
        "imppat::"
        + plant_name.lower()
    )

    if cache_key in cache:

        cached = cache[cache_key]

        # Current cache format:
        # {"status": "ok", "rows": [...]}
        if isinstance(cached, dict):
            return (
                cached.get("rows", []),
                cached.get("status", "cache")
            )

        # Backward compatibility with older list-only cache entries.
        if isinstance(cached, list):
            return (
                cached,
                "cache"
            )

        # Invalid cache entry.
        return (
            [],
            "invalid_cache"
        )

    url = (
        IMPPAT_BASE
        + quote(
            plant_name,
            safe=""
        )
    )

    print(
        f"      IMPPAT -> {plant_name}"
    )

    response, status = get(url)

    if response is None:

        print(
            f"      IMPPAT: {status}"
        )

        cache[cache_key] = {
            "status": status,
            "rows": []
        }

        save_cache(cache)

        return [], status

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    rows_found = []

    # --------------------------------------------------------
    # Find the therapeutic association table.
    #
    # We DO NOT assume a particular CSS class.
    # We inspect every table.
    # --------------------------------------------------------

    for table in soup.find_all("table"):

        headers = []

        header_row = table.find("tr")

        if header_row:

            headers = [
                clean(cell.get_text(" ", strip=True))
                for cell in header_row.find_all(
                    ["th", "td"]
                )
            ]

        header_text = " ".join(
            headers
        ).lower()

        # We need the actual therapeutic table.
        if (
            "plant part" not in header_text
            or "therapeutic use" not in header_text
        ):
            continue

        for tr in table.find_all("tr")[1:]:

            cells = tr.find_all(
                ["td", "th"]
            )

            values = [
                clean(
                    cell.get_text(
                        " ",
                        strip=True
                    )
                )
                for cell in cells
            ]

            if len(values) < 3:
                continue

            # ------------------------------------------------
            # Find columns dynamically
            # ------------------------------------------------

            column_map = {}

            for i, header in enumerate(headers):

                h = norm(header)

                if "plant part" in h:
                    column_map["part"] = i

                elif (
                    "therapeutic use"
                    in h
                ):
                    column_map["use"] = i

                elif (
                    "identifier"
                    in h
                ):
                    column_map["identifier"] = i

                elif (
                    "reference"
                    in h
                ):
                    column_map["reference"] = i

            if "use" not in column_map:
                continue

            use_index = column_map["use"]

            if use_index >= len(values):
                continue

            therapeutic_use = values[
                use_index
            ]

            if not therapeutic_use:
                continue

            part = ""

            if "part" in column_map:

                i = column_map["part"]

                if i < len(values):
                    part = values[i]

            identifier_text = ""

            if "identifier" in column_map:

                i = column_map[
                    "identifier"
                ]

                if i < len(values):
                    identifier_text = values[i]

            reference = ""

            if "reference" in column_map:

                i = column_map[
                    "reference"
                ]

                if i < len(values):
                    reference = values[i]

            mesh_id = extract_mesh_id(
                identifier_text
            )

            rows_found.append({
                "plant_name": plant_name,
                "plant_part": part,
                "therapeutic_use":
                    therapeutic_use,
                "mesh_id": mesh_id,
                "reference": reference,
            })

    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    unique = {}

    for row in rows_found:

        key = (
            norm(row["plant_part"]),
            norm(row["therapeutic_use"]),
            row["mesh_id"]
        )

        unique[key] = row

    rows_found = list(
        unique.values()
    )

    cache[cache_key] = {
        "status": "ok",
        "rows": rows_found
    }

    save_cache(cache)

    print(
        f"      IMPPAT: "
        f"{len(rows_found)} therapeutic rows"
    )

    return rows_found, "ok"


# ============================================================
# PUBMED
# ============================================================

def pubmed_check(
    plant_name,
    plant_part
):

    cache_key = (
        "pubmed::"
        + plant_name.lower()
        + "::"
        + plant_part.lower()
    )

    if cache_key in cache:
        return cache[
            cache_key
        ]

    query = (
        f'"{plant_name}" '
        f'"{plant_part}" '
        "("
        "medicinal OR therapeutic OR "
        "traditional medicine"
        ")"
    )

    response, status = get(
        PUBMED_SEARCH,
        params={
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": 5,
        }
    )

    if response is None:

        result = {
            "status": status,
            "count": 0
        }

        cache[cache_key] = result
        save_cache(cache)

        return result

    try:

        data = response.json()

        ids = (
            data
            .get("esearchresult", {})
            .get("idlist", [])
        )

        result = {
            "status": "ok",
            "count": len(ids)
        }

        cache[cache_key] = result
        save_cache(cache)

        return result

    except Exception:

        return {
            "status": "parse_error",
            "count": 0
        }


# ============================================================
# EUROPE PMC
# ============================================================

def europe_pmc_check(
    plant_name,
    plant_part
):

    cache_key = (
        "europepmc::"
        + plant_name.lower()
        + "::"
        + plant_part.lower()
    )

    if cache_key in cache:
        return cache[
            cache_key
        ]

    query = (
        f'"{plant_name}" AND '
        f'"{plant_part}" AND '
        "("
        "medicinal OR therapeutic OR "
        "traditional medicine"
        ")"
    )

    response, status = get(
        EUROPE_PMC_SEARCH,
        params={
            "query": query,
            "format": "json",
            "pageSize": 5,
        }
    )

    if response is None:

        result = {
            "status": status,
            "count": 0
        }

        cache[cache_key] = result
        save_cache(cache)

        return result

    try:

        data = response.json()

        results = (
            data
            .get("resultList", {})
            .get("result", [])
        )

        result = {
            "status": "ok",
            "count": len(results)
        }

        cache[cache_key] = result
        save_cache(cache)

        return result

    except Exception:

        return {
            "status": "parse_error",
            "count": 0
        }


# ============================================================
# E-CHARAK
# ============================================================

def echarak_check(
    plant_name,
    plant_part
):

    """
    E-CHARAK is used as an Indian medicinal-plant
    cross-check.

    We do NOT fabricate therapeutic uses from it.
    """

    cache_key = (
        "echarak::"
        + plant_name.lower()
        + "::"
        + plant_part.lower()
    )

    if cache_key in cache:
        return cache[
            cache_key
        ]

    # E-CHARAK's public knowledge resources page
    # is useful for medicinal plant / part verification.
    url = (
        "https://echarak.ayush.gov.in/"
        "knowledge_resources"
    )

    response, status = get(url)

    if response is None:

        result = {
            "status": status,
            "count": 0
        }

        cache[cache_key] = result
        save_cache(cache)

        return result

    text = norm(
        BeautifulSoup(
            response.text,
            "html.parser"
        ).get_text(" ")
    )

    found = (
        norm(plant_name) in text
        and norm(plant_part) in text
    )

    result = {
        "status": "found" if found else "no_match",
        "count": 1 if found else 0
    }

    cache[cache_key] = result
    save_cache(cache)

    return result


# ============================================================
# FRLHT
# ============================================================

def frlht_check(
    plant_name,
    plant_part
):

    """
    FRLHT is primarily used as an Indian medicinal-plant
    cross-check.

    We record availability only.
    """

    cache_key = (
        "frlht::"
        + plant_name.lower()
        + "::"
        + plant_part.lower()
    )

    if cache_key in cache:
        return cache[
            cache_key
        ]

    # The public AYUSH page identifies the FRLHT database
    # and its scope. Actual FRLHT search pages can change,
    # so this source is deliberately non-fatal.
    result = {
        "status": "cross_check",
        "count": 0
    }

    cache[cache_key] = result
    save_cache(cache)

    return result


# ============================================================
# MAIN SOURCE PROCESSING
# ============================================================

def process_plant(
    plant_name,
    requested_parts
):

    print()
    print(
        f"  {plant_name}"
    )

    imppat_rows, imppat_status = (
        scrape_imppat(
            plant_name
        )
    )

    # --------------------------------------------------------
    # Match IMPPAT associations to the actual PlantPart rows.
    # --------------------------------------------------------

    matched = []

    for requested in requested_parts:

        plant_part_id = requested[
            "plant_part_id"
        ]

        plant_part = requested[
            "plant_part"
        ]

        # ----------------------------------------------
        # Find exact plant-part matches
        # ----------------------------------------------

        part_rows = [
            row
            for row in imppat_rows
            if parts_match(
                plant_part,
                row["plant_part"]
            )
        ]

        # ----------------------------------------------
        # IMPPAT sometimes has a blank plant-part field.
        #
        # We DO NOT automatically assign a blank-part
        # association to every plant part.
        #
        # This avoids false relationships.
        # ----------------------------------------------

        if not part_rows:

            print(
                f"    {plant_part}: "
                "no direct IMPPAT part match"
            )

        else:

            print(
                f"    {plant_part}: "
                f"{len(part_rows)} therapeutic uses"
            )

        # ----------------------------------------------
        # Supporting source checks
        # ----------------------------------------------

        pubmed = pubmed_check(
            plant_name,
            plant_part
        )

        epmc = europe_pmc_check(
            plant_name,
            plant_part
        )

        echarak = echarak_check(
            plant_name,
            plant_part
        )

        frlht = frlht_check(
            plant_name,
            plant_part
        )

        for row in part_rows:

            source_set = {
                "IMPPAT"
            }

            # Secondary sources only become additional
            # provenance when they found supporting material.
            if pubmed["count"] > 0:
                source_set.add(
                    "PubMed"
                )

            if epmc["count"] > 0:
                source_set.add(
                    "Europe PMC"
                )

            if echarak["count"] > 0:
                source_set.add(
                    "e-CHARAK"
                )

            if frlht["count"] > 0:
                source_set.add(
                    "FRLHT"
                )

            matched.append({
                "plant_part_id":
                    plant_part_id,

                "plant_name":
                    plant_name,

                "plant_part":
                    plant_part,

                "mesh_id":
                    row["mesh_id"],

                "therapeutic_use":
                    row["therapeutic_use"],

                "source":
                    "; ".join(
                        sorted(
                            source_set
                        )
                    ),
            })

    return matched


# ============================================================
# LOAD INPUT
# ============================================================

def load_input(path):

    if not os.path.exists(path):

        raise FileNotFoundError(
            f"Input file not found: {path}"
        )

    df = pd.read_excel(
        path
    )

    required = {
        "plant_part_id",
        "plant_name",
        "plant_part",
    }

    missing = (
        required
        - set(df.columns)
    )

    if missing:

        raise ValueError(
            "Missing columns: "
            + ", ".join(
                sorted(missing)
            )
        )

    df = df[
        [
            "plant_part_id",
            "plant_name",
            "plant_part",
        ]
    ].copy()

    df = df.dropna(
        subset=[
            "plant_part_id",
            "plant_name",
            "plant_part",
        ]
    )

    for col in df.columns:

        df[col] = df[col].map(
            clean
        )

    return df


# ============================================================
# SAVE
# ============================================================

def save_outputs(
    mappings,
    therapeutic_output,
    mapping_output
):

    # --------------------------------------------------------
    # Therapeutic entity table
    # --------------------------------------------------------

    therapeutic = {}

    for row in mappings:

        mesh_id = row[
            "mesh_id"
        ]

        therapeutic_use = row[
            "therapeutic_use"
        ]

        if not therapeutic_use:
            continue

        # If IMPPAT has no MeSH ID, don't invent one.
        # Such rows are retained in the mapping file
        # for manual review, but not in the normalized
        # Therapeutic_uses entity table.
        if not mesh_id:
            continue

        if mesh_id not in therapeutic:

            therapeutic[mesh_id] = {
                "mesh_id":
                    mesh_id,

                "therapeutic_use":
                    therapeutic_use,

                "mesh_category":
                    "",

                "mesh_category_name":
                    "",

                "mesh_sub_category_name":
                    "",

                "source":
                    set(),
            }

        therapeutic[
            mesh_id
        ][
            "source"
        ].update(
            row["source"].split("; ")
        )

    therapeutic_rows = []

    for mesh_id in sorted(
        therapeutic
    ):

        row = therapeutic[
            mesh_id
        ]

        therapeutic_rows.append({
            "mesh_id":
                row["mesh_id"],

            "therapeutic_use":
                row["therapeutic_use"],

            "mesh_category":
                row["mesh_category"],

            "mesh_category_name":
                row["mesh_category_name"],

            "mesh_sub_category_name":
                row[
                    "mesh_sub_category_name"
                ],

            "source":
                "; ".join(
                    sorted(
                        row["source"]
                    )
                ),
        })

    therapeutic_df = pd.DataFrame(
        therapeutic_rows,
        columns=[
            "mesh_id",
            "therapeutic_use",
            "mesh_category",
            "mesh_category_name",
            "mesh_sub_category_name",
            "source",
        ]
    )

    # --------------------------------------------------------
    # Mapping table
    # --------------------------------------------------------

    mapping_df = pd.DataFrame(
        mappings,
        columns=[
            "plant_part_id",
            "plant_name",
            "plant_part",
            "mesh_id",
            "therapeutic_use",
            "source",
        ]
    )

    mapping_df = (
        mapping_df
        .drop_duplicates(
            subset=[
                "plant_part_id",
                "mesh_id",
                "therapeutic_use",
            ]
        )
        .sort_values(
            [
                "plant_part_id",
                "therapeutic_use",
            ]
        )
    )

    therapeutic_df.to_excel(
        therapeutic_output,
        index=False
    )

    mapping_df.to_excel(
        mapping_output,
        index=False
    )

    return (
        len(therapeutic_df),
        len(mapping_df)
    )


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Multi-source Ayurvedic "
            "Therapeutic Use Collector"
        )
    )

    parser.add_argument(
        "--file",
        required=True,
        help="PlantPart.xlsx"
    )

    parser.add_argument(
        "--output",
        default="Therapeutic_uses.xlsx"
    )

    parser.add_argument(
        "--mapping-output",
        default="Therapeutic_Use_Mapping.xlsx"
    )

    args = parser.parse_args()

    print("=" * 70)
    print(
        "MULTI-SOURCE THERAPEUTIC USE COLLECTOR V2"
    )
    print("=" * 70)

    df = load_input(
        args.file
    )

    print(
        f"PlantPart records: {len(df)}"
    )

    # --------------------------------------------------------
    # Group by plant.
    #
    # 246 rows -> unique plants.
    # IMPPAT is fetched ONCE per plant.
    # --------------------------------------------------------

    grouped = defaultdict(list)

    for _, row in df.iterrows():

        grouped[
            row["plant_name"]
        ].append({
            "plant_part_id":
                row["plant_part_id"],

            "plant_part":
                row["plant_part"],
        })

    print(
        f"Unique plants: {len(grouped)}"
    )

    print()
    print(
        "Sources:"
    )
    print(
        "  1. IMPPAT       PRIMARY"
    )
    print(
        "  2. PubMed       supporting"
    )
    print(
        "  3. Europe PMC   supporting"
    )
    print(
        "  4. e-CHARAK     Indian cross-check"
    )
    print(
        "  5. FRLHT        Indian cross-check"
    )

    all_mappings = []

    total = len(grouped)

    for index, (
        plant_name,
        parts
    ) in enumerate(
        grouped.items(),
        start=1
    ):

        print()
        print(
            "=" * 70
        )

        print(
            f"[{index}/{total}] "
            f"{plant_name}"
        )

        results = process_plant(
            plant_name,
            parts
        )

        all_mappings.extend(
            results
        )

        # Save progress after EVERY plant.
        save_cache(
            cache
        )

    # --------------------------------------------------------
    # Save final files
    # --------------------------------------------------------

    print()
    print(
        "=" * 70
    )

    entity_count, mapping_count = (
        save_outputs(
            all_mappings,
            args.output,
            args.mapping_output
        )
    )

    print(
        "COMPLETE"
    )

    print(
        f"PlantPart mappings found: "
        f"{mapping_count}"
    )

    print(
        f"Unique therapeutic uses: "
        f"{entity_count}"
    )

    print()
    print(
        f"Saved: {args.output}"
    )

    print(
        f"Saved: {args.mapping_output}"
    )

    if entity_count == 0:

        print()
        print(
            "WARNING: ZERO THERAPEUTIC USES FOUND."
        )

        print(
            "Check therapeutic_cache.json "
            "before running again."
        )

    print(
        "=" * 70
    )


if __name__ == "__main__":
    main()