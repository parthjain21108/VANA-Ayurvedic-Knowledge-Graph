from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any
import json

class QueryStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    INVALID = "invalid"

class EntityType(str, Enum):
    PLANT = "plant"
    PLANT_PART = "plant_part"
    PHYTOCHEMICAL = "phytochemical"
    THERAPEUTIC_USE = "therapeutic_use"
    TARGET = "target"
    BIOACTIVITY = "bioactivity"
    DOCUMENT = "document"

class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"

@dataclass
class EntityCandidate:
    entity_type: EntityType
    user_term: str
    resolution_status: ResolutionStatus
    canonical_value: str | None = None
    canonical_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    confidence: float | None = None

    def validate(self) -> None:
        if not self.user_term.strip():
            raise ValueError("user_term must not be empty")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.resolution_status == ResolutionStatus.RESOLVED and not (self.canonical_id or self.canonical_value):
            raise ValueError("resolved entity needs canonical identity")
        if self.resolution_status == ResolutionStatus.AMBIGUOUS and not self.candidates:
            raise ValueError("ambiguous entity needs candidates")

@dataclass
class QueryFilters:
    standard_type: str | None = None
    standard_relation: str | None = None
    standard_units: str | None = None
    min_standard_value: float | None = None
    max_standard_value: float | None = None
    normalized_value_nm_min: float | None = None
    normalized_value_nm_max: float | None = None
    high_confidence_tier: str | None = None
    target_organism: str | None = None
    target_type: str | None = None

@dataclass
class StructuredQuery:
    status: QueryStatus
    original_query: str
    operation: str
    requested_entity_type: str | None
    # Graph node types required by query wording but not necessarily concrete entity mentions.
    path_requirements: list[str] = field(default_factory=list)
    entities: list[EntityCandidate] = field(default_factory=list)
    filters: QueryFilters = field(default_factory=QueryFilters)
    confidence: float | None = None
    clarification_question: str | None = None
    notes: list[str] = field(default_factory=list)
    # Compatibility/debug field. It is derived after parsing; Gemini never emits it.
    intent: str = "semantic_query"

    def validate(self) -> None:
        if not self.original_query.strip(): raise ValueError("original_query empty")
        if self.status == QueryStatus.AMBIGUOUS and not self.clarification_question:
            raise ValueError("ambiguous query needs clarification_question")
        for path_type in self.path_requirements:
            if not isinstance(path_type, str) or not path_type.strip():
                raise ValueError("path_requirements must contain non-empty strings")
        for e in self.entities: e.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate(); return asdict(self)
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
