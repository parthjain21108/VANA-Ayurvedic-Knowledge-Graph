from __future__ import annotations

from enum import Enum


class Intent(str, Enum):
    PLANT_INFORMATION = "plant_information"
    PLANT_PARTS = "plant_parts"
    PLANT_PHYTOCHEMICALS = "plant_phytochemicals"
    PLANT_THERAPEUTIC_USES = "plant_therapeutic_uses"
    PLANT_BIOACTIVITIES = "plant_bioactivities"
    PLANT_TARGETS = "plant_targets"
    PLANT_PHYTOCHEMICAL_BIOACTIVITIES = "plant_phytochemical_bioactivities"
    PLANT_PHYTOCHEMICAL_TARGETS = "plant_phytochemical_targets"
    PLANT_THERAPEUTIC_USE_PHYTOCHEMICALS = "plant_therapeutic_use_phytochemicals"

    PHYTOCHEMICAL_INFORMATION = "phytochemical_information"
    PHYTOCHEMICAL_PLANTS = "phytochemical_plants"
    PHYTOCHEMICAL_BIOACTIVITIES = "phytochemical_bioactivities"
    PHYTOCHEMICAL_TARGETS = "phytochemical_targets"

    THERAPEUTIC_USE_PLANTS = "therapeutic_use_plants"
    THERAPEUTIC_USE_PHYTOCHEMICALS = "therapeutic_use_phytochemicals"

    TARGET_PHYTOCHEMICALS = "target_phytochemicals"
    TARGET_BIOACTIVITIES = "target_bioactivities"

    BIOACTIVITY_INFORMATION = "bioactivity_information"
    BIOACTIVITY_TARGETS = "bioactivity_targets"
    BIOACTIVITY_DOCUMENTS = "bioactivity_documents"

    DOCUMENT_INFORMATION = "document_information"
    UNKNOWN = "unknown"


PLANT_INTENTS = {
    Intent.PLANT_INFORMATION,
    Intent.PLANT_PARTS,
    Intent.PLANT_PHYTOCHEMICALS,
    Intent.PLANT_THERAPEUTIC_USES,
    Intent.PLANT_BIOACTIVITIES,
    Intent.PLANT_TARGETS,
    Intent.PLANT_PHYTOCHEMICAL_BIOACTIVITIES,
    Intent.PLANT_PHYTOCHEMICAL_TARGETS,
    Intent.PLANT_THERAPEUTIC_USE_PHYTOCHEMICALS,
}


def is_plant_intent(intent: Intent | str) -> bool:
    # Plant routing is a graph-semantic property, not a closed intent list.
    # Dynamic semantic queries use the compatibility label ``plant_dynamic``.
    if isinstance(intent, str) and (intent == "plant_dynamic" or intent.startswith("plant_")):
        return True
    try:
        normalized = intent if isinstance(intent, Intent) else Intent(intent)
    except ValueError:
        return False
    return normalized in PLANT_INTENTS


def ingestion_eligible(intent: Intent | str) -> bool:
    return is_plant_intent(intent)


def ingestion_requires_scientific_name() -> bool:
    return True


def all_supported_intents() -> tuple[str, ...]:
    return tuple(intent.value for intent in Intent)
