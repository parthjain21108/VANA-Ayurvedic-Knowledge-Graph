from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean(value)).casefold()


def identifier(value: Any) -> str:
    return _clean(value).casefold()


def canonical_number(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    try:
        d = Decimal(raw)
    except InvalidOperation:
        return raw
    if d == d.to_integral():
        return str(d.quantize(Decimal(1)))
    return format(d.normalize(), "f")


def node_key(label: str, row: dict[str, Any]) -> str:
    keys = {
        "Plant": ("scientific_name", normalized_text),
        "PlantPart": ("plant_part_id", identifier),
        "Phytochemicals": ("inchi_key", identifier),
        "TherapeuticUses": ("mesh_id", identifier),
        "Targets": ("target_chembl_id", identifier),
        "Documents": ("document_chembl_id", identifier),
    }
    if label == "Bioactivities":
        raise ValueError("Bioactivity identity is relationship-aware; use bioactivity_signature().")
    if label not in keys:
        raise KeyError(f"No identity rule for node table {label}")
    field, fn = keys[label]
    return fn(row.get(field))


def bioactivity_signature(row: dict[str, Any], target_id: Any, document_id: Any) -> tuple[str, ...] | None:
    target = identifier(target_id)
    document = identifier(document_id)
    if not target or not document:
        return None
    return (
        _clean(row.get("standard_type")),
        _clean(row.get("standard_relation")) or "NA",
        canonical_number(row.get("standard_value")),
        _clean(row.get("standard_units")),
        target,
        document,
    )
