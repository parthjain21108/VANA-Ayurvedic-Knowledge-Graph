import os
import re
import time
import requests
from xml.etree import ElementTree as ET
from urllib.parse import urljoin, quote_plus

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


# ============================================================
# API ENDPOINTS
# ============================================================

NCBI_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

GBIF_MATCH = "https://api.gbif.org/v1/species/match"

GBIF_OCCURRENCE_SEARCH = (
    "https://api.gbif.org/v1/occurrence/search"
)

IISc_KARNATAKA_SPECIES = (
    "https://indiaflora-ces.iisc.ac.in/plants.php"
)

IISc_KARNATAKA_BASE = (
    "https://indiaflora-ces.iisc.ac.in/"
)

IUCN_BASE = (
    "https://api.iucnredlist.org/api/v4"
)


def clean(value):
    """
    Normalize text safely for top-level collector logic.

    The IUCN class already has its own local clean() helper; this
    top-level helper is required by collect_plant() for common-name
    normalization.
    """
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    value = str(value)
    value = value.replace("\r", " ")
    value = value.replace("\n", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


# ============================================================
# CONFIGURATION
# ============================================================

HEADERS = {
    "User-Agent": (
        "AyurvedicKnowledgeGraph/1.0 "
        "(research data collection)"
    )
}


# ------------------------------------------------------------
# IUCN API KEY
#
# PUT YOUR REAL IUCN API TOKEN BETWEEN THE QUOTES.
#
# Example:
#
# IUCN_API_TOKEN = "your_real_token_here"
#
# ------------------------------------------------------------

IUCN_API_TOKEN = "6hKJBSGZVtroWfFBBY327GKnvQxFxdMVNVNA"


class PlantCollector:

    def __init__(self):

        # ----------------------------------------------------
        # NCBI configuration
        # ----------------------------------------------------

        self.email = os.getenv(
            "NCBI_EMAIL",
            ""
        )

        self.tool = os.getenv(
            "NCBI_TOOL",
            "AyurvedicKnowledgeGraphPlantCollector"
        )

        self.ncbi_api_key = os.getenv(
            "NCBI_API_KEY",
            ""
        )

        # ----------------------------------------------------
        # IUCN configuration
        # ----------------------------------------------------
        #
        # Direct Python configuration.
        # ----------------------------------------------------

        self.iucn_token = (
            IUCN_API_TOKEN
            if IUCN_API_TOKEN
            and IUCN_API_TOKEN
            != "PUT_YOUR_IUCN_API_KEY_HERE"
            else ""
        )

        # ----------------------------------------------------
        # HTTP session
        # ----------------------------------------------------

        self.session = requests.Session()

        self.session.headers.update(
            HEADERS
        )


    # ========================================================
    # MAIN COLLECTION PIPELINE
    # ========================================================

    def collect(
        self,
        input_name
    ):

        # ----------------------------------------------------
        # Preserve original input name.
        #
        # This becomes scientific_name in the final schema.
        # ----------------------------------------------------

        input_name = " ".join(
            str(input_name).split()
        )

        if not input_name:
            raise ValueError(
                "Plant name cannot be empty."
            )

        # ----------------------------------------------------
        # 1. GBIF species matching
        # ----------------------------------------------------

        gbif = self.gbif_match(
            input_name
        )

        accepted_name = (
            gbif.get("species")
            or gbif.get("canonicalName")
            or input_name
        )

        # ----------------------------------------------------
        # 2. NCBI taxonomy
        # ----------------------------------------------------

        ncbi = (
            self.ncbi_taxonomy(
                accepted_name
            )
            or
            self.ncbi_taxonomy(
                input_name
            )
        )

        taxonomy_id = None

        if ncbi:

            taxonomy_id = ncbi.get(
                "taxonomy_id"
            )

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # scientific_name is ALWAYS the original input name.
        # The GBIF accepted name is used only internally for
        # biological lookups and is NOT exported as a field.
        # ----------------------------------------------------

        scientific_name = input_name

        # ----------------------------------------------------
        # 3. IUCN global conservation information
        #
        # Use the resolved name for the biological lookup.
        # Global status and year come from the same IUCN
        # assessment.
        # ----------------------------------------------------

        iucn = self.iucn_lookup(
            accepted_name,
            gbif
        )

        # ----------------------------------------------------
        # 4. Karnataka regional conservation information
        #
        # Do NOT use GBIF occurrence count as conservation
        # status. The regional status comes from the IISc
        # Digital Flora of Karnataka record.
        # ----------------------------------------------------

        # IISc may retain the original name even when GBIF resolves
        # the plant to a newer accepted name. Try the exact input name
        # first, then the resolved name as a fallback.
        karnataka = self.karnataka_status(
            input_name
        )

        if not karnataka.get("found"):
            karnataka = self.karnataka_status(
                accepted_name
            )

        regional_status = karnataka.get(
            "status",
            "Not Found"
        )

        # ----------------------------------------------------
        # 5. Common names
        #
        # Prefer the IISc Karnataka flora common name.
        # Fall back to NCBI taxonomy common names.
        # ----------------------------------------------------

        # ----------------------------------------------------
        # English common names ONLY
        #
        # IISc's "Common name" field is the English common-name
        # field. Its separate "Vernacular name" field is NOT used.
        #
        # NCBI is retained only as fallback when IISc has no
        # English common name.
        # ----------------------------------------------------

        common_name_values = []

        iisc_common = clean(
            karnataka.get(
                "common_names",
                "",
            )
        )

        for part in re.split(
            r"\s*;\s*",
            iisc_common,
        ):
            part = " ".join(
                part.strip().split()
            )

            if (
                part
                and part.lower()
                not in {
                    x.lower()
                    for x in common_name_values
                }
            ):
                common_name_values.append(
                    part
                )

        # NCBI fallback only when IISc has no common name.
        if not common_name_values and ncbi:

            ncbi_common = clean(
                ncbi.get(
                    "common_names",
                    "",
                )
            )

            for part in re.split(
                r"\s*;\s*",
                ncbi_common,
            ):
                part = " ".join(
                    part.strip().split()
                )

                if (
                    part
                    and part.lower()
                    not in {
                        x.lower()
                        for x in common_name_values
                    }
                ):
                    common_name_values.append(
                        part
                    )

        # Final graph/database format: comma-separated.
        common_names = ", ".join(
            common_name_values
        )

        # ----------------------------------------------------
        # 6. Source tracking
        # ----------------------------------------------------

        sources = []

        if gbif:
            sources.append(
                "GBIF"
            )

        if ncbi:
            sources.append(
                "NCBI Taxonomy"
            )

        if iucn.get("found"):
            sources.append(
                "IUCN Red List"
            )

        if (
            karnataka.get("found")
            or karnataka.get("common_names")
        ):
            sources.append(
                "IISc Digital Flora of Karnataka"
            )

        # ----------------------------------------------------
        # 7. Create final Plant record
        # ----------------------------------------------------

        return {

            "scientific_name": scientific_name,

            "taxonomy_id": (
                taxonomy_id
                if taxonomy_id
                else "Not Found"
            ),

            "global_status": self.normalize_status_code(
                iucn.get(
                    "status",
                    "Not Found"
                )
            ),

            "regional_status": self.normalize_status_code(
                regional_status
            ),

            "iucn_year": iucn.get(
                "year",
                "Not Found"
            ),

            "source": "; ".join(
                sources
            ),

            "common_names": common_names
                or "Not Found",
        }


    # ========================================================
    # ERROR ROW
    # ========================================================

    def error_row(
        self,
        input_name,
        error
    ):

        return {

            "scientific_name": input_name,

            "taxonomy_id": "ERROR",

            "global_status": "ERROR",

            "regional_status": "ERROR",

            "iucn_year": "ERROR",

            "source": (
                f"Collector error: {error}"
            ),

            "common_names": "ERROR",
        }


    # ========================================================
    # GBIF SPECIES MATCH
    # ========================================================

    def gbif_match(
        self,
        name
    ):

        response = self.session.get(
            GBIF_MATCH,
            params={
                "scientificName": name
            },
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        if data.get(
            "matchType"
        ) == "NONE":

            return {}

        return data


    # ========================================================
    # NCBI TAXONOMY
    # ========================================================

    def ncbi_taxonomy(
        self,
        name
    ):

        params = {

            "db": "taxonomy",

            "term": (
                f'"{name}"'
                "[Scientific Name]"
            ),

            "retmode": "json",

            "tool": self.tool,

            "email": self.email,
        }

        if self.ncbi_api_key:

            params[
                "api_key"
            ] = self.ncbi_api_key

        # ----------------------------------------------------
        # Search taxonomy database
        # ----------------------------------------------------

        response = self.session.get(
            f"{NCBI_EUTILS}/esearch.fcgi",
            params=params,
            timeout=30
        )

        response.raise_for_status()

        ids = (
            response
            .json()
            .get(
                "esearchresult",
                {}
            )
            .get(
                "idlist",
                []
            )
        )

        if not ids:
            return None

        # ----------------------------------------------------
        # Fetch taxonomy record
        # ----------------------------------------------------

        params = {

            "db": "taxonomy",

            "id": ids[0],

            "retmode": "xml",

            "tool": self.tool,

            "email": self.email,
        }

        if self.ncbi_api_key:

            params[
                "api_key"
            ] = self.ncbi_api_key

        # Avoid excessive requests.
        time.sleep(0.34)

        response = self.session.get(
            f"{NCBI_EUTILS}/efetch.fcgi",
            params=params,
            timeout=30
        )

        response.raise_for_status()

        root = ET.fromstring(
            response.text
        )

        taxonomy_id = root.findtext(
            ".//TaxId"
        )

        scientific_name = (
            root.findtext(
                ".//ScientificName"
            )
        )

        if not taxonomy_id:
            return None

        # ----------------------------------------------------
        # NCBI common names
        # ----------------------------------------------------

        common_names = []

        for element in root.findall(
            ".//OtherNames/CommonName"
        ):

            value = (
                element.text
                or ""
            ).strip()

            if value and value not in common_names:
                common_names.append(value)

        return {

            "taxonomy_id":
                taxonomy_id,

            "scientific_name":
                scientific_name,

            "common_names":
                "; ".join(common_names),
        }


    # ========================================================
    # KARNATAKA / INDIA FLORA LOOKUP
    # ========================================================

    def karnataka_status(
        self,
        plant_name
    ):
        # ----------------------------------------------------
        # IISc India Flora / Flora of Karnataka lookup.
        #
        # The IISc site can expose a species record through
        # plants.php and then redirect/link to a herbsheet.php
        # record. Do not assume a fixed herbsheet ID.
        #
        # We therefore:
        #   1. try the public permalink and Karnataka endpoint
        #   2. follow normal HTTP redirects
        #   3. inspect the returned HTML for a herbsheet URL
        #   4. scrape the actual species page using visible text
        #
        # This is deliberately isolated to regional/common-name
        # collection. No other collector logic is changed.
        # ----------------------------------------------------

        result = {
            "found": False,
            "status": "Not Found",
            "common_names": "",
        }

        if BeautifulSoup is None:
            print(
                "    IISc Flora: BeautifulSoup4 is required. "
                "Install with: pip install beautifulsoup4"
            )
            return result

        candidate = " ".join(
            str(plant_name).strip().split()
        )

        if not candidate:
            return result

        urls = [
            (
                IISc_KARNATAKA_SPECIES,
                {"name": candidate},
            ),
            (
                "https://indiaflora-ces.iisc.ac.in/FloraKarnataka/plants.php",
                {"name": candidate},
            ),
        ]

        visited = set()

        def clean(value):
            return " ".join(
                str(value)
                .replace("\xa0", " ")
                .split()
            ).strip(" ;,")

        def normalize_text(value):
            return clean(value).lower()

        def add_unique(values, value):
            value = clean(value)
            if not value:
                return
            if value.lower() in {x.lower() for x in values}:
                return
            values.append(value)

        def extract_record(page, source_url):
            soup = BeautifulSoup(
                page,
                "html.parser"
            )

            # Remove script/style noise before text extraction.
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            text = clean(
                soup.get_text(" ", strip=True)
            )

            target = self.normalize_species_name(candidate)

            # The record must actually contain the requested botanical
            # binomial. Do not accept a generic IISc search page.
            if target not in normalize_text(text):
                return None

            names = []

            # ------------------------------------------------
            # Common name
            # ------------------------------------------------
            common_match = re.search(
                r"\bCommon\s+name\s*:\s*(.+?)(?=\s+Vernacular\s+name\s*:|\s+Habit\s*:|\s+Habitat\s*:|\s+Comments\s*/?\s*notes\s*:|\s+Conservation\s+Status\s*:|$)",
                text,
                flags=re.IGNORECASE,
            )

            if common_match:
                add_unique(
                    names,
                    common_match.group(1)
                )

            # ------------------------------------------------
            # Vernacular names are intentionally NOT included.
            #
            # The Plant schema requires English common names only.
            # ------------------------------------------------

            # ------------------------------------------------
            # Conservation status
            # ------------------------------------------------
            status = "Not Found"

            status_match = re.search(
                r"\bConservation\s+Status\s*:\s*(.+?)(?=\s+Literature\s*:|\s+Read\s+more\s*:|\s+Permalink\s*:|\s+Citation\s*:|$)",
                text,
                flags=re.IGNORECASE,
            )

            if status_match:
                status = self.normalize_status_code(
                    status_match.group(1)
                )

            # We found a real species record. Even if one field is absent,
            # return the other fields rather than treating the page as not found.
            return {
                "found": True,
                "status": status,
                "common_names": "; ".join(names),
                "url": source_url,
            }

        def inspect_for_species_links(html, base_url):
            soup = BeautifulSoup(
                html,
                "html.parser"
            )

            candidates = []

            # Direct herbsheet links are the most reliable route from
            # an IISc search/permalink page.
            for tag in soup.find_all("a", href=True):
                href = tag.get("href", "")
                if "herbsheet.php" in href.lower():
                    candidates.append(
                        urljoin(base_url, href)
                    )

            # Also handle meta-refresh and simple JavaScript redirects.
            for tag in soup.find_all("meta"):
                content = tag.get("content", "") or ""
                match = re.search(
                    r"url\s*=\s*([^;]+)",
                    content,
                    flags=re.IGNORECASE,
                )
                if match and "herbsheet.php" in match.group(1).lower():
                    candidates.append(
                        urljoin(base_url, match.group(1).strip(" '\""))
                    )

            for script in soup.find_all("script"):
                script_text = script.get_text(" ", strip=True)
                for match in re.findall(
                    r"(?:location(?:\.href)?|window\.location(?:\.href)?)\s*=\s*['\"]([^'\"]+herbsheet\.php[^'\"]*)['\"]",
                    script_text,
                    flags=re.IGNORECASE,
                ):
                    candidates.append(
                        urljoin(base_url, match)
                    )

            # De-duplicate while preserving order.
            unique = []
            seen = set()
            for url in candidates:
                if url not in seen:
                    seen.add(url)
                    unique.append(url)
            return unique

        for start_url, params in urls:
            try:
                response = self.session.get(
                    start_url,
                    params=params,
                    timeout=30,
                    allow_redirects=True,
                )
                response.raise_for_status()

                final_url = response.url
                visited.add(final_url)

                # First try the returned page itself.
                record = extract_record(
                    response.text,
                    final_url,
                )

                if record:
                    result.update(record)
                    return result

                # Otherwise find the actual species herbsheet page.
                linked_pages = inspect_for_species_links(
                    response.text,
                    final_url,
                )

                for record_url in linked_pages:
                    if record_url in visited:
                        continue

                    visited.add(record_url)

                    record_response = self.session.get(
                        record_url,
                        timeout=30,
                        allow_redirects=True,
                    )
                    record_response.raise_for_status()

                    record = extract_record(
                        record_response.text,
                        record_response.url,
                    )

                    if record:
                        result.update(record)
                        return result

            except requests.RequestException as exc:
                print(
                    f"    IISc Flora request error for '{candidate}': {exc}"
                )

            except Exception as exc:
                print(
                    f"    IISc Flora parsing error for '{candidate}': {exc}"
                )

        return result

    @staticmethod
    def normalize_species_name(
        name
    ):

        # ----------------------------------------------------
        # Compare the botanical species name while ignoring
        # author citations and other page text.
        # The input itself remains untouched in scientific_name.
        # ----------------------------------------------------

        value = " ".join(
            str(name)
            .strip()
            .split()
        )

        # Remove parenthesized author citations.
        value = re.sub(
            r"\([^)]*\)",
            " ",
            value
        )

        # Stop at common author-citation separators.
        value = re.split(
            r"\s+(?:Willd\.|Roxb\.|L\.|DC\.)",
            value,
            maxsplit=1,
            flags=re.IGNORECASE
        )[0]

        tokens = value.split()

        if len(tokens) >= 2:

            # Preserve subspecies/variety when explicitly
            # present in the input/index.
            if len(tokens) >= 4 and tokens[2].lower() in {
                "subsp.",
                "subspecies",
                "var.",
                "variety",
                "f.",
                "forma",
            }:

                return " ".join(
                    tokens[:4]
                ).lower()

            return " ".join(
                tokens[:2]
            ).lower()

        return value.lower()


    @staticmethod
    def normalize_status_code(
        status
    ):
        """
        Normalize IUCN/conservation status input to the full
        human-readable status label.

        Examples:
            LC -> Least Concern
            DD -> Data Deficient
            NE -> Not Evaluated
        """

        value = " ".join(
            str(status)
            .strip()
            .split()
        )

        # Prefer an explicit two-letter code in parentheses.
        match = re.search(
            r"\(([A-Z]{2})\)",
            value,
            flags=re.IGNORECASE,
        )

        if match:
            value = match.group(1).upper()

        # Handle both status codes and already-expanded labels.
        code_mapping = {
            "LC": "Least Concern",
            "NT": "Near Threatened",
            "VU": "Vulnerable",
            "EN": "Endangered",
            "CR": "Critically Endangered",
            "EW": "Extinct in the Wild",
            "EX": "Extinct",
            "DD": "Data Deficient",
            "NE": "Not Evaluated",
            "RE": "Regionally Extinct",
            "NA": "Not Applicable",
        }

        label_mapping = {
            "least concern": "Least Concern",
            "near threatened": "Near Threatened",
            "vulnerable": "Vulnerable",
            "endangered": "Endangered",
            "critically endangered": "Critically Endangered",
            "extinct in the wild": "Extinct in the Wild",
            "extinct": "Extinct",
            "data deficient": "Data Deficient",
            "not evaluated": "Not Evaluated",
            "not assessed": "Not Evaluated",
            "regionally extinct": "Regionally Extinct",
            "not applicable": "Not Applicable",
        }

        code = value.upper()

        if code in code_mapping:
            return code_mapping[code]

        return label_mapping.get(
            value.lower(),
            value
        )


    # ========================================================
    # IUCN RED LIST API V4
    # ========================================================
    #
    # IMPORTANT:
    #
    # IUCN API v4 does NOT use the old:
    #
    #     /species?scientific_name=...
    #
    # lookup.
    #
    # v4 uses:
    #
    #     1. /taxa/scientific_name
    #        genus_name + species_name
    #
    #     2. /assessment/{assessment_id}
    #
    # This fixes the 404 you were getting.
    #
    # ========================================================

    def iucn_lookup(
        self,
        plant_name,
        gbif
    ):

        # ----------------------------------------------------
        # No API token
        # ----------------------------------------------------

        if not self.iucn_token:

            return {
                "found": False,
                "status": "Not Configured",
                "year": "Not Configured",
            }

        # ----------------------------------------------------
        # Split scientific name.
        #
        # IUCN scientific_name endpoint expects genus_name
        # and species_name separately.
        #
        # Example:
        #
        # Vachellia leucophloea
        #
        # genus_name   = Vachellia
        # species_name = leucophloea
        # ----------------------------------------------------

        parts = (
            str(plant_name)
            .strip()
            .split()
        )

        if len(parts) < 2:

            return {
                "found": False,
                "status": "Not Found",
                "year": "Not Found",
            }

        genus_name = parts[0]

        species_name = parts[1]

        # ----------------------------------------------------
        # Build authorization header
        # ----------------------------------------------------

        headers = {
            "Authorization":
                f"Bearer {self.iucn_token}"
        }

        try:

            # ------------------------------------------------
            # STEP 1
            #
            # Find IUCN taxon/assessment records by name.
            # ------------------------------------------------

            response = self.session.get(

                f"{IUCN_BASE}/taxa/scientific_name",

                params={

                    "genus_name":
                        genus_name,

                    "species_name":
                        species_name,
                },

                headers=headers,

                timeout=30
            )

            # ------------------------------------------------
            # Handle authentication
            # ------------------------------------------------

            if response.status_code in (
                401,
                403
            ):

                print(
                    "    IUCN: authentication failed "
                    f"(HTTP {response.status_code})"
                )

                return {
                    "found": False,
                    "status": "Authentication Error",
                    "year": "Not Found",
                }

            # ------------------------------------------------
            # Not found
            # ------------------------------------------------

            if response.status_code == 404:

                print(
                    "    IUCN: species not found"
                )

                return {
                    "found": False,
                    "status": "Not Found",
                    "year": "Not Found",
                }

            # ------------------------------------------------
            # Rate limited
            # ------------------------------------------------

            if response.status_code == 429:

                print(
                    "    IUCN: rate limited"
                )

                return {
                    "found": False,
                    "status": "Rate Limited",
                    "year": "Not Found",
                }

            response.raise_for_status()

            data = response.json()

            # ------------------------------------------------
            # Normalize possible response structures.
            # ------------------------------------------------

            if isinstance(
                data,
                list
            ):

                records = data

            elif isinstance(
                data,
                dict
            ):

                records = (
                    data.get(
                        "data",
                        []
                    )
                )

                if not records:

                    records = (
                        data.get(
                            "assessments",
                            []
                        )
                    )

            else:

                records = []

            if not records:

                return {
                    "found": False,
                    "status": "NE",
                    "year": "Not Found",
                }

            # ------------------------------------------------
            # STEP 2
            #
            # Find the latest assessment.
            # ------------------------------------------------

            assessment_id = None

            selected_record = None

            # Prefer latest assessment.
            for record in records:

                if not isinstance(
                    record,
                    dict
                ):
                    continue

                if (
                    record.get(
                        "latest"
                    ) is True
                ):

                    selected_record = record

                    break

            # If latest wasn't explicitly marked,
            # use the first returned assessment.
            if selected_record is None:

                selected_record = records[0]

            # ------------------------------------------------
            # Extract assessment ID.
            # ------------------------------------------------

            if isinstance(
                selected_record,
                dict
            ):

                assessment_id = (
                    selected_record.get(
                        "assessment_id"
                    )
                    or
                    selected_record.get(
                        "id"
                    )
                )

            # ------------------------------------------------
            # If the API response itself already contains
            # enough information, use it.
            # ------------------------------------------------

            if not assessment_id:

                status = (
                    selected_record.get(
                        "red_list_category_code"
                    )
                    or
                    selected_record.get(
                        "category"
                    )
                    or
                    selected_record.get(
                        "red_list_category"
                    )
                    or
                    "Not Found"
                )

                year = (
                    selected_record.get(
                        "year_published"
                    )
                    or
                    selected_record.get(
                        "assessment_year"
                    )
                    or
                    selected_record.get(
                        "year"
                    )
                    or
                    "Not Found"
                )

                return {
                    "found": True,
                    "status": self.normalize_status_code(status),
                    "year": year,
                }

            # ------------------------------------------------
            # STEP 3
            #
            # Retrieve detailed assessment.
            # ------------------------------------------------

            assessment_response = (
                self.session.get(

                    f"{IUCN_BASE}/assessment/"
                    f"{assessment_id}",

                    headers=headers,

                    timeout=30
                )
            )

            if assessment_response.status_code == 429:

                print(
                    "    IUCN: assessment request "
                    "rate limited"
                )

                return {
                    "found": False,
                    "status": "Rate Limited",
                    "year": "Not Found",
                }

            if assessment_response.status_code in (
                401,
                403
            ):

                return {
                    "found": False,
                    "status": "Authentication Error",
                    "year": "Not Found",
                }

            if assessment_response.status_code == 404:

                return {
                    "found": False,
                    "status": "Not Found",
                    "year": "Not Found",
                }

            assessment_response.raise_for_status()

            assessment = (
                assessment_response.json()
            )

            # ------------------------------------------------
            # Extract category.
            #
            # v4 may expose the category as:
            #
            # red_list_category.code
            #
            # or:
            #
            # red_list_category_code
            # ------------------------------------------------

            status = None

            if isinstance(
                assessment,
                dict
            ):

                category = (
                    assessment.get(
                        "red_list_category"
                    )
                )

                if isinstance(
                    category,
                    dict
                ):

                    status = (
                        category.get(
                            "code"
                        )
                        or
                        category.get(
                            "name"
                        )
                    )

                if not status:

                    status = (
                        assessment.get(
                            "red_list_category_code"
                        )
                    )

                if not status:

                    status = (
                        assessment.get(
                            "category"
                        )
                    )

                if not status:

                    status = (
                        assessment.get(
                            "red_list_category"
                        )
                    )

            if not status:

                status = "Not Found"

            # ------------------------------------------------
            # Extract assessment year.
            # ------------------------------------------------

            year = None

            if isinstance(
                assessment,
                dict
            ):

                year = (
                    assessment.get(
                        "year_published"
                    )
                    or
                    assessment.get(
                        "assessment_year"
                    )
                    or
                    assessment.get(
                        "year"
                    )
                )

            if not year:

                year = (
                    selected_record.get(
                        "year_published"
                    )
                    or
                    selected_record.get(
                        "assessment_year"
                    )
                    or
                    selected_record.get(
                        "year"
                    )
                    or
                    "Not Found"
                )

            return {

                "found": True,

                "status": self.normalize_status_code(status),

                "year": year,
            }

        except requests.RequestException as exc:

            print(
                f"    IUCN request error: {exc}"
            )

            return {

                "found": False,

                "status": "Not Found",

                "year": "Not Found",
            }

        except Exception as exc:

            print(
                f"    IUCN parsing error: {exc}"
            )

            return {

                "found": False,

                "status": "Not Found",

                "year": "Not Found",
            }


# ============================================================
# TEST / CLI
# ============================================================

if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Ayurvedic Knowledge Graph "
            "Plant Collector"
        )
    )

    parser.add_argument(
        "--plant",
        help=(
            "Single plant scientific name"
        )
    )

    parser.add_argument(
        "--file",
        help=(
            "TXT, CSV, XLSX or XLS "
            "containing plant names"
        )
    )

    parser.add_argument(
        "--output",
        default="Plant.xlsx",
        help=(
            "Output Excel file"
        )
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # Read input names
    # --------------------------------------------------------

    names = []

    if args.plant and args.file:

        raise SystemExit(
            "Use either --plant or --file, "
            "not both."
        )

    if args.plant:

        names = [
            args.plant.strip()
        ]

    elif args.file:

        from pathlib import Path

        input_path = Path(
            args.file
        )

        if not input_path.exists():

            raise SystemExit(
                f"Input file not found:\n"
                f"{input_path}"
            )

        suffix = (
            input_path
            .suffix
            .lower()
        )

        # ----------------------------------------------------
        # TXT
        # ----------------------------------------------------

        if suffix in {
            ".txt",
            ".text"
        }:

            names = [
                x.strip()
                for x
                in input_path
                .read_text(
                    encoding="utf-8"
                )
                .splitlines()
                if x.strip()
            ]

        # ----------------------------------------------------
        # CSV
        # ----------------------------------------------------

        elif suffix == ".csv":

            import csv

            with input_path.open(
                "r",
                encoding="utf-8-sig",
                newline=""
            ) as f:

                rows = list(
                    csv.DictReader(f)
                )

            if not rows:

                raise SystemExit(
                    "CSV contains no rows."
                )

            columns = {
                str(k)
                .strip()
                .lower(): k
                for k
                in rows[0].keys()
            }

            key = None

            for candidate in (
                "plant_name",
                "plant",
                "name"
            ):

                if candidate in columns:

                    key = columns[
                        candidate
                    ]

                    break

            if key is None:

                raise SystemExit(
                    "CSV must contain "
                    "plant_name, plant, "
                    "or name column."
                )

            names = [
                str(
                    row[key]
                ).strip()
                for row
                in rows
                if row.get(key)
                and
                str(
                    row[key]
                ).strip()
            ]

        # ----------------------------------------------------
        # Excel
        # ----------------------------------------------------

        elif suffix in {
            ".xlsx",
            ".xls"
        }:

            import pandas as pd

            df = pd.read_excel(
                input_path
            )

            candidates = {
                str(c)
                .strip()
                .lower(): c
                for c
                in df.columns
            }

            key = None

            for candidate in (
                "plant_name",
                "plant",
                "name"
            ):

                if candidate in candidates:

                    key = candidates[
                        candidate
                    ]

                    break

            if key is None:

                raise SystemExit(
                    "Excel must contain "
                    "plant_name, plant, "
                    "or name column."
                )

            names = [
                str(value).strip()
                for value
                in df[key].dropna()
                if str(value).strip()
            ]

        else:

            raise SystemExit(
                "Supported input files: "
                ".txt, .csv, .xlsx, .xls"
            )

    else:

        print()

        print(
            "1. Enter one plant name"
        )

        print(
            "2. Enter a TXT/CSV/Excel file"
        )

        choice = input(
            "\nChoose 1 or 2: "
        ).strip()

        if choice == "1":

            name = input(
                "Enter plant name: "
            ).strip()

            names = (
                [name]
                if name
                else []
            )

        elif choice == "2":

            file_path = input(
                "Enter file path: "
            ).strip()

            from pathlib import Path

            input_path = Path(
                file_path
            )

            if not input_path.exists():

                raise SystemExit(
                    f"Input file not found:\n"
                    f"{input_path}"
                )

            suffix = (
                input_path
                .suffix
                .lower()
            )

            if suffix in {
                ".txt",
                ".text"
            }:

                names = [
                    x.strip()
                    for x
                    in input_path
                    .read_text(
                        encoding="utf-8"
                    )
                    .splitlines()
                    if x.strip()
                ]

            elif suffix in {
                ".xlsx",
                ".xls"
            }:

                import pandas as pd

                df = pd.read_excel(
                    input_path
                )

                candidates = {
                    str(c)
                    .strip()
                    .lower(): c
                    for c
                    in df.columns
                }

                key = None

                for candidate in (
                    "plant_name",
                    "plant",
                    "name"
                ):

                    if candidate in candidates:

                        key = candidates[
                            candidate
                        ]

                        break

                if key is None:

                    raise SystemExit(
                        "Excel must contain "
                        "plant_name, plant, "
                        "or name column."
                    )

                names = [
                    str(value).strip()
                    for value
                    in df[key].dropna()
                    if str(value).strip()
                ]

            elif suffix == ".csv":

                import pandas as pd

                df = pd.read_csv(
                    input_path
                )

                candidates = {
                    str(c)
                    .strip()
                    .lower(): c
                    for c
                    in df.columns
                }

                key = None

                for candidate in (
                    "plant_name",
                    "plant",
                    "name"
                ):

                    if candidate in candidates:

                        key = candidates[
                            candidate
                        ]

                        break

                if key is None:

                    raise SystemExit(
                        "CSV must contain "
                        "plant_name, plant, "
                        "or name column."
                    )

                names = [
                    str(value).strip()
                    for value
                    in df[key].dropna()
                    if str(value).strip()
                ]

            else:

                raise SystemExit(
                    "Supported input files: "
                    ".txt, .csv, .xlsx, .xls"
                )

        else:

            raise SystemExit(
                "Invalid choice."
            )

    if not names:

        raise SystemExit(
            "No plant names supplied."
        )

    # --------------------------------------------------------
    # Collect
    # --------------------------------------------------------

    collector = PlantCollector()

    rows = []

    print()

    print(
        "=" * 70
    )

    print(
        "AYURVEDIC KNOWLEDGE GRAPH "
        "PLANT COLLECTOR"
    )

    print(
        "=" * 70
    )

    print(
        f"Plants to collect: {len(names)}"
    )

    print()

    for i, name in enumerate(
        names,
        1
    ):

        print(
            f"[{i}/{len(names)}] "
            f"{name}"
        )

        attempt = 0

        while True:

            try:

                row = collector.collect(
                    name
                )

                rows.append(
                    row
                )

                print(
                    f"  Scientific: "
                    f"{row['scientific_name']}"
                )

                print(
                    f"  Taxonomy  : "
                    f"{row['taxonomy_id']}"
                )

                print(
                    f"  Global    : "
                    f"{row['global_status']}"
                )

                print(
                    f"  Karnataka : "
                    f"{row['regional_status']}"
                )

                print()
                break

            except Exception as exc:

                attempt += 1
                wait = min(60, 2 ** min(attempt, 5))

                print(
                    f"  ERROR: {exc}"
                )
                print(
                    f"  RETRY {attempt}: retrying '{name}' "
                    f"in {wait}s..."
                )
                time.sleep(wait)

    # --------------------------------------------------------
    # Export
    # --------------------------------------------------------

    import pandas as pd
    from pathlib import Path

    output_path = Path(
        args.output
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    OUTPUT_COLUMNS = [

        "scientific_name",

        "taxonomy_id",

        "global_status",

        "regional_status",

        "iucn_year",

        "source",

        "common_names",
    ]

    result = pd.DataFrame(
        rows,
        columns=OUTPUT_COLUMNS
    )

    # Keep one row per scientific name.
    result = result.drop_duplicates(
        subset=["scientific_name"],
        keep="last"
    )

    with pd.ExcelWriter(
        output_path,
        engine="openpyxl"
    ) as writer:

        result.to_excel(
            writer,
            sheet_name="Plant",
            index=False
        )

        ws = writer.book[
            "Plant"
        ]

        ws.freeze_panes = "A2"

        ws.auto_filter.ref = (
            ws.dimensions
        )

        widths = {

            "A": 40,

            "B": 18,

            "C": 18,

            "D": 18,

            "E": 14,

            "F": 60,

            "G": 50,
        }

        for col, width in (
            widths.items()
        ):

            ws.column_dimensions[
                col
            ].width = width

    # --------------------------------------------------------
    # Final report
    # --------------------------------------------------------

    print(
        "=" * 70
    )

    print(
        "PLANT COLLECTION COMPLETE"
    )

    print(
        "=" * 70
    )

    print(
        f"Rows written : {len(result)}"
    )

    print(
        f"Output       : "
        f"{output_path.resolve()}"
    )

    print()

    print(
        "Columns:"
    )

    for column in OUTPUT_COLUMNS:

        print(
            f"  - {column}"
        )

    print()

    print(
        "Name handling:"
    )

    print(
        "  scientific_name = original input name"
    )

    print(
        "  taxonomy_id     = NCBI taxonomy ID"
    )

    print(
        "  common_names    = English common names only; comma-separated"
    )

    print()

    print(
        "IUCN handling:"
    )

    print(
        "  IUCN API v4 scientific-name lookup"
    )

    print(
        "  IUCN assessment lookup"
    )

    print(
        "=" * 70
    )