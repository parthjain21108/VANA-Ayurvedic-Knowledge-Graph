from __future__ import annotations
import os
import re
from typing import Any
from .models import StructuredQuery, EntityCandidate, EntityType, ResolutionStatus, QueryFilters, QueryStatus
from .schema import NODE_TYPES, FILTER_FIELDS
from .prompts import SYSTEM_PROMPT

DEFAULT_MODEL = os.getenv("NLP_MODEL", "gemini-3.5-flash-lite")

class SemanticParseError(RuntimeError): pass


_GENERIC_PATH_TERMS = {
    "plant": "plant", "plants": "plant",
    "part": "plant_part", "parts": "plant_part",
    "plant part": "plant_part", "plant parts": "plant_part",
    "phytochemical": "phytochemical", "phytochemicals": "phytochemical",
    "compound": "phytochemical", "compounds": "phytochemical",
    "bioactivity": "bioactivity", "bioactivities": "bioactivity",
    "activity": "bioactivity", "activities": "bioactivity",
    "bioactive": "bioactivity",
    "activity record": "bioactivity", "activity records": "bioactivity",
    "bioactivity record": "bioactivity", "bioactivity records": "bioactivity",
    "document": "document", "documents": "document",
    "paper": "document", "papers": "document",
    "publication": "document", "publications": "document",
    "research record": "document", "research records": "document",
    "target": "target", "targets": "target",
    "target linked": "target", "target related": "target",
    "target protein": "target", "target proteins": "target",
    "molecular target": "target", "molecular targets": "target",
    "receptor": "target", "receptors": "target",
    "therapeutic use": "therapeutic_use", "therapeutic uses": "therapeutic_use",
    "condition": "therapeutic_use", "conditions": "therapeutic_use",
    "disease": "therapeutic_use", "diseases": "therapeutic_use",
    "disorder": "therapeutic_use", "disorders": "therapeutic_use",
}


def _norm_term(value: str) -> str:
    value = str(value or "").casefold().strip()
    value = re.sub(r"[\u2010-\u2015-]+", " ", value)
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())


def _structural_requirements(
    user_query: str,
    parsed_entities: list[Any],
    requested: str | None,
    explicit: list[str],
) -> tuple[list[str], list[Any], str | None]:
    """Convert generic graph nouns into path requirements without inventing entities."""
    required: list[str] = []
    for value in explicit:
        value = str(value)
        if value in {
            "plant", "plant_part", "phytochemical", "therapeutic_use",
            "bioactivity", "target", "document",
        } and value not in required:
            required.append(value)

    specific_entities: list[Any] = []
    for entity in parsed_entities:
        term = _norm_term(entity.user_term)
        mapped = _GENERIC_PATH_TERMS.get(term)
        if mapped:
            if mapped not in required:
                required.append(mapped)
            continue
        specific_entities.append(entity)

    q = _norm_term(user_query)

    # These indicate a required downstream path without naming a concrete target.
    if re.search(
        r"\b(?:target|targets|target linked|target-linked|target related|"
        r"target-related|target protein|target proteins|molecular target|"
        r"molecular targets|receptor|receptors)\b|"
        r"\b(?:measured|tested|assayed|assays)\s+against\b|"
        r"\bacting\s+on\b|\binteracts?\s+with\b",
        q,
    ):
        if "target" not in required:
            required.append("target")

    if re.search(
        r"\b(?:bioactivity|bioactivities|bioactive|activity|activities|"
        r"assay|assays|measurement|measurements)\b",
        q,
    ):
        if "bioactivity" not in required:
            required.append("bioactivity")

    if re.search(
        r"\b(?:document(?:ed|s)?|paper(?:s)?|publication(?:s)?|"
        r"research record(?:s)?)\b",
        q,
    ):
        if "document" not in required:
            required.append("document")

    if re.search(r"\b(?:phytochemical(?:s)?|compound(?:s)?)\b", q):
        if "phytochemical" not in required:
            required.append("phytochemical")

    if re.search(
        r"\b(?:therapeutic use(?:s)?|condition(?:s)?|disease(?:s)?|disorder(?:s)?)\b",
        q,
    ):
        if "therapeutic_use" not in required:
            required.append("therapeutic_use")

    # Explicit plant-info query: "Tell me about neem" means full plant overview.
    if (
        parsed_entities
        and len(specific_entities) == 1
        and specific_entities[0].entity_type == "plant"
        and requested in {None, "plant"}
    ):
        requested = None

    return required, specific_entities, requested


def _models():
    from pydantic import BaseModel, Field
    from typing import Literal, Optional
    Entity = Literal["plant", "plant_part", "phytochemical", "therapeutic_use", "bioactivity", "target", "document"]
    class E(BaseModel):
        entity_type: Entity
        user_term: str
        role: Literal["subject", "constraint", "context"] = "context"
    class F(BaseModel):
        standard_type: Optional[str] = None
        standard_relation: Optional[str] = None
        standard_units: Optional[str] = None
        min_standard_value: Optional[float] = None
        max_standard_value: Optional[float] = None
        normalized_value_nm_min: Optional[float] = None
        normalized_value_nm_max: Optional[float] = None
        high_confidence_tier: Optional[str] = None
        target_organism: Optional[str] = None
        target_type: Optional[str] = None
    class Q(BaseModel):
        status: Literal["resolved", "ambiguous", "unresolved", "invalid"]
        operation: Literal["info", "list_related", "constrained_relation"]
        requested_entity_type: Optional[Entity] = None
        # Structural graph nodes required by wording, not concrete node mentions.
        path_requirements: list[Entity] = Field(default_factory=list)
        entities: list[E] = Field(default_factory=list)
        filters: F = Field(default_factory=F)
        confidence: Optional[float] = None
        clarification_question: Optional[str] = None
        notes: list[str] = Field(default_factory=list)
    return Q


def parse_query(user_query: str, *, api_key: str | None = None, model: str = DEFAULT_MODEL, client: Any | None = None) -> StructuredQuery:
    text = str(user_query).strip()
    if not text: raise ValueError("user_query must not be empty")
    key = api_key or os.getenv("GEMINI_API_KEY")
    if client is None:
        if not key: raise SemanticParseError("GEMINI_API_KEY is not set")
        try:
            from google import genai
        except ImportError as e:
            raise SemanticParseError("google-genai is not installed") from e
        client = genai.Client(api_key=key)
    Q = _models()
    try:
        from google import genai
        config = genai.types.GenerateContentConfig(response_mime_type="application/json", response_schema=Q, temperature=0)
    except Exception:
        class C: pass
        config = C(); config.response_mime_type = "application/json"; config.response_schema = Q; config.temperature = 0
    try:
        response = client.models.generate_content(model=model, contents=f"{SYSTEM_PROMPT}\n\nUSER QUERY:\n{text}", config=config)
        parsed = Q.model_validate_json(response.text)
    except Exception as e:
        raise SemanticParseError(f"Gemini semantic parsing failed: {e}") from e
    if parsed.status in {"ambiguous", "invalid", "unresolved"}:
        status = QueryStatus(parsed.status)
    else:
        status = QueryStatus.RESOLVED
    explicit_entity_models = [e for e in parsed.entities if e.user_term.strip()]
    path_requirements, retained_entity_models, normalized_requested = _structural_requirements(
        text,
        explicit_entity_models,
        parsed.requested_entity_type,
        list(getattr(parsed, "path_requirements", []) or []),
    )
    entities = [
        EntityCandidate(
            EntityType(e.entity_type),
            e.user_term.strip(),
            ResolutionStatus.UNRESOLVED,
        )
        for e in retained_entity_models
    ]
    f = parsed.filters.model_dump()
    filters = QueryFilters(**{k: v for k, v in f.items() if v is not None})
    explicit_types = {e.entity_type.value for e in entities}
    compatibility_intent = "plant_dynamic" if "plant" in explicit_types else "semantic_query"
    return StructuredQuery(
        status=status,
        original_query=text,
        operation=parsed.operation,
        requested_entity_type=normalized_requested,
        path_requirements=path_requirements,
        entities=entities,
        filters=filters,
        confidence=parsed.confidence,
        clarification_question=parsed.clarification_question,
        notes=list(parsed.notes),
        intent=compatibility_intent,
    )
