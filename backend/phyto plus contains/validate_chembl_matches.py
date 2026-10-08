import pandas as pd
import requests
import re
import time
from pathlib import Path

# ============================================================
# FIX #1 VALIDATOR
# ChEMBL ID <-> InChIKey validation
# ============================================================

INPUT_FILE = Path("fix1_test_checkpoint.xlsx")
OUTPUT_FILE = Path("fix1_chembl_validation.xlsx")

print("=" * 70)
print("FIX #1 — CHEMBL / INCHIKEY VALIDATOR")
print("=" * 70)


# ============================================================
# LOAD FILE
# ============================================================

if not INPUT_FILE.exists():
    raise FileNotFoundError(
        f"\nInput file not found:\n{INPUT_FILE.resolve()}"
    )

df = pd.read_excel(INPUT_FILE, dtype=str).fillna("")
df.columns = [str(c).strip() for c in df.columns]

required = {
    "imppat_id",
    "phytochemical_name",
    "inchi_key",
    "chembl_id",
}

missing = required - set(df.columns)

if missing:
    raise ValueError(
        f"\nMissing required columns: {sorted(missing)}\n"
        f"Found:\n{list(df.columns)}"
    )

print(f"\nInput: {INPUT_FILE.resolve()}")
print(f"Rows: {len(df)}")


# ============================================================
# NORMALIZATION
# ============================================================

def normalize(value):
    return re.sub(r"\s+", "", str(value).strip()).upper()


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Ayurvedic-Plant-Graph-ChEMBL-Validator/1.0"
})


# ============================================================
# CACHE
# ============================================================

cache = {}


# ============================================================
# CHembl lookup
# ============================================================

def get_chembl_inchikey(chembl_id):

    chembl_id = chembl_id.strip().upper()

    if chembl_id in cache:
        return cache[chembl_id]

    url = (
        "https://www.ebi.ac.uk/chembl/api/data/"
        f"molecule/{chembl_id}.json"
    )

    last_error = None

    for attempt in range(1, 6):

        print(f"      API attempt {attempt}/5")

        try:

            response = session.get(
                url,
                timeout=(15, 60)
            )

            if response.status_code == 200:

                data = response.json()

                structures = data.get(
                    "molecule_structures"
                ) or {}

                key = structures.get(
                    "standard_inchi_key"
                )

                if not key:

                    result = (
                        None,
                        "NO_INCHIKEY"
                    )

                    cache[chembl_id] = result
                    return result

                result = (
                    normalize(key),
                    "OK"
                )

                cache[chembl_id] = result
                return result

            elif response.status_code == 404:

                result = (
                    None,
                    "CHEMBL_NOT_FOUND"
                )

                cache[chembl_id] = result
                return result

            else:

                last_error = (
                    f"HTTP_{response.status_code}"
                )

        except requests.exceptions.Timeout:

            last_error = "TIMEOUT"

        except requests.exceptions.ConnectionError:

            last_error = "CONNECTION_ERROR"

        except requests.exceptions.RequestException as e:

            last_error = (
                f"REQUEST_ERROR: {type(e).__name__}"
            )

        except Exception as e:

            last_error = (
                f"ERROR: {type(e).__name__}"
            )

        if attempt < 5:

            wait = 2 ** (attempt - 1)

            print(
                f"      Failed: {last_error}"
            )

            print(
                f"      Retrying in {wait}s..."
            )

            time.sleep(wait)

    result = (
        None,
        f"LOOKUP_FAILED: {last_error}"
    )

    cache[chembl_id] = result

    return result


# ============================================================
# VALIDATION
# ============================================================

results = []

for index, row in df.iterrows():

    imppat_id = row["imppat_id"].strip()
    name = row["phytochemical_name"].strip()

    local_key = normalize(
        row["inchi_key"]
    )

    chembl_id = row["chembl_id"].strip()

    print(
        f"\n[{index + 1}/{len(df)}] "
        f"{imppat_id} | {name}"
    )

    result = {
        "imppat_id": imppat_id,
        "phytochemical_name": name,
        "local_inchi_key": row["inchi_key"],
        "chembl_id": chembl_id,
        "chembl_inchi_key": "",
        "status": "",
        "details": "",
    }


    # --------------------------------------------------------
    # No ChEMBL
    # --------------------------------------------------------

    if not chembl_id:

        result["status"] = "UNRESOLVED"

        result["details"] = (
            "No ChEMBL ID supplied. "
            "No name-based mapping performed."
        )

        print("    ChEMBL: EMPTY")
        print("    Status: UNRESOLVED")

        results.append(result)

        continue


    # --------------------------------------------------------
    # No local InChIKey
    # --------------------------------------------------------

    if not local_key:

        result["status"] = "NO_LOCAL_INCHIKEY"

        result["details"] = (
            "ChEMBL ID exists but local "
            "compound has no InChIKey."
        )

        print(
            f"    ChEMBL: {chembl_id}"
        )

        print(
            "    Status: NO_LOCAL_INCHIKEY"
        )

        results.append(result)

        continue


    # --------------------------------------------------------
    # ChEMBL lookup
    # --------------------------------------------------------

    print(
        f"    ChEMBL: {chembl_id}"
    )

    print(
        "    Querying ChEMBL..."
    )

    chembl_key, lookup_status = (
        get_chembl_inchikey(chembl_id)
    )


    # --------------------------------------------------------
    # Lookup failed
    # --------------------------------------------------------

    if chembl_key is None:

        if lookup_status.startswith(
            "LOOKUP_FAILED"
        ):

            result["status"] = (
                "TEMPORARY_LOOKUP_FAILURE"
            )

            result["details"] = (
                lookup_status
            )

            print(
                "    Status: TEMPORARY LOOKUP FAILURE"
            )

        else:

            result["status"] = (
                "CHEMBL_INVALID"
            )

            result["details"] = (
                lookup_status
            )

            print(
                f"    Status: {lookup_status}"
            )

        results.append(result)

        continue


    # --------------------------------------------------------
    # Compare InChIKeys
    # --------------------------------------------------------

    result["chembl_inchi_key"] = (
        chembl_key
    )

    print(
        f"    Local InChIKey : {local_key}"
    )

    print(
        f"    ChEMBL InChIKey: {chembl_key}"
    )


    if chembl_key == local_key:

        result["status"] = (
            "VALIDATED_MATCH"
        )

        result["details"] = (
            "Local InChIKey exactly matches "
            "ChEMBL InChIKey."
        )

        print("    Status: PASS")

    else:

        result["status"] = (
            "MISMATCH"
        )

        result["details"] = (
            "ChEMBL ID does not correspond "
            "to local compound according to InChIKey."
        )

        print("    !!! MISMATCH !!!")


    results.append(result)

    time.sleep(0.5)


# ============================================================
# REPORT
# ============================================================

report = pd.DataFrame(results)


summary = pd.DataFrame({
    "Metric": [
        "Total compounds",
        "ChEMBL IDs populated",
        "Validated ChEMBL matches",
        "ChEMBL mismatches",
        "Unresolved ChEMBL",
        "No local InChIKey",
        "Temporary lookup failures",
        "Invalid ChEMBL IDs",
    ],

    "Count": [

        len(report),

        (
            report["chembl_id"] != ""
        ).sum(),

        (
            report["status"]
            == "VALIDATED_MATCH"
        ).sum(),

        (
            report["status"]
            == "MISMATCH"
        ).sum(),

        (
            report["status"]
            == "UNRESOLVED"
        ).sum(),

        (
            report["status"]
            == "NO_LOCAL_INCHIKEY"
        ).sum(),

        (
            report["status"]
            == "TEMPORARY_LOOKUP_FAILURE"
        ).sum(),

        (
            report["status"]
            == "CHEMBL_INVALID"
        ).sum(),
    ]
})


validated = report[
    report["status"]
    == "VALIDATED_MATCH"
].copy()


mismatches = report[
    report["status"]
    == "MISMATCH"
].copy()


unresolved = report[
    report["status"]
    == "UNRESOLVED"
].copy()


temporary_failures = report[
    report["status"]
    == "TEMPORARY_LOOKUP_FAILURE"
].copy()


# ============================================================
# SAVE
# ============================================================

with pd.ExcelWriter(
    OUTPUT_FILE,
    engine="openpyxl"
) as writer:

    summary.to_excel(
        writer,
        sheet_name="Summary",
        index=False
    )

    report.to_excel(
        writer,
        sheet_name="All Results",
        index=False
    )

    validated.to_excel(
        writer,
        sheet_name="Validated",
        index=False
    )

    mismatches.to_excel(
        writer,
        sheet_name="MISMATCHES",
        index=False
    )

    unresolved.to_excel(
        writer,
        sheet_name="Unresolved",
        index=False
    )

    temporary_failures.to_excel(
        writer,
        sheet_name="Retry_Required",
        index=False
    )


# ============================================================
# FINAL
# ============================================================

print("\n")
print("=" * 70)
print("FINAL RESULT")
print("=" * 70)

print(
    summary.to_string(index=False)
)

print("\n")


if len(mismatches) == 0:

    print(
        "✅ NO CONFIRMED CHEMBL / INCHIKEY MISMATCHES"
    )

else:

    print(
        "❌ CONFIRMED CHEMBL / INCHIKEY MISMATCHES FOUND"
    )

    print("\nMISMATCHES:")

    print(
        mismatches[
            [
                "imppat_id",
                "phytochemical_name",
                "local_inchi_key",
                "chembl_id",
                "chembl_inchi_key",
            ]
        ].to_string(index=False)
    )


if len(temporary_failures) > 0:

    print(
        f"\n⚠️ {len(temporary_failures)} ChEMBL lookups "
        "could not be verified because of network/API failure."
    )

    print(
        "These are NOT considered mismatches."
    )


print("\nReport:")
print(
    OUTPUT_FILE.resolve()
)

print("=" * 70)