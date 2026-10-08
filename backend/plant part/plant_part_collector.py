import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook


# ================================================================
# AYURVEDIC PLANT PART COLLECTOR - IMPPAT ONLY
# ================================================================
# Purpose:
#   Collect ALL plant-part associations available for the requested
#   plant from IMPPAT only.
#
# Sources used:
#   * IMPPAT therapeutic-use plant page
#   * IMPPAT phytochemical-association plant page
#
# No e-CHARAK, Kew, PubMed, Europe PMC, Wikipedia, GBIF, etc.
# are used for plant-part discovery.
#
# Output:
#   plant_part_id | plant_name | plant_part | source_name
# ================================================================

HEADERS = {
    "User-Agent": (
        "AyurvedicPlantPartCollector/IMPPAT-only "
        "(academic research; respectful automated access)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

CONNECT_TIMEOUT = 12
READ_TIMEOUT = 30
REQUEST_DELAY = 0.75

CACHE = Path("cache_imppat_plant_parts")

THERAPEUTICS_URL = (
    "https://cb.imsc.res.in/imppat/therapeutics/{}"
)

PHYTOCHEMICAL_URL = (
    "https://cb.imsc.res.in/imppat/phytochemical/{}"
)

# IMPPAT uses singular forms in its tables.
PART_ALIASES = {
    "root bark": "root bark",
    "roots": "root",
    "root": "root",
    "rhizomes": "rhizome",
    "rhizome": "rhizome",
    "leaves": "leaf",
    "leaf": "leaf",
    "flowers": "flower",
    "flower": "flower",
    "fruits": "fruit",
    "fruit": "fruit",
    "seeds": "seed",
    "seed": "seed",
    "barks": "bark",
    "bark": "bark",
    "stems": "stem",
    "stem": "stem",
    "wood": "wood",
    "woods": "wood",
    "bulbs": "bulb",
    "bulb": "bulb",
    "tubers": "tuber",
    "tuber": "tuber",
    "stigmas": "stigma",
    "stigma": "stigma",
    "stamens": "stamen",
    "stamen": "stamen",
    "husk": "husk",
    "husks": "husk",
    "gum": "gum",
    "gums": "gum",
    "resin": "resin",
    "resins": "resin",
    "latex": "latex",
    "oil": "oil",
    "oils": "oil",
    "whole plant": "whole plant",
    "whole herb": "whole plant",
    "whole plant parts": "whole plant",
    "aerial part": "aerial parts",
    "aerial parts": "aerial parts",
    "plant exudate": "plant exudate",
    "plant exudates": "plant exudate",
}

PART_CODES = {
    "root": "RO",
    "root bark": "RB",
    "rhizome": "RH",
    "leaf": "LE",
    "flower": "FL",
    "fruit": "FR",
    "seed": "SE",
    "bark": "BA",
    "stem": "ST",
    "wood": "WO",
    "bulb": "BU",
    "tuber": "TU",
    "stigma": "SG",
    "stamen": "SM",
    "husk": "HU",
    "gum": "GU",
    "resin": "RE",
    "latex": "LX",
    "oil": "OI",
    "whole plant": "WH",
    "aerial parts": "AE",
    "plant exudate": "PL",
}


def clean(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\xa0", " ").split())


def canonical_plant_name(name):
    """
    Preserve the supplied scientific name for output, but normalize
    whitespace/case only for comparisons.
    """
    return clean(name)


def comparison_name(name):
    return re.sub(r"\s+", " ", clean(name)).strip().casefold()


def safe_name(name):
    return re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        canonical_plant_name(name)
    )[:180]


def read_plants(path):
    p = Path(path)

    if not p.exists():
        raise FileNotFoundError(str(p))

    plants = []

    with p.open(encoding="utf-8-sig") as f:
        for line in f:
            line = clean(line)

            if not line or line.startswith("#"):
                continue

            # Also tolerate:
            # 1. Acacia leucophloea
            # 2) Acacia leucophloea
            line = re.sub(
                r"^\s*\d+[\).\s-]+",
                "",
                line
            )

            if line:
                plants.append(line)

    return list(dict.fromkeys(plants))


def ensure_cache():
    CACHE.mkdir(parents=True, exist_ok=True)


def cache_path(plant, source):
    ensure_cache()
    return CACHE / f"{safe_name(plant)}__{source}.json"


def load_cache(path):
    try:
        if path.exists():
            return json.loads(
                path.read_text(encoding="utf-8")
            )
    except Exception:
        pass

    return None


def save_cache(path, data):
    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


class IMPPATClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.last_request = 0.0

    def get(self, url):
        wait = REQUEST_DELAY - (
            time.monotonic() - self.last_request
        )

        if wait > 0:
            time.sleep(wait)

        response = self.session.get(
            url,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        )

        self.last_request = time.monotonic()

        return response


def normalize_part(value):
    text = clean(value).casefold()

    if not text:
        return ""

    # Exact IMPPAT part first.
    if text in PART_ALIASES:
        return PART_ALIASES[text]

    # Handle harmless surrounding punctuation.
    text = re.sub(r"^[\s\-–—:;,]+|[\s\-–—:;,]+$", "", text)

    if text in PART_ALIASES:
        return PART_ALIASES[text]

    return ""


def header_index(headers, candidates):
    normalized = [
        clean(x).casefold()
        for x in headers
    ]

    for candidate in candidates:
        candidate = candidate.casefold()

        for index, value in enumerate(normalized):
            if value == candidate:
                return index

    for candidate in candidates:
        candidate = candidate.casefold()

        for index, value in enumerate(normalized):
            if candidate in value:
                return index

    return None


def extract_table_rows(html, requested_plant):
    """
    Extract ALL rows physically present in the returned IMPPAT HTML.

    IMPPAT's tables use client-side pagination on pages such as the
    plant therapeutic/phytochemical detail pages. The complete table
    rows are present in the HTML; the browser pagination controls only
    change which rows are displayed.

    We therefore deliberately parse the complete HTML table instead of
    scraping only the first visible page.
    """
    soup = BeautifulSoup(html, "html.parser")

    requested_cmp = comparison_name(requested_plant)

    tables = soup.find_all("table")

    results = []
    table_stats = []

    for table_number, table in enumerate(tables, start=1):
        rows = table.find_all("tr")

        if not rows:
            continue

        parsed = []

        for tr in rows:
            cells = tr.find_all(["th", "td"])

            if not cells:
                continue

            parsed.append([
                clean(cell.get_text(" ", strip=True))
                for cell in cells
            ])

        if len(parsed) < 2:
            continue

        # Locate a header row containing "Plant part".
        header_row_index = None
        plant_index = None
        part_index = None

        for idx, row in enumerate(parsed[:5]):
            pi = header_index(
                row,
                [
                    "Indian medicinal plant",
                    "Plant",
                    "Plant name",
                ]
            )

            pti = header_index(
                row,
                [
                    "Plant part",
                    "Plant Part",
                ]
            )

            if pti is not None:
                header_row_index = idx
                plant_index = pi
                part_index = pti
                break

        if header_row_index is None or part_index is None:
            continue

        # If the plant column is not explicitly available, the detail
        # page itself is plant-specific. Accept rows using the page
        # context.
        for row in parsed[header_row_index + 1:]:
            if part_index >= len(row):
                continue

            part = normalize_part(row[part_index])

            if not part:
                continue

            if plant_index is not None and plant_index < len(row):
                row_plant = comparison_name(row[plant_index])

                if row_plant and row_plant != requested_cmp:
                    continue

            results.append(part)

        table_stats.append({
            "table_number": table_number,
            "rows_seen": len(parsed) - header_row_index - 1,
        })

    return sorted(set(results)), table_stats


def fetch_imppat_page(client, plant, source, url_template):
    path = cache_path(plant, source)
    cached = load_cache(path)

    if cached is not None:
        return cached

    url = url_template.format(
        quote(
            plant,
            safe=""
        )
    )

    print(f"    [IMPPAT] GET {url}")

    try:
        response = client.get(url)
    except requests.RequestException as exc:
        result = {
            "status": "request_error",
            "url": url,
            "error": str(exc),
            "parts": [],
        }
        save_cache(path, result)
        return result

    if response.status_code != 200:
        result = {
            "status": f"http_{response.status_code}",
            "url": response.url,
            "parts": [],
        }
        save_cache(path, result)
        return result

    parts, table_stats = extract_table_rows(
        response.text,
        plant
    )

    result = {
        "status": "found" if parts else "no_parts",
        "url": response.url,
        "parts": parts,
        "table_stats": table_stats,
        "bytes": len(response.text),
    }

    save_cache(path, result)

    return result


def collect_imppat_parts(client, plant):
    """
    Union of plant parts found in BOTH authoritative IMPPAT
    plant-association pages:

      1. Plant -> Therapeutic use associations
      2. Plant -> Phytochemical associations

    This prevents a part from being missed merely because it appears
    in one IMPPAT association type but not the other.
    """
    therapeutic = fetch_imppat_page(
        client,
        plant,
        "therapeutics",
        THERAPEUTICS_URL
    )

    phytochemical = fetch_imppat_page(
        client,
        plant,
        "phytochemical",
        PHYTOCHEMICAL_URL
    )

    all_parts = set(therapeutic.get("parts", []))
    all_parts.update(
        phytochemical.get("parts", [])
    )

    return {
        "parts": sorted(all_parts),
        "therapeutics": therapeutic,
        "phytochemical": phytochemical,
    }


def plant_part_id(plant, part):
    """
    Existing ID convention preserved, extended only for IMPPAT-specific
    parts such as plant exudate.
    """
    words = re.findall(
        r"[A-Za-z]+",
        plant.upper()
    )

    if len(words) >= 2:
        prefix = (
            words[0][:2]
            + "-"
            + words[1][:2]
        )
    elif words:
        prefix = words[0][:4]
    else:
        prefix = "PL"

    code = PART_CODES.get(
        part,
        "XX"
    )

    return f"{prefix}-{code}"


def load_existing_rows(output):
    path = Path(output)

    if not path.exists():
        return []

    try:
        wb = load_workbook(
            path,
            read_only=True,
            data_only=True
        )

        ws = (
            wb["PlantPart"]
            if "PlantPart" in wb.sheetnames
            else wb.active
        )

        rows = []

        for row in ws.iter_rows(
            min_row=2,
            values_only=True
        ):
            values = list(row) + [
                "",
                "",
                "",
                "",
            ]

            if not any(
                x is not None
                for x in values
            ):
                continue

            rows.append([
                values[0] or "",
                values[1] or "",
                values[2] or "",
                values[3] or "",
            ])

        wb.close()

        return rows

    except Exception:
        return []


def merge_rows(old_rows, new_rows):
    """
    Preserve the existing collector's merge behavior but ensure that
    this IMPPAT-only collector never introduces non-IMPPAT sources.
    """
    data = {}

    for row in old_rows + new_rows:
        plant = clean(row[1])
        part = normalize_part(row[2])

        if not plant or not part:
            continue

        key = (
            comparison_name(plant),
            comparison_name(part),
        )

        if key not in data:
            data[key] = [
                plant_part_id(
                    plant,
                    part
                ),
                plant,
                part,
                "IMPPAT",
            ]

    return sorted(
        data.values(),
        key=lambda r: (
            comparison_name(r[1]),
            comparison_name(r[2]),
        )
    )


def write_xlsx(rows, output):
    wb = Workbook()

    ws = wb.active
    ws.title = "PlantPart"

    ws.append([
        "plant_part_id",
        "plant_name",
        "plant_part",
        "source_name",
    ])

    for row in rows:
        ws.append(row)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    widths = {
        "A": 22,
        "B": 38,
        "C": 24,
        "D": 20,
    }

    for column, width in widths.items():
        ws.column_dimensions[column].width = width

    wb.save(output)


def write_audit(audit):
    path = Path(
        "PlantPart_IMPPAT_audit.xlsx"
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "IMPPAT_Audit"

    ws.append([
        "plant_name",
        "source_page",
        "status",
        "parts_found",
        "url",
    ])

    for row in audit:
        ws.append(row)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    for column, width in zip(
        "ABCDE",
        [38, 24, 20, 60, 100]
    ):
        ws.column_dimensions[column].width = width

    wb.save(path)


def run(input_file, output):
    ensure_cache()

    plants = read_plants(input_file)
    client = IMPPATClient()

    print("=" * 72)
    print("        AYURVEDIC PLANT PART COLLECTOR")
    print("                     IMPPAT ONLY")
    print("=" * 72)
    print(f"Plants found: {len(plants)}")
    print()
    print("Source:")
    print("  IMPPAT therapeutic-use associations")
    print("  IMPPAT phytochemical associations")
    print()
    print(
        "Pagination handling:"
        " complete IMPPAT HTML table is parsed;"
    )
    print(
        "all rows behind the visible DataTables pages are included."
    )
    print()

    output_rows = []
    audit = []

    for number, plant in enumerate(
        plants,
        start=1
    ):
        print(
            f"[{number}/{len(plants)}] "
            f"{plant}"
        )

        result = collect_imppat_parts(
            client,
            plant
        )

        parts = result["parts"]

        therapeutic = result["therapeutics"]
        phytochemical = result["phytochemical"]

        therapeutic_parts = set(
            therapeutic.get("parts", [])
        )

        phytochemical_parts = set(
            phytochemical.get("parts", [])
        )

        print(
            "  IMPPAT therapeutic parts:",
            ", ".join(
                sorted(therapeutic_parts)
            )
            if therapeutic_parts
            else "none"
        )

        print(
            "  IMPPAT phytochemical parts:",
            ", ".join(
                sorted(phytochemical_parts)
            )
            if phytochemical_parts
            else "none"
        )

        print(
            "  IMPPAT UNION:",
            ", ".join(parts)
            if parts
            else "NONE"
        )

        for part in parts:
            row = [
                plant_part_id(
                    plant,
                    part
                ),
                plant,
                part,
                "IMPPAT",
            ]

            output_rows.append(row)

            print(
                "   ->",
                " | ".join(row)
            )

        audit.append([
            plant,
            "therapeutics",
            therapeutic.get("status", ""),
            ", ".join(
                sorted(therapeutic_parts)
            ),
            therapeutic.get("url", ""),
        ])

        audit.append([
            plant,
            "phytochemical",
            phytochemical.get("status", ""),
            ", ".join(
                sorted(phytochemical_parts)
            ),
            phytochemical.get("url", ""),
        ])

        print()

    final_rows = merge_rows(
        load_existing_rows(output),
        output_rows
    )

    write_xlsx(
        final_rows,
        output
    )

    write_audit(audit)

    print("=" * 72)
    print("DONE")
    print("=" * 72)
    print(
        "PlantPart:",
        Path(output).resolve()
    )
    print(
        "IMPPAT audit:",
        Path(
            "PlantPart_IMPPAT_audit.xlsx"
        ).resolve()
    )
    print(
        "Rows:",
        len(final_rows)
    )
    print(
        "Source:",
        "IMPPAT ONLY"
    )
    print()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Collect plant parts from IMPPAT only"
        )
    )

    parser.add_argument(
        "--file",
        required=True,
        help=(
            "TXT file containing one plant "
            "name per line"
        )
    )

    parser.add_argument(
        "--output",
        default="PlantPart.xlsx",
        help="Output Excel filename"
    )

    args = parser.parse_args()

    try:
        run(
            args.file,
            args.output
        )

    except KeyboardInterrupt:
        print(
            "\nStopped by user."
        )
        sys.exit(130)

    except FileNotFoundError as exc:
        print()
        print(
            "ERROR: input file not found:"
        )
        print(" ", exc)
        print()
        print(
            "Current directory:"
        )
        print(" ", Path.cwd())
        sys.exit(1)

    except Exception as exc:
        print()
        print("=" * 72)
        print("FATAL")
        print("=" * 72)
        print(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()