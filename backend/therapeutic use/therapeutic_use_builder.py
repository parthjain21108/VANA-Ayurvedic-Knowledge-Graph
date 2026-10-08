#!/usr/bin/env python3

import argparse
import ast
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

MESH_BASE = "https://id.nlm.nih.gov/mesh"
MESH_LOOKUP_BASE = "https://id.nlm.nih.gov/mesh/lookup"

UNRESOLVED_FILE = "therapeutic_use_unresolved.xlsx"

FINAL_NODE_COLUMNS = [
    "mesh_id",
    "therapeutic_use",
    "mesh_category",
    "mesh_category_name",
    "mesh_sub_category_name",
    "source",
]

MAPPING_COLUMNS = [
    "plant_part_id",
    "plant_name",
    "plant_part",
    "mesh_id",
    "therapeutic_use",
    "source",
]

HEADERS = {
    "User-Agent": (
        "Ayurvedic-Plant-Therapeutic-Use-Project/1.0 "
        "(educational research)"
    ),
    "Accept": "application/json",
}

TIMEOUT = 20

MESH_CATEGORIES = {
    "A": "Anatomy",
    "B": "Organisms",
    "C": "Diseases",
    "D": "Chemicals and Drugs",
    "E": "Analytical, Diagnostic and Therapeutic Techniques and Equipment",
    "F": "Psychiatry and Psychology",
    "G": "Phenomena and Processes",
    "H": "Disciplines and Occupations",
    "I": "Anthropology, Education, Sociology and Social Phenomena",
    "J": "Technology, Industry, Agriculture",
    "K": "Humanities",
    "L": "Information Science",
    "M": "Named Groups",
    "N": "Health Care",
    "V": "Publication Characteristics",
    "Z": "Geographicals",
}

# Exact names for the first two characters of the MeSH tree
# number (e.g. D27 = Chemical Actions and Uses).
# Fall back to the broad root category when a code is not listed.
MESH_TREE_CATEGORY_NAMES = {
    "D27": "Chemical Actions and Uses",
    "D50": "Physiological Effects of Drugs",
    "D23": "Biological Factors",
    "D26": "Pharmacological and Therapeutic Uses",
    "C12": "Diseases",
    "C10": "Neoplasms",
    "C23": "Pathological Conditions, Signs and Symptoms",
    "F01": "Behavior and Behavior Mechanisms",
    "F02": "Psychological Phenomena",
    "G07": "Physiological Phenomena",
    "N02": "Health Services and Health Care Facilities, and Other Health Care Services",
}

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# HELPERS
# ============================================================

def clean(value):
    """
    Normalize values coming from Excel, JSON, JSON-LD, or cache.

    JSON-LD values can arrive either as real dictionaries or as
    stringified dictionaries such as:

        "{'@language': 'en', '@value': 'Psychotropic Drugs'}"

    In both cases the stored value must be the plain text:
        Psychotropic Drugs
    """

    if value is None:
        return ""

    # ------------------------------------------------------------
    # Real dict / JSON-LD object
    # ------------------------------------------------------------

    if isinstance(value, dict):

        if "@value" in value:
            return clean(value["@value"])

        if "value" in value:
            return clean(value["value"])

        for key in (
            "prefLabel",
            "preferredTerm",
            "label",
            "name",
        ):
            if key in value:
                result = clean(value[key])
                if result:
                    return result

        return ""

    # ------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------

    if isinstance(value, (list, tuple, set)):

        values = []

        for item in value:
            item = clean(item)

            if item and item not in values:
                values.append(item)

        return "; ".join(values)

    # ------------------------------------------------------------
    # Pandas NA
    # ------------------------------------------------------------

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    value = str(value).strip()

    if not value:
        return ""

    # ------------------------------------------------------------
    # Stringified JSON / Python dict/list.
    #
    # Parse only when the entire string looks structured.
    # This prevents ordinary therapeutic names from being altered.
    # ------------------------------------------------------------

    if (
        value.startswith("{")
        and value.endswith("}")
    ) or (
        value.startswith("[")
        and value.endswith("]")
    ):

        parsed = None

        try:
            parsed = json.loads(value)
        except Exception:
            try:
                parsed = ast.literal_eval(value)
            except Exception:
                parsed = None

        if parsed is not None and parsed != value:
            return clean(parsed)

    value = value.replace("\r", " ")
    value = value.replace("\n", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def clean_id(value):
    """Normalize an identifier without stringifying JSON-LD objects."""

    return clean(value).upper()


def normalize(value):
    value = clean(value).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return value.strip()


def mesh_id_from_text(value):
    value = clean(value)

    match = re.search(
        r"\b(?:MESH\s*:\s*)?(D\d{6})\b",
        value,
        flags=re.IGNORECASE,
    )

    return (
        match.group(1).upper()
        if match
        else ""
    )


def load_cache(path):
    path = Path(path)

    if not path.exists():
        return {}

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as handle:
            data = json.load(handle)

        return data if isinstance(data, dict) else {}

    except Exception as exc:
        print(
            f"WARNING: could not load MeSH cache: {exc}"
        )
        return {}


def save_cache(cache, path):
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
                indent=2,
                ensure_ascii=False,
            )

        os.replace(
            temp,
            path,
        )

    except Exception as exc:
        print(
            f"WARNING: could not save MeSH cache: {exc}"
        )

        try:
            if temp.exists():
                temp.unlink()
        except Exception:
            pass


def load_project_therapeutic_cache():
    """
    Load the project's existing therapeutic_cache.json when available.

    This cache already contains:
      - IMPPAT therapeutic names keyed by plant
      - authoritative MeSH browser category metadata

    It is used as the primary enrichment source so the final node
    reproduces the project's established therapeutic-use schema.
    """
    project_root = (
        Path(__file__).resolve().parent.parent
    )

    cache_path = (
        project_root
        / "therapeutic_cache.json"
    )

    if not cache_path.exists():
        return {}

    try:
        with cache_path.open(
            "r",
            encoding="utf-8",
        ) as handle:
            data = json.load(handle)

        return data if isinstance(data, dict) else {}

    except Exception:
        return {}


def build_imppat_name_lookup(
    therapeutic_cache,
    plant_names,
):
    lookup = {}

    wanted = {
        clean(name).lower()
        for name in plant_names
        if clean(name)
    }

    for key, value in therapeutic_cache.items():

        prefix = "imppat::therapeutics::"

        if not key.lower().startswith(prefix):
            continue

        plant_name = key[len(prefix):].strip().lower()

        if plant_name not in wanted:
            continue

        rows = (
            value.get("rows", [])
            if isinstance(value, dict)
            else []
        )

        for row in rows:

            mesh_id = mesh_id_from_text(
                row.get("mesh_id", "")
            )

            if not mesh_id:
                continue

            name = clean(
                row.get(
                    "therapeutic_use",
                    "",
                )
            )

            if name:
                lookup.setdefault(
                    mesh_id,
                    name,
                )

    return lookup


def build_mesh_browser_lookup(
    therapeutic_cache,
):
    """
    Load only clean cached MeSH browser metadata.

    Older cache entries may contain JSON-LD values serialized as text.
    Those are normalized by clean(), but if hierarchy metadata is
    missing/obviously malformed we let the official MeSH Browser
    enrichment refresh the record later.
    """

    lookup = {}

    prefix = "mesh_browser::"

    for key, value in therapeutic_cache.items():

        if not key.lower().startswith(prefix):
            continue

        mesh_id = mesh_id_from_text(
            key[len(prefix):]
        )

        if not mesh_id:
            continue

        if not isinstance(value, dict):
            continue

        result = {
            "mesh_id": mesh_id,
            "category": clean(
                value.get(
                    "mesh_category",
                    "",
                )
            ),
            "category_name": clean(
                value.get(
                    "mesh_category_name",
                    "",
                )
            ),
            "sub_category_name": clean(
                value.get(
                    "mesh_sub_category_name",
                    "",
                )
            ),
        }

        # A cached record is considered usable only when its
        # hierarchy fields are actual resolved text, not empty or
        # an obviously serialized object.
        if (
            result["category"]
            and result["category_name"]
            and not result["category"].startswith("{")
            and not result["category_name"].startswith("{")
            and not result["sub_category_name"].startswith("{")
        ):
            lookup[mesh_id] = result

    return lookup


def request_json(url, params=None):
    try:
        response = session.get(
            url,
            params=params,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        print(
            f"      MeSH request failed: {exc}"
        )
        return None

    if response.status_code != 200:
        print(
            f"      MeSH lookup HTTP "
            f"{response.status_code}"
        )
        return None

    try:
        return response.json()
    except Exception:
        print(
            "      MeSH lookup returned invalid JSON"
        )
        return None


def find_values(obj, wanted_keys):
    found = []

    if isinstance(obj, dict):

        for key, value in obj.items():

            key_text = str(key)

            if key_text in wanted_keys:
                if isinstance(value, list):
                    found.extend(value)
                else:
                    found.append(value)

            found.extend(
                find_values(
                    value,
                    wanted_keys,
                )
            )

    elif isinstance(obj, list):

        for item in obj:
            found.extend(
                find_values(
                    item,
                    wanted_keys,
                )
            )

    return found


def find_descriptor_ids(obj):
    found = []

    if isinstance(obj, str):

        for value in re.findall(
            r"(?:id\.nlm\.nih\.gov/mesh/)?(D\d{6})\b",
            obj,
            flags=re.IGNORECASE,
        ):

            value = value.upper()

            if value not in found:
                found.append(value)

    elif isinstance(obj, list):

        for item in obj:
            for value in find_descriptor_ids(item):
                if value not in found:
                    found.append(value)

    elif isinstance(obj, dict):

        for value in obj.values():
            for item in find_descriptor_ids(value):
                if item not in found:
                    found.append(item)

    return found



# ============================================================
# OFFICIAL NLM MESH BROWSER ENRICHMENT
# ============================================================

def scrape_mesh_metadata(
    mesh_id,
    cache,
    cache_file,
):
    """
    Resolve hierarchy metadata from the official NLM MeSH Browser.

    Final TherapeuticUse nodes keep ONE primary MeSH category/path.

    Selection rule:
      1. Collect descriptor tree numbers from the official Browser.
      2. Select the shortest valid tree path.
      3. For equal-depth paths, select lexical tree order.
      4. Derive category/category-name/immediate sub-category from
         that single selected path.

    This intentionally prevents values such as:
        C01; C08
        Infections; Respiratory Tract Diseases

    from appearing in one TherapeuticUse node.
    """

    mesh_id = clean_id(mesh_id)

    if not re.fullmatch(r"D\d{6}", mesh_id):
        return {
            "mesh_category": "NA",
            "mesh_category_name": "NA",
            "mesh_sub_category_name": "NA",
        }

    cache_key = "mesh_browser::" + mesh_id
    cached = cache.get(cache_key)

    if isinstance(cached, dict):

        cached_result = {
            "mesh_category":
                clean(
                    cached.get(
                        "mesh_category",
                        "",
                    )
                ),

            "mesh_category_name":
                clean(
                    cached.get(
                        "mesh_category_name",
                        "",
                    )
                ),

            "mesh_sub_category_name":
                clean(
                    cached.get(
                        "mesh_sub_category_name",
                        "",
                    )
                ),
        }

        # Old cache entries may contain the now-invalid multi-category
        # representation. Do not use those entries for the final node;
        # refresh them from the official MeSH Browser.
        has_multiple_categories = (
            ";" in cached_result["mesh_category"]
            or ";" in cached_result["mesh_category_name"]
        )

        has_serialized_object = any(
            value.startswith("{")
            or value.startswith("[")
            for value in (
                cached_result["mesh_category"],
                cached_result["mesh_category_name"],
                cached_result["mesh_sub_category_name"],
            )
            if value
        )

        if (
            cached_result["mesh_category"]
            and cached_result["mesh_category_name"]
            and not has_multiple_categories
            and not has_serialized_object
        ):
            return cached_result

    url = (
        "https://meshb.nlm.nih.gov/record/ui"
        "/?ui="
        + quote(
            mesh_id,
            safe="",
        )
    )

    print(
        f"      MeSH Browser -> {mesh_id}"
    )

    try:

        response = session.get(
            url,
            timeout=TIMEOUT,
            headers={
                "Accept":
                    "text/html,application/xhtml+xml",
            },
        )

    except requests.RequestException:

        result = {
            "mesh_category": "NA",
            "mesh_category_name": "NA",
            "mesh_sub_category_name": "NA",
        }

        cache[cache_key] = result

        save_cache(
            cache,
            cache_file,
        )

        return result

    if response.status_code != 200:

        result = {
            "mesh_category": "NA",
            "mesh_category_name": "NA",
            "mesh_sub_category_name": "NA",
        }

        cache[cache_key] = result

        save_cache(
            cache,
            cache_file,
        )

        return result

    try:

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        # --------------------------------------------------------
        # Build a lookup of all visible MeSH tree labels.
        # --------------------------------------------------------

        hierarchy = {}

        for anchor in soup.find_all("a"):

            anchor_text = clean(
                anchor.get_text(
                    " ",
                    strip=True,
                )
            )

            match = re.match(
                r"^(.+?)\s*\[([A-Z]\d{2}(?:\.\d{3})*)\]$",
                anchor_text,
            )

            if not match:
                continue

            hierarchy[
                match.group(2).upper()
            ] = clean(
                match.group(1)
            )

        # --------------------------------------------------------
        # Extract the actual Tree Number(s) section.
        #
        # Different MeSH Browser markup versions place the values
        # at different levels around the "Tree Number(s)" label.
        # Check the label parent and a few ancestors.
        # --------------------------------------------------------

        tree_numbers = []

        tree_label = soup.find(
            string=lambda value: (
                value
                and normalize(value)
                == "tree number s"
            )
        )

        if tree_label is not None:

            containers = []

            current = tree_label.parent

            for _ in range(4):

                if current is None:
                    break

                containers.append(
                    current
                )

                current = current.parent

            for container in containers:

                container_text = clean(
                    container.get_text(
                        " ",
                        strip=True,
                    )
                )

                found = re.findall(
                    r"\b([A-Z]\d{2}(?:\.\d{3})*)\b",
                    container_text.upper(),
                )

                if found:

                    tree_numbers = found
                    break

        # Fallback: when the page markup does not expose the
        # Tree Number(s) container cleanly, use tree-number links
        # but prefer the most specific descriptor paths over their
        # ancestor entries.
        if not tree_numbers:

            hierarchy_keys = list(
                hierarchy.keys()
            )

            # Keep only paths that are deeper than a top-level
            # category when such paths exist.
            detailed = [
                key
                for key in hierarchy_keys
                if "." in key
            ]

            tree_numbers = (
                detailed
                if detailed
                else hierarchy_keys
            )

        tree_numbers = list(
            dict.fromkeys(
                tree_numbers
            )
        )

        valid_tree_numbers = [
            value
            for value in tree_numbers
            if re.fullmatch(
                r"[A-Z]\d{2}(?:\.\d{3})*",
                value,
            )
        ]

        # --------------------------------------------------------
        # ONE category/path only.
        #
        # Same deterministic ordering already used by resolve_mesh():
        # shortest tree path, then lexical order.
        # --------------------------------------------------------

        valid_tree_numbers = sorted(
            valid_tree_numbers,
            key=lambda value: (
                len(
                    value.split(".")
                ),
                value,
            ),
        )

        selected_tree = (
            valid_tree_numbers[0]
            if valid_tree_numbers
            else ""
        )

        if not selected_tree:

            result = {
                "mesh_category": "NA",
                "mesh_category_name": "NA",
                "mesh_sub_category_name": "NA",
            }

            cache[cache_key] = result

            save_cache(
                cache,
                cache_file,
            )

            return result

        parts = selected_tree.split(
            "."
        )

        category_code = parts[0]

        category_name = hierarchy.get(
            category_code,
            "",
        )

        if not category_name:

            category_name = (
                MESH_TREE_CATEGORY_NAMES.get(
                    category_code,
                    MESH_CATEGORIES.get(
                        category_code[0],
                        "",
                    ),
                )
            )

        sub_category_name = ""

        if len(parts) >= 2:

            sub_code = (
                category_code
                + "."
                + parts[1]
            )

            sub_category_name = hierarchy.get(
                sub_code,
                "",
            )

        result = {
            "mesh_category":
                clean(
                    category_code
                )
                or "NA",

            "mesh_category_name":
                clean(
                    category_name
                )
                or "NA",

            "mesh_sub_category_name":
                clean(
                    sub_category_name
                )
                or "NA",
        }

        cache[cache_key] = result

        save_cache(
            cache,
            cache_file,
        )

        return result

    except Exception:

        result = {
            "mesh_category": "NA",
            "mesh_category_name": "NA",
            "mesh_sub_category_name": "NA",
        }

        cache[cache_key] = result

        save_cache(
            cache,
            cache_file,
        )

        return result


# ============================================================
# MESH RESOLUTION
# ============================================================

def fetch_resource_json(
    resource_uri,
    cache,
    cache_file,
):
    resource_uri = clean(resource_uri)

    if not resource_uri:
        return None

    cache_key = (
        "RESOURCE:"
        + resource_uri
    )

    if cache_key in cache:

        cached = cache[cache_key]

        if cached == "__NOT_FOUND__":
            return None

        return cached

    url = (
        resource_uri.rstrip("/")
        + ".json"
    )

    data = request_json(url)

    if data is None:

        cache[
            cache_key
        ] = "__NOT_FOUND__"

        save_cache(
            cache,
            cache_file,
        )

        return None

    cache[
        cache_key
    ] = data

    save_cache(
        cache,
        cache_file,
    )

    return data


def _extract_broader_descriptor_ids(data):
    """Return descriptor IDs referenced by MeSH broader-descriptor fields."""
    candidates = find_values(
        data,
        {
            "broaderDescriptor",
            "broaderDescriptorRelation",
            "broaderConcept",
            "broaderConceptDescriptor",
        },
    )

    found = []

    for value in candidates:
        if isinstance(value, dict):
            value = json.dumps(
                value,
                ensure_ascii=False,
            )

        for mesh_id in find_descriptor_ids(value):
            if mesh_id not in found:
                found.append(mesh_id)

    return found


def resolve_mesh(
    mesh_id,
    cache,
    cache_file,
    _visited=None,
):
    mesh_id = mesh_id_from_text(mesh_id)

    if not mesh_id:
        return None

    if _visited is None:
        _visited = set()

    if mesh_id in _visited:
        return None

    _visited = set(_visited)
    _visited.add(mesh_id)

    cache_key = "MESH:" + mesh_id

    if cache_key in cache:
        return cache[cache_key]

    data = fetch_resource_json(
        f"{MESH_BASE}/{mesh_id}",
        cache,
        cache_file,
    )

    if not data:
        return None

    labels = find_values(
        data,
        {
            "prefLabel",
            "preferredTerm",
            "label",
        },
    )

    name = ""

    for value in labels:
        value = clean(value)

        if not value:
            continue

        if value.startswith(
            ("http://", "https://")
        ):
            continue

        # Prefer simple human-readable strings over JSON-LD objects.
        if isinstance(value, str):
            name = value
            break

    # ------------------------------------------------------------
    # MeSH tree numbers
    #
    # Choose the most specific path deterministically, preferring
    # the shortest valid tree path and then lexical order. This
    # prevents RDF traversal order from incorrectly selecting D50
    # when the descriptor also belongs to D27.
    # ------------------------------------------------------------

    tree_values = find_values(
        data,
        {"treeNumber"},
    )

    tree_numbers = []

    for value in tree_values:
        for match in re.findall(
            r"\b[A-Z]\d{2}(?:\.\d{3})*\b",
            clean(value),
        ):
            if match not in tree_numbers:
                tree_numbers.append(match)

    tree_numbers = sorted(
        tree_numbers,
        key=lambda value: (
            len(value.split(".")),
            value,
        ),
    )

    category = ""
    category_name = ""
    sub_category_name = ""

    selected_tree = (
        tree_numbers[0]
        if tree_numbers
        else ""
    )

    if selected_tree:
        parts = selected_tree.split(".")
        root = parts[0][0]

        category = selected_tree[:3]

        category_name = (
            MESH_TREE_CATEGORY_NAMES.get(
                category,
                MESH_CATEGORIES.get(
                    root,
                    "",
                ),
            )
        )

        # --------------------------------------------------------
        # Sub-category:
        # use the immediate broader MeSH descriptor when available.
        # For D27.* this resolves to labels such as
        # "Pharmacologic Actions".
        # --------------------------------------------------------

        broader_ids = (
            _extract_broader_descriptor_ids(data)
        )

        for parent_id in broader_ids:
            parent = resolve_mesh(
                parent_id,
                cache,
                cache_file,
                _visited=_visited,
            )

            if parent and parent.get("name"):
                sub_category_name = clean(
                    parent["name"]
                )
                break

    # If no broader descriptor is available, leave subcategory blank
    # rather than incorrectly using the therapeutic-use name itself.
    result = {
        "mesh_id": mesh_id,
        "name": name,
        "category": category,
        "category_name": category_name,
        "sub_category_name": sub_category_name,
    }

    cache[cache_key] = result
    save_cache(
        cache,
        cache_file,
    )

    return result


def lookup_mesh_by_name(
    raw_name,
    cache,
    cache_file,
):
    """
    EXACT-ONLY MeSH resolution.

    1. Exact descriptor lookup.
    2. Exact entry-term lookup.
    3. No fuzzy matching.
    4. No semantic guessing.
    """

    raw_name = clean(raw_name)

    if not raw_name:
        return None

    cache_key = (
        "NAME_LOOKUP:"
        + normalize(raw_name)
    )

    if cache_key in cache:

        cached = cache[
            cache_key
        ]

        if cached == "__NOT_FOUND__":
            return None

        return cached

    print(
        f"      MeSH name lookup: {raw_name}"
    )

    # --------------------------------------------------------
    # Exact descriptor lookup
    # --------------------------------------------------------

    data = request_json(
        f"{MESH_LOOKUP_BASE}/descriptor",
        {
            "label": raw_name,
            "match": "exact",
            "limit": 10,
        },
    )

    candidates = (
        data
        if isinstance(data, list)
        else []
    )

    target_key = normalize(
        raw_name
    )

    for candidate in candidates:

        if not isinstance(
            candidate,
            dict,
        ):
            continue

        label = clean(
            candidate.get(
                "label",
                "",
            )
        )

        resource = clean(
            candidate.get(
                "resource",
                "",
            )
        )

        if normalize(label) != target_key:
            continue

        mesh_id = mesh_id_from_text(
            resource
        )

        if not mesh_id:
            continue

        info = resolve_mesh(
            mesh_id,
            cache,
            cache_file,
        )

        if info and info["name"]:

            result = info.copy()

            cache[
                cache_key
            ] = result

            save_cache(
                cache,
                cache_file,
            )

            return result

    # --------------------------------------------------------
    # Exact entry-term lookup
    # --------------------------------------------------------

    data = request_json(
        f"{MESH_LOOKUP_BASE}/term",
        {
            "label": raw_name,
            "match": "exact",
            "limit": 10,
        },
    )

    candidates = (
        data
        if isinstance(data, list)
        else []
    )

    for candidate in candidates:

        if not isinstance(
            candidate,
            dict,
        ):
            continue

        label = clean(
            candidate.get(
                "label",
                "",
            )
        )

        resource = clean(
            candidate.get(
                "resource",
                "",
            )
        )

        if normalize(label) != target_key:
            continue

        term_data = fetch_resource_json(
            resource,
            cache,
            cache_file,
        )

        for mesh_id in find_descriptor_ids(
            term_data
        ):

            info = resolve_mesh(
                mesh_id,
                cache,
                cache_file,
            )

            if info and info["name"]:

                result = info.copy()

                cache[
                    cache_key
                ] = result

                save_cache(
                    cache,
                    cache_file,
                )

                return result

    cache[
        cache_key
    ] = "__NOT_FOUND__"

    save_cache(
        cache,
        cache_file,
    )

    return None


# ============================================================
# BUILD
# ============================================================

def build(
    input_file,
    output_file,
    mapping_file,
    cache_file,
):
    input_file = Path(
        input_file
    )

    output_file = Path(
        output_file
    )

    mapping_file = Path(
        mapping_file
    )

    cache_file = Path(
        cache_file
    )

    if not input_file.exists():
        raise ValueError(
            f"Input mapping not found:\n"
            f"{input_file}"
        )

    df = pd.read_excel(
        input_file
    )

    df.columns = [
        str(c).strip()
        for c in df.columns
    ]

    required = [
        "plant_part_id",
        "plant_name",
        "plant_part",
        "therapeutic_use",
    ]

    missing = [
        c
        for c in required
        if c not in df.columns
    ]

    if missing:
        raise ValueError(
            "Missing required columns:\n"
            + "\n".join(
                f"  {c}"
                for c in missing
            )
        )

    if "source" not in df.columns:
        df["source"] = ""

    df = df[
        [
            "plant_part_id",
            "plant_name",
            "plant_part",
            "mesh_id",
            "therapeutic_use",
            "source",
        ]
        if "mesh_id" in df.columns
        else [
            "plant_part_id",
            "plant_name",
            "plant_part",
            "therapeutic_use",
            "source",
        ]
    ].copy()

    if "mesh_id" not in df.columns:
        df.insert(
            3,
            "mesh_id",
            "",
        )

    for column in df.columns:
        df[column] = df[column].map(
            clean
        )

    cache = load_cache(
        cache_file
    )

    project_therapeutic_cache = (
        load_project_therapeutic_cache()
    )

    imppat_name_lookup = (
        build_imppat_name_lookup(
            project_therapeutic_cache,
            df["plant_name"].tolist(),
        )
    )

    mesh_browser_lookup = (
        build_mesh_browser_lookup(
            project_therapeutic_cache
        )
    )

    resolved_rows = []
    unresolved_rows = []

    print(
        "=" * 70
    )
    print(
        "THERAPEUTIC USE MESH RESOLVER"
    )
    print(
        "=" * 70
    )

    print(
        f"Mapping rows: {len(df)}"
    )

    for index, row in df.iterrows():

        raw_use = clean(
            row["therapeutic_use"]
        )

        if not raw_use:
            continue

        mesh_id = mesh_id_from_text(
            row.get("mesh_id", "")
        )

        info = None

        # ----------------------------------------------------
        # PATH 1: MeSH ID is already in the dedicated field.
        # ----------------------------------------------------

        if mesh_id:

            info = resolve_mesh(
                mesh_id,
                cache,
                cache_file,
            )

        # ----------------------------------------------------
        # PATH 2: The collector stores the identifier bundle
        # inside therapeutic_use itself, for example:
        #
        #   MESH:D002492 , UMLS:C0007681 , ICD-11:XM6QP4
        #
        # The previous builder incorrectly sent that whole
        # identifier bundle to the plain-name MeSH lookup.
        #
        # Extract the real MeSH ID first.
        # ----------------------------------------------------

        if info is None:

            mesh_id = mesh_id_from_text(
                raw_use
            )

            if mesh_id:

                info = resolve_mesh(
                    mesh_id,
                    cache,
                    cache_file,
                )

        # ----------------------------------------------------
        # PATH 3: No MeSH identifier is available.
        #
        # Strip known identifier tokens and then perform the
        # exact descriptor / exact entry-term lookup.
        # ----------------------------------------------------

        if info is None:

            readable = raw_use

            readable = re.sub(
                r"\bMESH\s*:\s*D\d{6}\b",
                "",
                readable,
                flags=re.IGNORECASE,
            )

            readable = re.sub(
                r"\bUMLS\s*:\s*C\d{7}\b",
                "",
                readable,
                flags=re.IGNORECASE,
            )

            readable = re.sub(
                r"\bICD[-\s]*11\s*:\s*[A-Z0-9.]+\b",
                "",
                readable,
                flags=re.IGNORECASE,
            )

            readable = re.sub(
                r"\bDOID\s*:\s*\d+\b",
                "",
                readable,
                flags=re.IGNORECASE,
            )

            readable = re.sub(
                r"\bSNOMED(?:CT)?\s*:\s*\d+\b",
                "",
                readable,
                flags=re.IGNORECASE,
            )

            readable = re.sub(
                r"\s*[,;|]+\s*",
                " ",
                readable,
            )

            readable = re.sub(
                r"\s+",
                " ",
                readable,
            ).strip(" ,;:-")

            if readable:

                info = lookup_mesh_by_name(
                    readable,
                    cache,
                    cache_file,
                )

        if info is None:

            unresolved_rows.append({
                "plant_part_id":
                    clean(
                        row.get(
                            "plant_part_id",
                            "",
                        )
                    ),
                "plant_name":
                    clean(
                        row.get(
                            "plant_name",
                            "",
                        )
                    ),
                "plant_part":
                    clean(
                        row.get(
                            "plant_part",
                            "",
                        )
                    ),
                "therapeutic_use":
                    raw_use,
                "source":
                    clean(
                        row.get(
                            "source",
                            "",
                        )
                    ),
                "reason":
                    "No exact MeSH descriptor or entry-term match",
            })

            # Keep unresolved row in mapping for audit.
            resolved_rows.append({
                "plant_part_id":
                    clean(
                        row.get(
                            "plant_part_id",
                            "",
                        )
                    ),
                "plant_name":
                    clean(
                        row.get(
                            "plant_name",
                            "",
                        )
                    ),
                "plant_part":
                    clean(
                        row.get(
                            "plant_part",
                            "",
                        )
                    ),
                "mesh_id": "",
                "therapeutic_use":
                    raw_use,
                "source":
                    clean(
                        row.get(
                            "source",
                            "",
                        )
                    ),
            })

            continue

        resolved_rows.append({
            "plant_part_id":
                clean(
                    row.get(
                        "plant_part_id",
                        "",
                    )
                ),
            "plant_name":
                clean(
                    row.get(
                        "plant_name",
                        "",
                    )
                ),
            "plant_part":
                clean(
                    row.get(
                        "plant_part",
                        "",
                    )
                ),
            "mesh_id":
                clean(
                    info["mesh_id"]
                ).upper(),
            "therapeutic_use":
                imppat_name_lookup.get(
                    clean(
                        info["mesh_id"]
                    ).upper(),
                    clean(
                        info["name"]
                    ),
                ),
            "source":
                "IMPPAT",
        })

    resolved_mapping = pd.DataFrame(
        resolved_rows,
        columns=MAPPING_COLUMNS,
    )

    # De-duplicate mappings without destroying
    # distinct plant-part relationships.
    resolved_mapping = (
        resolved_mapping
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
        .reset_index(
            drop=True
        )
    )

    # --------------------------------------------------------
    # Build unique TherapeuticUse nodes
    # --------------------------------------------------------

    therapeutic_rows = []

    for mesh_id, group in (
        resolved_mapping[
            resolved_mapping["mesh_id"] != ""
        ]
        .groupby(
            "mesh_id",
            sort=True,
        )
    ):

        first = group.iloc[0]

        mesh_id = clean(
            mesh_id
        ).upper()

        # --------------------------------------------------------
        # Therapeutic name:
        # IMPPAT is the source of the therapeutic association/name.
        # --------------------------------------------------------

        therapeutic_name = (
            imppat_name_lookup.get(
                mesh_id,
                clean(
                    first["therapeutic_use"]
                ),
            )
        )

        # --------------------------------------------------------
        # Hierarchy:
        # use the same official NLM MeSH Browser semantics as the
        # project's therapeutic collector. This deliberately avoids
        # deriving category/subcategory from RDF traversal order.
        # --------------------------------------------------------

        hierarchy = scrape_mesh_metadata(
            mesh_id,
            project_therapeutic_cache,
            output_file.parent
            / "therapeutic_cache.json",
        )

        info = {
            "mesh_id": mesh_id,
            "name": clean(
                therapeutic_name
            ),
            "category": clean(
                hierarchy.get(
                    "mesh_category",
                    "",
                )
            ),
            "category_name": clean(
                hierarchy.get(
                    "mesh_category_name",
                    "",
                )
            ),
            "sub_category_name": clean(
                hierarchy.get(
                    "mesh_sub_category_name",
                    "",
                )
            ),
        }

        sources = ["IMPPAT"]

        therapeutic_rows.append({
            "mesh_id":
                clean(
                    mesh_id
                ).upper(),

            "therapeutic_use":
                clean(
                    info.get(
                        "name",
                        "",
                    )
                ),

            "mesh_category":
                clean(
                    info.get(
                        "category",
                        "",
                    )
                ),

            "mesh_category_name":
                clean(
                    info.get(
                        "category_name",
                        "",
                    )
                ),

            "mesh_sub_category_name":
                clean(
                    info.get(
                        "sub_category_name",
                        "",
                    )
                ),

            "source":
                "IMPPAT",
        })


    therapeutic_df = pd.DataFrame(
        therapeutic_rows,
        columns=FINAL_NODE_COLUMNS,
    )

    # Final defensive normalization so JSON-LD objects can never
    # leak into the Excel node file.
    for column in FINAL_NODE_COLUMNS:

        if column != "mesh_id":
            therapeutic_df[column] = (
                therapeutic_df[column].map(
                    clean
                )
            )

    therapeutic_df["mesh_id"] = (
        therapeutic_df["mesh_id"]
        .map(clean_id)
    )

    # One node per MeSH ID.
    therapeutic_df = (
        therapeutic_df
        .drop_duplicates(
            subset=["mesh_id"],
            keep="first",
        )
        .reset_index(
            drop=True
        )
    )

    # --------------------------------------------------------
    # Save outputs
    # --------------------------------------------------------

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    mapping_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    therapeutic_df.to_excel(
        output_file,
        index=False,
    )

    resolved_mapping.to_excel(
        mapping_file,
        index=False,
    )

    if unresolved_rows:

        audit_file = (
            output_file.parent
            / UNRESOLVED_FILE
        )

        pd.DataFrame(
            unresolved_rows
        ).to_excel(
            audit_file,
            index=False,
        )

        print()
        print(
            f"Unresolved audit: {audit_file}"
        )

    save_cache(
        cache,
        cache_file,
    )

    print()
    print(
        "=" * 70
    )
    print(
        "FINAL RESULT"
    )
    print(
        "=" * 70
    )

    print(
        f"Input mappings:       {len(df)}"
    )

    print(
        f"Resolved mappings:    "
        f"{(resolved_mapping['mesh_id'] != '').sum()}"
    )

    print(
        f"Unresolved mappings:  "
        f"{len(unresolved_rows)}"
    )

    print(
        f"Unique TherapeuticUse nodes: "
        f"{len(therapeutic_df)}"
    )

    print()
    print(
        f"Node output:\n"
        f"  {output_file}"
    )

    print(
        f"Mapping output:\n"
        f"  {mapping_file}"
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
            "Resolve therapeutic uses to MeSH IDs "
            "and build the final TherapeuticUse node file."
        )
    )

    parser.add_argument(
        "--file",
        required=True,
        help=(
            "Therapeutic_Use_Mapping.xlsx"
        ),
    )

    parser.add_argument(
        "--output",
        required=True,
        help=(
            "Final Therapeutic_uses.xlsx"
        ),
    )

    parser.add_argument(
        "--mapping-output",
        required=True,
        help=(
            "Resolved Therapeutic_Use_Mapping.xlsx"
        ),
    )

    parser.add_argument(
        "--cache",
        required=True,
        help=(
            "Persistent MeSH cache JSON"
        ),
    )

    args = parser.parse_args()

    build(
        args.file,
        args.output,
        args.mapping_output,
        args.cache,
    )


if __name__ == "__main__":
    main()