import argparse
import json
import os
import random
import re
import time
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://cb.imsc.res.in/imppat"
CACHE_FILE = "imppat_cache_v2.json"

OUTPUT_COLUMNS = [
    "plant_part_id",
    "plant_name",
    "plant_part",
    "imppat_id",
    "phytochemical_name",
    "source_url",
    "source",
    "references",
    "association_level",
    "confidence",
    "evidence",
]

ALIASES = {
    "leaves": "leaf",
    "roots": "root",
    "stems": "stem",
    "barks": "bark",
    "fruits": "fruit",
    "seeds": "seed",
    "flowers": "flower",
    "rhizomes": "rhizome",
    "wholeplant": "whole plant",
    "whole plant": "whole plant",
    "aerial parts": "aerial part",
    "aerial part": "aerial part",
    "wood": "wood",
}


def clean(value):
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    value = str(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_part(value):
    value = clean(value).lower()
    return ALIASES.get(value, value)


def normalize_plant(value):
    value = clean(value).lower()
    value = re.sub(r"\s+", " ", value)
    return value


def split_references(value):
    value = clean(value)

    if not value:
        return set()

    # IMPPAT references are usually comma-separated,
    # but preserve DOI/ISBN tokens.
    parts = re.split(r"\s*,\s*", value)

    return {
        p.strip().lower()
        for p in parts
        if p.strip()
    }


def load_cache(path):
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache, path):
    tmp = path + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            cache,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(tmp, path)


def create_session():
    s = requests.Session()

    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })

    return s


def fetch(session, url, retries=4):
    for attempt in range(retries):
        try:
            r = session.get(
                url,
                timeout=35,
            )

            if r.status_code == 200:
                return r.text

            if r.status_code in (
                429,
                500,
                502,
                503,
                504,
            ):
                wait = (
                    min(20, 2.5 * (attempt + 1))
                    + random.uniform(0.3, 1.2)
                )

                print(
                    f"      HTTP {r.status_code}; "
                    f"retry in {wait:.1f}s"
                )

                time.sleep(wait)
                continue

            print(f"      HTTP {r.status_code}")
            return None

        except requests.RequestException as exc:
            if attempt == retries - 1:
                print(
                    f"      request failed: {exc}"
                )
                return None

            wait = (
                min(15, 2.5 * (attempt + 1))
                + random.uniform(0.3, 1.2)
            )

            print(
                f"      request error; "
                f"retry in {wait:.1f}s"
            )

            time.sleep(wait)

    return None


def parse_phytochemical_table(html):
    """
    Parse the plant -> phytochemical association table
    used by IMPPAT.

    Keeps BOTH:

      1. explicit plant-part rows
      2. plant-level rows where Plant Part is blank.
    """

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    records = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        headers = [
            clean(
                c.get_text(
                    " ",
                    strip=True,
                )
            ).lower()
            for c in rows[0].find_all(
                ["th", "td"]
            )
        ]

        header_text = " ".join(headers)

        if (
            "plant part" not in header_text
            or "phytochemical" not in header_text
        ):
            continue

        plant_idx = None
        part_idx = None
        id_idx = None
        name_idx = None
        ref_idx = None

        for i, h in enumerate(headers):
            if "indian medicinal plant" in h:
                plant_idx = i

            elif h == "plant part":
                part_idx = i

            elif "imppat phytochemical" in h:
                id_idx = i

            elif "phytochemical name" in h:
                name_idx = i

            elif h == "references":
                ref_idx = i

        if None in (
            plant_idx,
            part_idx,
            id_idx,
            name_idx,
        ):
            continue

        for row in rows[1:]:
            cells = row.find_all(
                ["td", "th"]
            )

            values = [
                clean(
                    c.get_text(
                        " ",
                        strip=True,
                    )
                )
                for c in cells
            ]

            if len(values) <= max(
                plant_idx,
                part_idx,
                id_idx,
                name_idx,
            ):
                continue

            imppat_id = clean(
                values[id_idx]
            )

            if not imppat_id.startswith(
                "IMPHY"
            ):
                continue

            records.append({
                "plant_name": clean(
                    values[plant_idx]
                ),
                "plant_part": clean(
                    values[part_idx]
                ),
                "imppat_id": imppat_id,
                "phytochemical_name": clean(
                    values[name_idx]
                ),
                "references": (
                    clean(values[ref_idx])
                    if (
                        ref_idx is not None
                        and ref_idx < len(values)
                    )
                    else ""
                ),
            })

    return records


def get_plant_records(
    session,
    plant_name,
    cache,
):
    key = normalize_plant(
        plant_name
    )

    if key in cache:
        return cache[key]

    encoded = quote(
        plant_name,
        safe="",
    )

    urls = [
        f"{BASE_URL}/phytochemical/{encoded}",
        f"{BASE_URL}/basicsearch/{encoded}",
    ]

    html = None

    for url in urls:
        print(f"      GET {url}")

        html = fetch(
            session,
            url,
        )

        if html:
            break

    records = (
        parse_phytochemical_table(html)
        if html
        else []
    )

    cache[key] = records

    return records


def get_explicit(
    records,
    requested_part,
):
    requested = normalize_part(
        requested_part
    )

    return [
        r
        for r in records
        if normalize_part(
            r["plant_part"]
        ) == requested
    ]


def get_blank(records):
    return [
        r
        for r in records
        if not normalize_part(
            r["plant_part"]
        )
    ]


def infer_from_shared_reference(
    explicit_records,
    blank_record,
):
    """
    Conservative inference:

    A blank plant-level association is promoted
    to a requested part ONLY when its IMPPAT
    reference set overlaps a part-specific
    association for the same plant and compound.

    This does not invent a part from the
    compound name.
    """

    blank_refs = split_references(
        blank_record["references"]
    )

    if not blank_refs:
        return []

    candidates = []

    for r in explicit_records:
        if (
            r["imppat_id"]
            != blank_record["imppat_id"]
        ):
            continue

        refs = split_references(
            r["references"]
        )

        overlap = blank_refs & refs

        if overlap:
            candidates.append(
                (r, overlap)
            )

    return candidates


def build(
    input_file,
    output_file,
    include_inferred=True,
):
    print("=" * 78)
    print(
        "IMPPAT PLANT-PART PHYTOCHEMICAL "
        "COLLECTOR — WIDE / CONSERVATIVE"
    )
    print("=" * 78)

    df = pd.read_excel(
        input_file
    )

    required = [
        "plant_part_id",
        "plant_name",
        "plant_part",
        "source_name",
    ]

    missing = [
        c
        for c in required
        if c not in df.columns
    ]

    if missing:
        raise ValueError(
            "Missing columns: "
            + ", ".join(missing)
        )

    df = df.copy()

    for c in required:
        df[c] = df[c].map(clean)

    print(
        f"Plant-part rows: {len(df)}"
    )

    plants = (
        df["plant_name"]
        .drop_duplicates()
        .tolist()
    )

    print(
        f"Unique plants: {len(plants)}"
    )

    session = create_session()

    cache = load_cache(
        CACHE_FILE
    )

    rows = []
    unmatched = []
    inferred_rows = []
    plant_level_candidates = []

    for n, plant in enumerate(
        plants,
        1,
    ):
        print(
            f"\n[{n}/{len(plants)}] {plant}"
        )

        records = get_plant_records(
            session,
            plant,
            cache,
        )

        save_cache(
            cache,
            CACHE_FILE,
        )

        if not records:
            print(
                "    IMPPAT records: 0"
            )

            plant_rows = df[
                df["plant_name"]
                .map(normalize_plant)
                == normalize_plant(plant)
            ]

            for _, pr in plant_rows.iterrows():
                unmatched.append({
                    "plant_part_id":
                        pr["plant_part_id"],
                    "plant_name":
                        plant,
                    "plant_part":
                        pr["plant_part"],
                    "reason":
                        "No IMPPAT plant "
                        "phytochemical "
                        "page/records",
                })

            continue

        plant_records = [
            r
            for r in records
            if normalize_plant(
                r["plant_name"]
            )
            == normalize_plant(plant)
        ]

        explicit = [
            r
            for r in plant_records
            if normalize_part(
                r["plant_part"]
            )
        ]

        blanks = [
            r
            for r in plant_records
            if not normalize_part(
                r["plant_part"]
            )
        ]

        print(
            f"    IMPPAT records: "
            f"{len(plant_records)}"
        )

        print(
            f"    Explicit part records: "
            f"{len(explicit)}"
        )

        print(
            f"    Plant-level records: "
            f"{len(blanks)}"
        )

        plant_rows = df[
            df["plant_name"]
            .map(normalize_plant)
            == normalize_plant(plant)
        ]

        for _, pr in plant_rows.iterrows():
            requested_part = pr[
                "plant_part"
            ]

            exact = get_explicit(
                plant_records,
                requested_part,
            )

            print(
                f"      {requested_part}: "
                f"exact={len(exact)}"
            )

            exact_keys = set()

            for r in exact:
                key = (
                    pr["plant_part_id"],
                    r["imppat_id"],
                    normalize_part(
                        requested_part
                    ),
                )

                exact_keys.add(key)

                rows.append({
                    "plant_part_id":
                        pr["plant_part_id"],

                    "plant_name":
                        plant,

                    "plant_part":
                        requested_part,

                    "imppat_id":
                        r["imppat_id"],

                    "phytochemical_name":
                        r["phytochemical_name"],

                    "source_url":
                        (
                            f"{BASE_URL}/phytochemical/"
                            f"{quote(plant, safe='')}"
                        ),

                    "source":
                        "IMPPAT",

                    "references":
                        r["references"],

                    "association_level":
                        "explicit_part",

                    "confidence":
                        "HIGH",

                    "evidence":
                        (
                            "IMPPAT explicitly lists "
                            "plant part "
                            f"'{requested_part}'"
                        ),
                })

            # Examine plant-level records
            # rather than discarding them.
            for b in blanks:
                candidates = (
                    infer_from_shared_reference(
                        plant_records,
                        b,
                    )
                )

                # Only promote if the shared
                # reference points to THIS
                # requested part.
                for matched, overlap in candidates:

                    if (
                        normalize_part(
                            matched["plant_part"]
                        )
                        != normalize_part(
                            requested_part
                        )
                    ):
                        continue

                    key = (
                        pr["plant_part_id"],
                        b["imppat_id"],
                        normalize_part(
                            requested_part
                        ),
                    )

                    if key in exact_keys:
                        continue

                    if not include_inferred:
                        continue

                    inferred_rows.append({
                        "plant_part_id":
                            pr["plant_part_id"],

                        "plant_name":
                            plant,

                        "plant_part":
                            requested_part,

                        "imppat_id":
                            b["imppat_id"],

                        "phytochemical_name":
                            b["phytochemical_name"],

                        "source_url":
                            (
                                f"{BASE_URL}/phytochemical/"
                                f"{quote(plant, safe='')}"
                            ),

                        "source":
                            "IMPPAT",

                        "references":
                            b["references"],

                        "association_level":
                            "reference_inferred",

                        "confidence":
                            "MEDIUM",

                        "evidence":
                            (
                                "Plant-level IMPPAT "
                                "association shares "
                                "reference(s) with "
                                f"explicit '{requested_part}' "
                                "association: "
                                f"{'; '.join(sorted(overlap))}"
                            ),
                    })

            if not exact and not any(
                x["plant_part_id"]
                == pr["plant_part_id"]
                for x in inferred_rows
            ):
                unmatched.append({
                    "plant_part_id":
                        pr["plant_part_id"],

                    "plant_name":
                        plant,

                    "plant_part":
                        requested_part,

                    "reason":
                        "No explicit part match; "
                        "no reference-supported "
                        "inference",
                })

        # Preserve all plant-level records
        # for audit instead of silently assigning them.
        for b in blanks:
            plant_level_candidates.append({
                "plant_name":
                    plant,

                "plant_part":
                    "",

                "imppat_id":
                    b["imppat_id"],

                "phytochemical_name":
                    b["phytochemical_name"],

                "source_url":
                    (
                        f"{BASE_URL}/phytochemical/"
                        f"{quote(plant, safe='')}"
                    ),

                "references":
                    b["references"],

                "reason":
                    "IMPPAT associates compound "
                    "with plant but gives no "
                    "plant part",
            })

        # Checkpoint every plant.
        checkpoint = pd.DataFrame(
            rows + inferred_rows,
            columns=OUTPUT_COLUMNS,
        )

        checkpoint = (
            checkpoint.drop_duplicates()
        )

        checkpoint.to_excel(
            output_file,
            index=False,
        )

    # Final main mapping.
    result = pd.DataFrame(
        rows + inferred_rows,
        columns=OUTPUT_COLUMNS,
    )

    if not result.empty:
        result = (
            result.drop_duplicates(
                subset=[
                    "plant_part_id",
                    "imppat_id",
                    "association_level",
                ]
            )
            .sort_values(
                [
                    "plant_part_id",
                    "phytochemical_name",
                ]
            )
            .reset_index(drop=True)
        )

    result.to_excel(
        output_file,
        index=False,
    )

    # Audit sheets are separate files so
    # the main mapping stays clean.
    pd.DataFrame(
        unmatched
    ).to_excel(
        os.path.splitext(
            output_file
        )[0] + "_unmatched.xlsx",
        index=False,
    )

    pd.DataFrame(
        inferred_rows,
        columns=OUTPUT_COLUMNS,
    ).to_excel(
        os.path.splitext(
            output_file
        )[0] + "_inferred.xlsx",
        index=False,
    )

    pd.DataFrame(
        plant_level_candidates
    ).drop_duplicates().to_excel(
        os.path.splitext(
            output_file
        )[0] + "_plant_level_candidates.xlsx",
        index=False,
    )

    exact_count = len(rows)

    inferred_count = len(
        inferred_rows
    )

    unique_compounds = (
        result["imppat_id"].nunique()
        if not result.empty
        else 0
    )

    print("\n" + "=" * 78)
    print("DONE")
    print("=" * 78)

    print(
        f"Explicit part mappings : "
        f"{exact_count}"
    )

    print(
        f"Reference-inferred     : "
        f"{inferred_count}"
    )

    print(
        f"Total main mappings    : "
        f"{len(result)}"
    )

    print(
        f"Unique phytochemicals  : "
        f"{unique_compounds}"
    )

    print(
        f"Unmatched plant-parts  : "
        f"{len(unmatched)}"
    )

    print(
        f"Plant-level candidates : "
        f"{len(plant_level_candidates)}"
    )

    print(
        f"Main output            : "
        f"{os.path.abspath(output_file)}"
    )

    print("=" * 78)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--file",
        required=True,
    )

    parser.add_argument(
        "--output",
        default="Phytochemical_Mapping.xlsx",
    )

    parser.add_argument(
        "--no-inferred",
        action="store_true",
        help=(
            "Disable conservative "
            "reference-supported inference"
        ),
    )

    args = parser.parse_args()

    build(
        args.file,
        args.output,
        include_inferred=not args.no_inferred,
    )


if __name__ == "__main__":
    main()