#!/usr/bin/env python3
"""
IMPPAT-NATIVE PHYTOCHEMICAL ENTITY BUILDER

Input:
    Phytochemical_Mapping.xlsx

Required columns:
    imppat_id
    phytochemical_name

For every unique IMPHY ID, this script reads the IMPPAT detailed page:
    https://cb.imsc.res.in/imppat/phytochemical-detailedpage/<IMPHY_ID>

Output:
    Phytochemicals.xlsx

IMPORTANT CH-EMBL ID VALIDATION RULE
-------------------------------------
A ChEMBL ID is accepted ONLY when the ChEMBL molecule's
standard InChIKey exactly matches the IMPPAT InChIKey.

Therefore:

    name -> first ChEMBL result
        IS NOT ALLOWED.

Name search may generate candidates, but every candidate must
pass exact InChIKey validation before being accepted.

Resolution priority:

    1. ChEMBL ID reported directly by IMPPAT
       -> validate against IMPPAT InChIKey.

    2. Exact ChEMBL InChIKey search.

    3. ChEMBL name search
       -> candidate generation only
       -> candidate must match IMPPAT InChIKey.

    4. No structural match
       -> chembl_id remains blank.

This prevents incorrect ChEMBL assignments caused by name-only
matching.

Other behavior:
- IMPPAT is the primary source.
- ClassyFire values are taken from IMPPAT.
- No direct ClassyFire API requests.
- Missing values remain blank, except chembl_id which is recorded as NA when unavailable.
- Results are cached.
- Checkpoint saved every 10 compounds.
- Safe to stop and rerun.
"""

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup


IMPPAT_DETAIL = "https://cb.imsc.res.in/imppat/phytochemical-detailedpage/{}"
CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data"

CACHE_DEFAULT = "imppat_phytochemical_cache.json"
CHECKPOINT_DEFAULT = "Phytochemicals_checkpoint.xlsx"


session = requests.Session()

session.headers.update({
    "User-Agent": "AyurvedicPlantProject/1.0 phytochemical research"
})


OUTPUT_COLUMNS = [
    "inchi_key",
    "phytochemical_name",
    "smiles",
    "imppat_id",
    "chembl_id",
    "source_url",
    "classyfire_kingdom",
    "classyfire_superclass",
    "classyfire_class",
    "classyfire_subclass",
]


# ----------------------------------------------------------------------
# GENERAL HELPERS
# ----------------------------------------------------------------------

def clean(value):
    if value is None:
        return ""

    if pd.isna(value):
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def norm(value):
    return clean(value).lower()


def fill_missing_with_na(df):
    """
    Convert only missing/blank output values to the literal string NA.

    Internal collector/checkpoint records continue to use empty strings so
    existing resolution, caching, and fallback logic is unchanged.
    """
    df = df.copy()

    for column in df.columns:

        df[column] = df[column].apply(
            lambda value:
                "NA"
                if (
                    value is None
                    or (
                        isinstance(value, str)
                        and not value.strip()
                    )
                    or pd.isna(value)
                    if not isinstance(value, (list, dict, tuple, set))
                    else False
                )
                else value
        )

    return df


def load_json(path):
    if not path.exists():
        return {}

    try:
        return json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    tmp.replace(path)


# ----------------------------------------------------------------------
# HTTP FETCH
# ----------------------------------------------------------------------

def fetch(
    url,
    cache,
    cache_key,
    retries=3,
    timeout=30
):
    """
    Fetch normal HTML pages with caching.
    """

    if cache_key in cache:

        item = cache[cache_key]

        if item.get("ok"):
            return item.get("text", "")

        return None

    for attempt in range(1, retries + 1):

        try:

            response = session.get(
                url,
                timeout=timeout
            )

            if response.status_code == 200:

                cache[cache_key] = {
                    "ok": True,
                    "text": response.text
                }

                return response.text

            if response.status_code == 429:

                wait = min(
                    30,
                    3 * attempt
                )

                print(
                    f"      HTTP 429 -> waiting {wait}s"
                )

                time.sleep(wait)

                continue

            if response.status_code in (400, 404):

                cache[cache_key] = {
                    "ok": False,
                    "status": response.status_code
                }

                return None

            print(
                f"      HTTP {response.status_code}"
            )

        except requests.RequestException as exc:

            if attempt < retries:

                wait = min(
                    10,
                    2 * attempt
                )

                print(
                    f"      request error -> "
                    f"retrying in {wait}s"
                )

                time.sleep(wait)

            else:

                print(
                    f"      request failed: {exc}"
                )

    cache[cache_key] = {
        "ok": False
    }

    return None


# ----------------------------------------------------------------------
# IMPPAT PARSING
# ----------------------------------------------------------------------

def page_text(soup):
    return clean(
        soup.get_text(
            " ",
            strip=True
        )
    )


def extract_after_label(
    text,
    label,
    stop_labels
):
    """
    Extract text following a visible IMPPAT label
    until the next known label.
    """

    pattern = (
        re.escape(label)
        + r"\s*(.*?)\s*(?="
        + "|".join(
            re.escape(x)
            for x in stop_labels
        )
        + r"|$)"
    )

    match = re.search(
        pattern,
        text,
        flags=re.I
    )

    if not match:
        return ""

    return clean(
        match.group(1)
    )


def extract_section(
    text,
    heading,
    next_headings
):
    pattern = (
        re.escape(heading)
        + r"\s*(.*?)\s*(?="
        + "|".join(
            re.escape(x)
            for x in next_headings
        )
        + r"|$)"
    )

    match = re.search(
        pattern,
        text,
        flags=re.I
    )

    return (
        clean(match.group(1))
        if match
        else ""
    )


def extract_value(
    text,
    label
):
    """
    Generic extraction for IMPPAT fields such as:

        SMILES: XXXXX
        InChIKey: XXXXX
    """

    pattern = (
        re.escape(label)
        + r"\s*:\s*(.*?)"
        r"(?=\s+(?:"
        r"[A-Za-z][A-Za-z0-9 /().-]{1,50}"
        r"):\s*|$)"
    )

    match = re.search(
        pattern,
        text,
        flags=re.I
    )

    if match:
        return clean(
            match.group(1)
        )

    return ""


def extract_external_identifier(
    text,
    label
):
    """
    IMPPAT renders external identifiers such as:

        CID:12921
        ChEMBL:CHEMBL3677268

    Handle both spacing variants.
    """

    pattern = (
        re.escape(label)
        + r"\s*:?\s*"
        r"([A-Za-z0-9._:-]+)"
    )

    match = re.search(
        pattern,
        text,
        flags=re.I
    )

    if match:
        return clean(
            match.group(1)
        )

    return ""


def parse_classification(text):

    fields = {
        "classyfire_kingdom": "",
        "classyfire_superclass": "",
        "classyfire_class": "",
        "classyfire_subclass": "",
    }

    labels = [
        (
            "classyfire_kingdom",
            "ClassyFire Kingdom"
        ),
        (
            "classyfire_superclass",
            "ClassyFire Superclass"
        ),
        (
            "classyfire_class",
            "ClassyFire Class"
        ),
        (
            "classyfire_subclass",
            "ClassyFire Subclass"
        ),
    ]

    # These are boundaries that mean the ClassyFire section
    # has ended. We MUST stop before NP Classifier data.
    stop_labels = [
        "NP Classifier Biosynthetic pathway",
        "NP Classifier Superclass",
        "NP Classifier Class",
        "NP-Likeness score",
        "Chemical structure download",
        "External chemical identifiers",
        "Synonymous chemical names",
    ]

    for key, label in labels:

        pattern = (
            re.escape(label)
            + r"\s*:?\s*(.*?)\s*(?="
            + "|".join(
                re.escape(stop)
                for stop in stop_labels
            )
            + r"|ClassyFire Kingdom"
            + r"|ClassyFire Superclass"
            + r"|ClassyFire Class"
            + r"|ClassyFire Subclass"
            + r"|$)"
        )

        match = re.search(
            pattern,
            text,
            flags=re.I | re.S
        )

        if match:

            value = clean(
                match.group(1)
            )

            value = value.rstrip(
                " ,;"
            )

            fields[key] = value

    return fields


def parse_imppat_page(
    html,
    requested_id,
    fallback_name
):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    text = page_text(soup)

    result = {
        "imppat_id": requested_id,
        "phytochemical_name": fallback_name,
        "inchi_key": "",
        "smiles": "",
        "chembl_id": "",
        "classyfire_kingdom": "",
        "classyfire_superclass": "",
        "classyfire_class": "",
        "classyfire_subclass": "",
    }

    # --------------------------------------------------------------
    # IMPPAT ID
    # --------------------------------------------------------------

    m = re.search(
        r"IMPPAT\s+Phytochemical\s+identifier\s*:\s*([A-Z0-9]+)",
        text,
        flags=re.I
    )

    if m:
        result["imppat_id"] = clean(
            m.group(1)
        )

    # --------------------------------------------------------------
    # PHYTOCHEMICAL NAME
    # --------------------------------------------------------------

    m = re.search(
        r"Phytochemical\s+name\s*:\s*(.*?)\s*"
        r"(?=Synonymous chemical names|"
        r"External chemical identifiers|"
        r"Chemical structure information|$)",
        text,
        flags=re.I
    )

    if m:
        result["phytochemical_name"] = clean(
            m.group(1)
        )

    # --------------------------------------------------------------
    # REQUIRED STRUCTURE FIELDS
    # --------------------------------------------------------------

    result["smiles"] = extract_value(
        text,
        "SMILES"
    )

    result["inchi_key"] = extract_value(
        text,
        "InChIKey"
    )

    # --------------------------------------------------------------
    # DIRECT CHEMBL ID FROM IMPPAT
    #
    # IMPORTANT:
    # This value is ONLY a candidate.
    # It will be validated later against InChIKey.
    # --------------------------------------------------------------

    chembl = extract_external_identifier(
        text,
        "ChEMBL"
    )

    if chembl.upper().startswith("CHEMBL"):
        result["chembl_id"] = chembl.upper()

    # --------------------------------------------------------------
    # CLASSIFICATION
    # --------------------------------------------------------------

    result.update(
        parse_classification(text)
    )

    return result


# ----------------------------------------------------------------------
# CHEMBL API
# ----------------------------------------------------------------------

def chembl_json(
    url,
    cache,
    key
):
    """
    Fetch JSON from ChEMBL with caching.
    """

    if key in cache:

        item = cache[key]

        return (
            item.get("data")
            if item.get("ok")
            else None
        )

    for attempt in range(1, 4):

        try:

            response = session.get(
                url,
                timeout=25
            )

            if response.status_code == 200:

                try:
                    data = response.json()
                except Exception:
                    data = None

                cache[key] = {
                    "ok": True,
                    "data": data
                }

                return data

            if response.status_code == 429:

                wait = min(
                    20,
                    3 * attempt
                )

                print(
                    f"      ChEMBL HTTP 429 "
                    f"-> waiting {wait}s"
                )

                time.sleep(wait)

                continue

            if response.status_code in (
                400,
                404
            ):

                cache[key] = {
                    "ok": False,
                    "status": response.status_code
                }

                return None

            print(
                f"      ChEMBL HTTP "
                f"{response.status_code}"
            )

        except requests.RequestException as exc:

            if attempt < 3:

                time.sleep(
                    2 * attempt
                )

            else:

                print(
                    f"      ChEMBL request failed: "
                    f"{exc}"
                )

    cache[key] = {
        "ok": False
    }

    return None


# ----------------------------------------------------------------------
# CHEMBL STRUCTURAL VALIDATION
# ----------------------------------------------------------------------

def chembl_from_id(
    chembl_id,
    expected_inchikey,
    cache
):
    """
    Validate a ChEMBL ID against the expected IMPPAT InChIKey.

    The ID is accepted ONLY when:

        ChEMBL molecule standard InChIKey
            ==
        IMPPAT InChIKey
    """

    chembl_id = clean(
        chembl_id
    ).upper()

    expected_inchikey = clean(
        expected_inchikey
    ).upper()

    if not chembl_id:
        return ""

    if not expected_inchikey:
        return ""

    if not chembl_id.startswith(
        "CHEMBL"
    ):
        return ""

    url = (
        f"{CHEMBL_API}/molecule/"
        f"{quote(chembl_id, safe='')}.json"
    )

    data = chembl_json(
        url,
        cache,
        "chembl_id:" + chembl_id
    )

    if not data:
        return ""

    molecule_id = clean(
        data.get(
            "molecule_chembl_id",
            ""
        )
    ).upper()

    structures = (
        data.get(
            "molecule_structures"
        )
        or {}
    )

    returned_inchikey = clean(
        structures.get(
            "standard_inchi_key",
            ""
        )
    ).upper()

    if (
        molecule_id == chembl_id
        and returned_inchikey == expected_inchikey
    ):
        return chembl_id

    return ""


def chembl_from_inchikey(
    inchikey,
    cache
):
    """
    Search ChEMBL by exact standard InChIKey.

    ONLY exact InChIKey matches are accepted.

    There is deliberately NO fallback to:
        first result
        first ChEMBL ID
        name similarity
    """

    inchikey = clean(
        inchikey
    ).upper()

    if not inchikey:
        return ""

    url = (
        f"{CHEMBL_API}/molecule.json?"
        f"molecule_structures__standard_inchi_key="
        f"{quote(inchikey, safe='')}"
        f"&limit=20"
    )

    data = chembl_json(
        url,
        cache,
        "chembl_inchikey:" + norm(
            inchikey
        )
    )

    if not data:
        return ""

    for molecule in data.get(
        "molecules",
        []
    ):

        cid = clean(
            molecule.get(
                "molecule_chembl_id",
                ""
            )
        ).upper()

        structures = (
            molecule.get(
                "molecule_structures"
            )
            or {}
        )

        returned_key = clean(
            structures.get(
                "standard_inchi_key",
                ""
            )
        ).upper()

        if (
            cid.startswith("CHEMBL")
            and returned_key == inchikey
        ):
            return cid

    return ""


def chembl_from_name(
    name,
    expected_inchikey,
    cache
):
    """
    Search ChEMBL by name ONLY to generate candidates.

    A name match is NOT enough.

    Every candidate is checked against the expected
    IMPPAT InChIKey.

    Therefore:

        name == candidate name
            DOES NOT mean
        accept candidate

    The candidate must also satisfy:

        candidate standard InChIKey
            ==
        IMPPAT InChIKey
    """

    name = clean(name)

    expected_inchikey = clean(
        expected_inchikey
    ).upper()

    if not name:
        return ""

    if not expected_inchikey:
        return ""

    url = (
        f"{CHEMBL_API}/molecule/search.json?"
        f"q={quote(name, safe='')}"
        f"&limit=20"
    )

    data = chembl_json(
        url,
        cache,
        "chembl_name:" + norm(name)
    )

    if not data:
        return ""

    molecules = data.get(
        "molecules",
        []
    )

    wanted = norm(name)

    # --------------------------------------------------------------
    # Exact preferred-name candidates first.
    # BUT STILL VALIDATE STRUCTURE.
    # --------------------------------------------------------------

    exact_matches = []

    for molecule in molecules:

        if norm(
            molecule.get(
                "pref_name",
                ""
            )
        ) == wanted:

            exact_matches.append(
                molecule
            )

    # Then remaining candidates.
    candidates = (
        exact_matches
        + [
            molecule
            for molecule in molecules
            if molecule not in exact_matches
        ]
    )

    # --------------------------------------------------------------
    # STRUCTURAL VALIDATION
    # --------------------------------------------------------------

    for molecule in candidates:

        cid = clean(
            molecule.get(
                "molecule_chembl_id",
                ""
            )
        ).upper()

        if not cid.startswith(
            "CHEMBL"
        ):
            continue

        structures = (
            molecule.get(
                "molecule_structures"
            )
            or {}
        )

        returned_key = clean(
            structures.get(
                "standard_inchi_key",
                ""
            )
        ).upper()

        # CRITICAL SAFETY CHECK
        if returned_key == expected_inchikey:

            return cid

    # No structurally validated candidate.
    return ""


def resolve_chembl(
    result,
    cache
):
    """
    Resolve ChEMBL conservatively.

    Priority:

        1. IMPPAT ChEMBL ID
           -> validate against InChIKey.

        2. Exact ChEMBL InChIKey search.

        3. ChEMBL name search
           -> candidate only
           -> exact InChIKey validation.

        4. Blank.

    NEVER accept a ChEMBL ID merely because:
        - the names match
        - it is the first search result
        - it was returned by a fuzzy name search
    """

    expected_inchikey = clean(
        result.get(
            "inchi_key",
            ""
        )
    ).upper()

    if not expected_inchikey:

        print(
            "      ChEMBL validation skipped: "
            "no IMPPAT InChIKey"
        )

        return ""

    # --------------------------------------------------------------
    # 1. DIRECT IMPPAT CHEMBL ID
    # --------------------------------------------------------------

    direct_id = clean(
        result.get(
            "chembl_id",
            ""
        )
    ).upper()

    if direct_id.startswith(
        "CHEMBL"
    ):

        print(
            f"      ChEMBL: validating "
            f"{direct_id} against InChIKey"
        )

        validated = chembl_from_id(
            direct_id,
            expected_inchikey,
            cache
        )

        if validated:

            return validated

        print(
            f"      ChEMBL: rejecting "
            f"{direct_id} "
            f"(InChIKey mismatch)"
        )

    # --------------------------------------------------------------
    # 2. EXACT INCHIKEY SEARCH
    # --------------------------------------------------------------

    print(
        "      ChEMBL: exact InChIKey lookup"
    )

    cid = chembl_from_inchikey(
        expected_inchikey,
        cache
    )

    if cid:

        print(
            f"      ChEMBL: {cid} "
            f"(InChIKey validated)"
        )

        return cid

    # --------------------------------------------------------------
    # 3. NAME SEARCH AS CANDIDATE GENERATOR
    # --------------------------------------------------------------

    print(
        "      ChEMBL: name search "
        "(candidate validation required)"
    )

    cid = chembl_from_name(
        result.get(
            "phytochemical_name",
            ""
        ),
        expected_inchikey,
        cache
    )

    if cid:

        print(
            f"      ChEMBL: {cid} "
            f"(name candidate + "
            f"InChIKey validated)"
        )

        return cid

    # --------------------------------------------------------------
    # 4. NO STRUCTURAL MATCH
    # --------------------------------------------------------------

    print(
        "      ChEMBL: no structurally "
        "validated match"
    )

    return ""


# ----------------------------------------------------------------------
# INPUT
# ----------------------------------------------------------------------

def load_entities(path):

    df = pd.read_excel(
        path,
        dtype=str
    ).fillna("")

    required = {
        "imppat_id",
        "phytochemical_name"
    }

    missing = (
        required
        - set(df.columns)
    )

    if missing:

        raise ValueError(
            "Missing required columns: "
            + ", ".join(
                sorted(missing)
            )
        )

    df["imppat_id"] = (
        df["imppat_id"]
        .map(clean)
    )

    df["phytochemical_name"] = (
        df["phytochemical_name"]
        .map(clean)
    )

    df = df[
        (df["imppat_id"] != "")
        &
        (df["phytochemical_name"] != "")
    ]

    return (
        df[
            [
                "imppat_id",
                "phytochemical_name"
            ]
        ]
        .drop_duplicates(
            "imppat_id"
        )
        .reset_index(
            drop=True
        )
    )


# ----------------------------------------------------------------------
# CHECKPOINT
# ----------------------------------------------------------------------

def load_checkpoint(path):

    if not path.exists():
        return {}

    try:

        df = pd.read_excel(
            path,
            dtype=str
        ).fillna("")

    except Exception:

        return {}

    if "imppat_id" not in df.columns:
        return {}

    records = {}

    for _, row in df.iterrows():

        iid = clean(
            row.get(
                "imppat_id"
            )
        )

        if not iid:
            continue

        record = {}

        for col in OUTPUT_COLUMNS:

            record[col] = clean(
                row.get(col)
            )

        records[iid] = record

    return records


def save_checkpoint(
    path,
    records
):

    rows = list(
        records.values()
    )

    df = pd.DataFrame(
        rows,
        columns=OUTPUT_COLUMNS
    )

    df.to_excel(
        path,
        index=False
    )


# ----------------------------------------------------------------------
# BUILD
# ----------------------------------------------------------------------

def build(
    input_file,
    output_file,
    cache_file,
    checkpoint_file,
    delay
):

    print("=" * 70)
    print(
        "IMPPAT-NATIVE PHYTOCHEMICAL ENTITY BUILDER"
    )
    print("=" * 70)

    entities = load_entities(
        input_file
    )

    print(
        f"Unique IMPPAT compounds: "
        f"{len(entities)}"
    )

    cache = load_json(
        cache_file
    )

    records = load_checkpoint(
        checkpoint_file
    )

    if records:

        print(
            f"Checkpoint records loaded: "
            f"{len(records)}"
        )

    for i, row in entities.iterrows():

        iid = row["imppat_id"]
        name = row["phytochemical_name"]

        # ----------------------------------------------------------
        # DO NOT blindly trust checkpoint ChEMBL values.
        #
        # A checkpoint may have been produced by the OLD unsafe
        # name-based resolver.
        #
        # If structure fields are already available, we still
        # revalidate the ChEMBL ID before accepting it.
        # ----------------------------------------------------------

        if iid in records:

            old = records[iid]

            if (
                old.get("inchi_key")
                and old.get("smiles")
            ):

                print(
                    f"[{i + 1}/{len(entities)}] "
                    f"{iid} - cached"
                )

                # --------------------------------------------------
                # Revalidate old ChEMBL value.
                # --------------------------------------------------

                old_chembl = clean(
                    old.get(
                        "chembl_id",
                        ""
                    )
                ).upper()

                if old_chembl and old_chembl != "NA":

                    print(
                        f"    ChEMBL: revalidating "
                        f"{old_chembl}"
                    )

                    validated = chembl_from_id(
                        old_chembl,
                        old.get(
                            "inchi_key",
                            ""
                        ),
                        cache
                    )

                    if validated:
                        old["chembl_id"] = validated

                        print(
                            f"    ChEMBL: "
                            f"{validated} "
                            f"(validated)"
                        )

                    else:
                        old["chembl_id"] = "NA"

                        print(
                            f"    ChEMBL: "
                            f"{old_chembl} "
                            f"REJECTED "
                            f"(InChIKey mismatch) -> NA"
                        )

                    records[iid] = old

                elif not old_chembl:
                    old["chembl_id"] = "NA"

                    records[iid] = old

                continue

        print(
            f"[{i + 1}/{len(entities)}] "
            f"{iid}"
        )

        print(
            f"    {name}"
        )

        # ----------------------------------------------------------
        # IMPPAT PAGE
        # ----------------------------------------------------------

        url = IMPPAT_DETAIL.format(
            quote(
                iid,
                safe=""
            )
        )

        html = fetch(
            url,
            cache,
            "imppat_detail:" + iid
        )

        if not html:

            print(
                "    IMPPAT: page unavailable"
            )

            records[iid] = {
                **{
                    col: ""
                    for col in OUTPUT_COLUMNS
                },
                "imppat_id": iid,
                "phytochemical_name": name,
                "chembl_id": "NA",
                "source_url": url,
            }

            continue

        # ----------------------------------------------------------
        # PARSE IMPPAT
        # ----------------------------------------------------------

        result = parse_imppat_page(
            html,
            iid,
            name
        )

        result["source_url"] = url

        print(
            "    SMILES: "
            + (
                "resolved"
                if result["smiles"]
                else "not found"
            )
        )

        print(
            "    InChIKey: "
            + (
                "resolved"
                if result["inchi_key"]
                else "not found"
            )
        )

        # ----------------------------------------------------------
        # ALWAYS RUN STRUCTURAL CHEMBL RESOLUTION
        #
        # This is different from the old code.
        #
        # OLD:
        #
        # if IMPPAT had a ChEMBL ID:
        #     accept immediately
        #
        # NEW:
        #
        # every ChEMBL ID must pass InChIKey validation.
        # ----------------------------------------------------------

        original_chembl = clean(
            result.get(
                "chembl_id",
                ""
            )
        ).upper()

        # Clear it first.
        #
        # resolve_chembl receives the original as a candidate.
        result["chembl_id"] = ""

        if original_chembl:

            print(
                f"    ChEMBL: IMPPAT candidate "
                f"{original_chembl}"
            )

        else:

            print(
                "    ChEMBL: no IMPPAT ID"
            )

        validation_input = {
            **result,
            "chembl_id": original_chembl,
        }

        validated_chembl = resolve_chembl(
            validation_input,
            cache
        )

        if validated_chembl:

            result["chembl_id"] = (
                validated_chembl
            )

        else:

            result["chembl_id"] = "NA"

        # ----------------------------------------------------------
        # SAVE RECORD
        # ----------------------------------------------------------

        records[iid] = result

        # ----------------------------------------------------------
        # CHECKPOINT
        # ----------------------------------------------------------

        if (
            (i + 1) % 10 == 0
        ):

            save_json(
                cache_file,
                cache
            )

            save_checkpoint(
                checkpoint_file,
                records
            )

            print(
                f"    checkpoint saved "
                f"({i + 1}/{len(entities)})"
            )

        if delay > 0:

            time.sleep(
                delay
            )

    # ------------------------------------------------------------------
    # FINAL CACHE SAVE
    # ------------------------------------------------------------------

    save_json(
        cache_file,
        cache
    )

    # ------------------------------------------------------------------
    # FINAL CHECKPOINT
    # ------------------------------------------------------------------

    save_checkpoint(
        checkpoint_file,
        records
    )

    # ------------------------------------------------------------------
    # OUTPUT
    # ------------------------------------------------------------------

    output_rows = []

    for iid in entities[
        "imppat_id"
    ]:

        record = records.get(
            iid
        )

        if not record:

            record = {
                **{
                    col: ""
                    for col in OUTPUT_COLUMNS
                },
                "imppat_id": iid,
                "chembl_id": "NA",
            }

        output_rows.append(
            record
        )

    output_df = pd.DataFrame(
        output_rows,
        columns=OUTPUT_COLUMNS
    )

    # Final node-table representation:
    # every missing/blank value is written as literal "NA".
    #
    # Do this only after all collection, checkpoint, cache, and
    # resolution logic has completed so no existing logic changes.
    output_df = fill_missing_with_na(
        output_df
    )

    output_df.to_excel(
        output_file,
        index=False
    )

    # ------------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------------

    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)

    print(
        f"Output: {output_file}"
    )

    print(
        f"Entities: {len(output_df)}"
    )

    for col in [
        "inchi_key",
        "smiles",
        "chembl_id",
        "classyfire_kingdom",
        "classyfire_superclass",
        "classyfire_class",
        "classyfire_subclass",
    ]:

        count = (
            output_df[col]
            .astype(str)
            .str.strip()
            != ""
        ).sum()

        print(
            f"{col}: {count}"
        )

    print()
    print(
        "Primary source: IMPPAT"
    )

    print(
        "ChEMBL: structurally validated "
        "against IMPPAT InChIKey"
    )

    print(
        "ClassyFire: extracted from IMPPAT; "
        "no direct ClassyFire API used."
    )


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--file",
        required=True,
        help="Phytochemical_Mapping.xlsx"
    )

    parser.add_argument(
        "--output",
        required=True,
        help="Phytochemicals.xlsx"
    )

    parser.add_argument(
        "--cache",
        default=CACHE_DEFAULT
    )

    parser.add_argument(
        "--checkpoint",
        default=CHECKPOINT_DEFAULT
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=0.25
    )

    args = parser.parse_args()

    build(
        Path(args.file),
        Path(args.output),
        Path(args.cache),
        Path(args.checkpoint),
        args.delay
    )


if __name__ == "__main__":
    main()