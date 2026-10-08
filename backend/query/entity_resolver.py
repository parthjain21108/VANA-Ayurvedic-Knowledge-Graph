from __future__ import annotations

from difflib import SequenceMatcher
import os
import re
from typing import Any

from .models import (
    EntityCandidate,
    EntityType,
    QueryStatus,
    ResolutionStatus,
    StructuredQuery,
)
from .plant_resolver import resolve_plant
from .schema import (
    CANONICAL_KEYS,
    NODE_LABELS,
    SEARCH_FIELDS,
    DISPLAY_FIELDS,
)


def fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() * 100.0


class EntityResolutionError(RuntimeError):
    """Raised when graph-backed entity resolution cannot be completed."""


def _norm(value: Any) -> str:
    text = str(value or "").casefold().strip()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _tokens(value: Any) -> list[str]:
    """Return tokens plus conservative English inflection variants."""
    result: list[str] = []

    for token in _norm(value).split():
        if not token:
            continue

        result.append(token)

        # Keep the original token and add only simple, reversible-ish
        # morphology so "leaves" can match "leaf", "disorders" can match
        # "disorder", etc. Domain-specific aliases remain forbidden.
        if len(token) > 4 and token.endswith("ies"):
            result.append(token[:-3] + "y")
        if len(token) > 4 and token.endswith("ves"):
            result.append(token[:-3] + "f")
        if len(token) > 3 and token.endswith("s"):
            result.append(token[:-1])

    return list(dict.fromkeys(result))


def _extract(row: Any) -> dict[str, Any]:
    """
    Accept both Neo4j Record rows returned by RETURN n and plain mappings.
    """
    try:
        node = row["n"]
    except (KeyError, TypeError, IndexError):
        node = row

    if hasattr(node, "items"):
        return dict(node.items())

    return dict(node)


def _display(entity_type: str, node: dict[str, Any]) -> str:
    for field in DISPLAY_FIELDS[entity_type]:
        value = node.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()

    return str(node.get(CANONICAL_KEYS[entity_type], "")).strip()


def _identity(entity_type: str, node: dict[str, Any]) -> str:
    canonical = node.get(CANONICAL_KEYS[entity_type])
    if canonical is not None and str(canonical).strip():
        return str(canonical).strip()

    return _display(entity_type, node)


def _unique(
    entity_type: str,
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    for node in nodes:
        identity = _identity(entity_type, node)
        if identity:
            result[identity] = node

    return list(result.values())


def _primary_fields(entity_type: str) -> list[str]:
    """
    Fields that represent the entity itself.

    The first display field is treated as its semantic name, while the
    canonical key is always included as an identity field.
    """
    fields: list[str] = []

    display = DISPLAY_FIELDS[entity_type]
    if display:
        fields.append(display[0])

    canonical = CANONICAL_KEYS[entity_type]
    if canonical not in fields:
        fields.append(canonical)

    return fields


def _exact_primary_match(
    session: Any,
    entity_type: str,
    term: str,
    plant_name: str | None = None,
) -> list[dict[str, Any]]:
    """Exact case-insensitive identity lookup against real Neo4j nodes."""
    label = NODE_LABELS[entity_type]
    fields = _primary_fields(entity_type)

    scope = ""
    if entity_type == "plant_part" and plant_name:
        scope = """
AND toLower(toString(n.`plant_name`)) = toLower($plant_name)
"""

    found: list[dict[str, Any]] = []

    for field in fields:
        query = f"""
MATCH (n:`{label}`)
WHERE toLower(toString(n.`{field}`)) = toLower($term)
{scope}
RETURN n
LIMIT 50
"""

        try:
            rows = list(
                session.run(
                    query,
                    term=term.strip(),
                    plant_name=plant_name,
                )
            )
        except Exception as exc:
            raise EntityResolutionError(
                f"Exact Neo4j lookup failed for {entity_type}.{field}: {exc}"
            ) from exc

        found.extend(_extract(row) for row in rows)

    return _unique(entity_type, found)


def _contextual_candidates(
    session: Any,
    entity_type: str,
    plant_name: str | None = None,
) -> list[dict[str, Any]]:
    """
    Retrieve a bounded real-graph candidate domain for semantic resolution.

    These finite domains are the places where literal lexical recall is
    insufficient for natural language:
      - Plant Part: all parts for the resolved plant.
      - Target: all Targets in the graph.
      - Therapeutic Use: all Therapeutic Uses in the graph.

    Gemini is allowed to choose only from nodes returned here.
    """
    label = NODE_LABELS[entity_type]

    if entity_type == "plant_part":
        if not plant_name:
            return []

        query = f"""
MATCH (n:`{label}`)
WHERE toLower(toString(n.`plant_name`)) = toLower($plant_name)
RETURN n
ORDER BY toLower(toString(n.`plant_part`))
"""

        try:
            rows = list(session.run(query, plant_name=plant_name))
        except Exception as exc:
            raise EntityResolutionError(
                f"Could not retrieve plant parts for {plant_name}: {exc}"
            ) from exc

        return _unique(
            entity_type,
            [_extract(row) for row in rows],
        )

    if entity_type in {"target", "therapeutic_use"}:
        display_field = DISPLAY_FIELDS[entity_type][0]

        query = f"""
MATCH (n:`{label}`)
RETURN n
ORDER BY toLower(toString(n.`{display_field}`))
"""

        try:
            rows = list(session.run(query))
        except Exception as exc:
            raise EntityResolutionError(
                f"Could not retrieve {entity_type} candidate set."
            ) from exc

        return _unique(
            entity_type,
            [_extract(row) for row in rows],
        )

    return []


def _candidate_rows(
    session: Any,
    entity_type: str,
    term: str,
    limit: int,
    plant_name: str | None = None,
) -> list[dict[str, Any]]:
    """
    Retrieve lexical candidates from graph searchable fields.

    Exact phrase and phrase-containment are intentionally deterministic.
    """
    label = NODE_LABELS[entity_type]
    fields = SEARCH_FIELDS[entity_type]

    if not fields or not _norm(term):
        return []

    scope = ""
    if entity_type == "plant_part" and plant_name:
        scope = """
AND toLower(toString(n.`plant_name`)) = toLower($plant_name)
"""

    candidates: dict[str, dict[str, Any]] = {}

    def add_rows(rows: list[Any]) -> None:
        for row in rows:
            node = _extract(row)
            identity = _identity(entity_type, node)
            if identity:
                candidates[identity] = node

    exact_clause = " OR ".join(
        f"toLower(toString(n.`{field}`)) = toLower($term)"
        for field in fields
    )

    query = f"""
MATCH (n:`{label}`)
WHERE ({exact_clause})
{scope}
RETURN n
LIMIT $limit
"""

    try:
        rows = list(
            session.run(
                query,
                term=term,
                plant_name=plant_name,
                limit=limit,
            )
        )
    except Exception as exc:
        raise EntityResolutionError(
            f"Neo4j exact candidate search failed for {entity_type}: {exc}"
        ) from exc

    add_rows(rows)

    contains_clause = " OR ".join(
        f"toLower(toString(n.`{field}`)) CONTAINS toLower($term)"
        for field in fields
    )

    query = f"""
MATCH (n:`{label}`)
WHERE ({contains_clause})
{scope}
RETURN n
LIMIT $limit
"""

    try:
        rows = list(
            session.run(
                query,
                term=term,
                plant_name=plant_name,
                limit=limit,
            )
        )
    except Exception as exc:
        raise EntityResolutionError(
            f"Neo4j containment search failed for {entity_type}: {exc}"
        ) from exc

    add_rows(rows)

    return list(candidates.values())


def _score(
    entity_type: str,
    term: str,
    node: dict[str, Any],
) -> float:
    """
    Rank a REAL graph node.

    Primary entity-name fields dominate category/metadata fields.
    Token overlap provides recall for natural phrases such as
    "skin disorders" vs "Skin Diseases".
    """
    target = _norm(term)
    target_tokens = set(_tokens(term))
    primary_fields = set(_primary_fields(entity_type))

    best_primary = 0.0
    best_secondary = 0.0

    if not target:
        return 0.0

    for field in SEARCH_FIELDS[entity_type]:
        value = _norm(node.get(field))
        if not value:
            continue

        value_tokens = set(_tokens(value))
        overlap = (
            len(target_tokens & value_tokens) / max(1, len(target_tokens))
        )

        sequence = SequenceMatcher(None, target, value).ratio()

        if value == target:
            score = 1.0
        elif target in value or value in target:
            score = 0.95
        else:
            score = max(
                0.55 * overlap + 0.45 * sequence,
                0.80 * sequence,
            )

        if field in primary_fields:
            best_primary = max(best_primary, score)
        else:
            best_secondary = max(best_secondary, score)

    return 0.85 * best_primary + 0.15 * best_secondary


def _rank_candidates(
    entity_type: str,
    term: str,
    nodes: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], float]]:
    """Rank real graph nodes while protecting recall for natural paraphrases."""
    stopwords = {
        "a", "an", "and", "for", "from", "in", "of", "on",
        "the", "to", "with", "which", "what",
    }
    target_tokens = {
        token
        for token in _tokens(term)
        if token not in stopwords
    }

    ranked: list[tuple[dict[str, Any], float]] = []
    for node in _unique(entity_type, nodes):
        score = _score(entity_type, term, node)

        # Finite semantic domains (targets, therapeutic uses, plant parts)
        # need better recall for phrases such as "conditions affecting the
        # skin" or "EGF receptor". A single meaningful overlapping token is
        # enough to keep a candidate in the semantic-review set, but not enough
        # to trigger the deterministic winner threshold.
        if entity_type in {"plant_part", "target", "therapeutic_use"} and target_tokens:
            best_overlap = 0
            for field in SEARCH_FIELDS[entity_type]:
                value_tokens = {
                    token
                    for token in _tokens(node.get(field))
                    if token not in stopwords
                }
                best_overlap = max(
                    best_overlap,
                    len(target_tokens & value_tokens),
                )

            if best_overlap:
                overlap_floor = min(
                    0.94,
                    0.72 + 0.06 * max(0, best_overlap - 1),
                )
                score = max(score, overlap_floor)

        ranked.append((node, score))

    ranked.sort(
        key=lambda item: (
            -item[1],
            _norm(_display(entity_type, item[0])),
            _identity(entity_type, item[0]),
        )
    )
    return ranked


def _unresolved(entity: EntityCandidate) -> EntityCandidate:
    return EntityCandidate(
        entity_type=entity.entity_type,
        user_term=entity.user_term,
        resolution_status=ResolutionStatus.UNRESOLVED,
        canonical_value=None,
        canonical_id=None,
        aliases=list(entity.aliases),
        candidates=[],
        confidence=None,
    )


def _ambiguous(
    entity: EntityCandidate,
    entity_type: str,
    nodes: list[dict[str, Any]],
    confidence: float | None = None,
) -> EntityCandidate:
    choices = [
        (
            f"{node.get(CANONICAL_KEYS[entity_type])}"
            f" | {_display(entity_type, node)}"
        )
        for node in nodes
    ]

    return EntityCandidate(
        entity_type=entity.entity_type,
        user_term=entity.user_term,
        resolution_status=ResolutionStatus.AMBIGUOUS,
        canonical_value=None,
        canonical_id=None,
        aliases=list(entity.aliases),
        candidates=choices,
        confidence=confidence,
    )


def _resolved_as_type(
    entity: EntityCandidate,
    entity_type: str,
    node: dict[str, Any],
    confidence: float,
) -> EntityCandidate:
    """Resolve a mention as another graph type only after an exact primary-field match."""
    retyped = EntityCandidate(
        entity_type=EntityType(entity_type),
        user_term=entity.user_term,
        resolution_status=ResolutionStatus.RESOLVED,
        aliases=list(entity.aliases),
    )
    return _resolved(retyped, entity_type, node, confidence)


def _resolved(
    entity: EntityCandidate,
    entity_type: str,
    node: dict[str, Any],
    confidence: float,
) -> EntityCandidate:
    canonical = node.get(CANONICAL_KEYS[entity_type])
    display = _display(entity_type, node)

    value = str(canonical) if canonical is not None else display
    canonical_id = str(canonical) if canonical is not None else None

    return EntityCandidate(
        entity_type=entity.entity_type,
        user_term=entity.user_term,
        resolution_status=ResolutionStatus.RESOLVED,
        canonical_value=value,
        canonical_id=canonical_id,
        aliases=list(entity.aliases),
        candidates=[],
        confidence=max(0.0, min(1.0, float(confidence))),
    )


def _semantic_choice(
    entity: EntityCandidate,
    entity_type: str,
    ranked_nodes: list[dict[str, Any]],
    max_candidates: int,
) -> EntityCandidate | None:
    """
    Use Gemini only as a chooser among REAL graph nodes.

    The model never supplies a canonical ID and never invents an entity.
    """
    if not ranked_nodes:
        return None

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
        from pydantic import BaseModel
    except ImportError as exc:
        raise EntityResolutionError(
            "Gemini semantic resolution requires google-genai and pydantic."
        ) from exc

    class Choice(BaseModel):
        index: int | None = None
        confidence: float | None = None

    # Finite domains can be larger, but ranking first keeps the prompt bounded
    # and makes semantic resolution deterministic/reproducible.
    if entity_type in {"plant_part", "target", "therapeutic_use"}:
        # Preserve semantic recall when the correct real graph node ranks beyond
        # the original 25-candidate cutoff.
        candidate_limit = min(
            len(ranked_nodes),
            max(100, max_candidates * 10),
        )
    else:
        candidate_limit = max_candidates

    candidates = ranked_nodes[:candidate_limit]

    lines: list[str] = []

    for index, node in enumerate(candidates):
        display = _display(entity_type, node)
        canonical = node.get(CANONICAL_KEYS[entity_type])

        searchable: list[str] = []
        for field in SEARCH_FIELDS[entity_type]:
            value = node.get(field)
            if value is not None and str(value).strip():
                searchable.append(f"{field}={value}")

        lines.append(
            f"{index}: display={display}; canonical={canonical}; "
            + "; ".join(searchable)
        )

    prompt = f"""
Resolve the user's entity mention against EXISTING records from the
Ayurvedic Neo4j knowledge graph.

Entity type:
{entity_type}

User mention:
{entity.user_term}

Every candidate below already exists in Neo4j.

Rules:
- Choose the candidate whose meaning best matches the COMPLETE user phrase.
- Semantic meaning matters more than exact spelling.
- Singular/plural and ordinary wording differences are acceptable.
- Abbreviations may refer to a full stored name.
- The selected entity MUST be one of the supplied candidates.
- Never invent an entity.
- Never invent a canonical ID.
- Do not choose a merely related category when another candidate represents
  the requested entity itself.
- Return null when none is defensible.

Candidates:
{chr(10).join(lines)}
""".strip()

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=os.getenv("NLP_MODEL", "gemini-3.5-flash-lite"),
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Choice,
                temperature=0,
            ),
        )

        raw = getattr(response, "text", "")
        if not raw:
            raise EntityResolutionError(
                f"Gemini returned empty semantic-resolution output for "
                f"'{entity.user_term}'."
            )

        if hasattr(Choice, "model_validate_json"):
            choice = Choice.model_validate_json(raw)
        else:
            choice = Choice.parse_raw(raw)

    except EntityResolutionError:
        raise
    except Exception as exc:
        raise EntityResolutionError(
            f"Gemini semantic resolution failed for "
            f"'{entity.user_term}': {exc}"
        ) from exc

    index = choice.index

    if index is None:
        return None

    if not isinstance(index, int):
        raise EntityResolutionError(
            "Gemini returned a non-integer candidate index."
        )

    if not 0 <= index < len(candidates):
        raise EntityResolutionError(
            "Gemini returned an invalid candidate index."
        )

    confidence = (
        float(choice.confidence)
        if choice.confidence is not None
        else 0.85
    )
    confidence = max(0.0, min(1.0, confidence))

    return _resolved(
        entity,
        entity_type,
        candidates[index],
        confidence,
    )


def resolve_entity(
    entity: EntityCandidate,
    session: Any,
    *,
    max_candidates: int = 5,
    plant_scientific_name: str | None = None,
) -> EntityCandidate:
    entity_type = entity.entity_type.value

    if entity_type not in CANONICAL_KEYS:
        raise EntityResolutionError(
            f"Unsupported entity type: {entity_type}"
        )

    # Plant resolution has its own graph-first resolver, which can safely
    # use external taxonomy validation only when the graph lacks the plant.
    if entity_type == "plant":
        return resolve_plant(entity, session)

    # ---------------------------------------------------------
    # 1. Exact identity always wins.
    # ---------------------------------------------------------
    exact = _exact_primary_match(
        session,
        entity_type,
        entity.user_term,
        plant_scientific_name,
    )

    if len(exact) == 1:
        return _resolved(entity, entity_type, exact[0], 1.0)

    if len(exact) > 1:
        return _ambiguous(entity, entity_type, exact, 1.0)

    # ---------------------------------------------------------
    # 2. Finite semantic domains.
    #
    # This is the critical recall path for:
    #   leaves
    #   EGFR
    #   skin disorders
    #   conditions affecting the skin
    #
    # We search the real graph domain first; Gemini may only choose among
    # those returned nodes.
    # ---------------------------------------------------------
    if entity_type in {"plant_part", "target", "therapeutic_use"}:
        contextual = _contextual_candidates(
            session,
            entity_type,
            plant_scientific_name,
        )

        if not contextual:
            return _unresolved(entity)

        ranked = _rank_candidates(
            entity_type,
            entity.user_term,
            contextual,
        )

        best_node, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else -1.0

        # Strong deterministic winner: don't spend a Gemini call.
        if (
            best_score >= 0.96
            and best_score > second_score + 0.12
        ):
            return _resolved(
                entity,
                entity_type,
                best_node,
                best_score,
            )

        semantic = _semantic_choice(
            entity,
            entity_type,
            [node for node, _ in ranked],
            max_candidates,
        )

        if semantic is not None:
            return semantic

        return _ambiguous(
            entity,
            entity_type,
            [node for node, _ in ranked[:max_candidates]],
            best_score,
        )

    # ---------------------------------------------------------
    # 3. Conservative cross-domain recovery.
    #
    # Gemini can occasionally label a therapeutic-use phrase as bioactivity.
    # Recover only from an EXACT primary-field match in the alternate domain.
    # ---------------------------------------------------------
    if entity_type == "bioactivity":
        alternate_exact = _exact_primary_match(
            session,
            "therapeutic_use",
            entity.user_term,
            plant_scientific_name,
        )
        if len(alternate_exact) == 1:
            return _resolved_as_type(
                entity,
                "therapeutic_use",
                alternate_exact[0],
                1.0,
            )

    # ---------------------------------------------------------
    # 4. Normal lexical recall for larger domains.
    # ---------------------------------------------------------
    candidates = _candidate_rows(
        session=session,
        entity_type=entity_type,
        term=entity.user_term,
        limit=max(50, max_candidates * 10),
        plant_name=plant_scientific_name,
    )

    if not candidates:
        return _unresolved(entity)

    ranked = _rank_candidates(
        entity_type,
        entity.user_term,
        candidates,
    )

    best_node, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else -1.0

    if (
        best_score >= 0.96
        and best_score > second_score + 0.12
    ):
        return _resolved(
            entity,
            entity_type,
            best_node,
            best_score,
        )

    semantic = _semantic_choice(
        entity,
        entity_type,
        [node for node, _ in ranked],
        max_candidates,
    )

    if semantic is not None:
        return semantic

    return _ambiguous(
        entity,
        entity_type,
        [node for node, _ in ranked[:max_candidates]],
        best_score,
    )


def resolve_entities(
    query: StructuredQuery,
    session: Any,
    *,
    max_candidates: int = 5,
) -> StructuredQuery:
    originals = list(query.entities)
    results_by_index: dict[int, EntityCandidate] = {}
    resolved_plant: str | None = None

    # Plant must be resolved first because Plant Part is scoped by the
    # canonical scientific_name stored on the Plant Part nodes.
    for index, entity in enumerate(originals):
        if entity.entity_type != EntityType.PLANT:
            continue

        result = resolve_entity(
            entity,
            session,
            max_candidates=max_candidates,
        )
        results_by_index[index] = result

        if result.resolution_status == ResolutionStatus.RESOLVED:
            resolved_plant = result.canonical_value

    # Resolve all remaining entities using the resolved plant as context.
    for index, entity in enumerate(originals):
        if entity.entity_type == EntityType.PLANT:
            continue

        result = resolve_entity(
            entity,
            session,
            max_candidates=max_candidates,
            plant_scientific_name=resolved_plant,
        )
        results_by_index[index] = result

    # Restore the original mention order by position. This also preserves a
    # legitimate cross-domain retype without losing it during bucketing.
    ordered: list[EntityCandidate] = []
    for index, original in enumerate(originals):
        ordered.append(results_by_index.get(index, _unresolved(original)))

    unresolved = [
        entity
        for entity in ordered
        if entity.resolution_status != ResolutionStatus.RESOLVED
    ]

    if any(
        entity.resolution_status == ResolutionStatus.AMBIGUOUS
        for entity in unresolved
    ):
        status = QueryStatus.AMBIGUOUS
    elif unresolved:
        status = QueryStatus.UNRESOLVED
    else:
        status = query.status

    return StructuredQuery(
        status=status,
        original_query=query.original_query,
        operation=query.operation,
        requested_entity_type=query.requested_entity_type,
        path_requirements=list(getattr(query, "path_requirements", []) or []),
        entities=ordered,
        filters=query.filters,
        confidence=query.confidence,
        clarification_question=query.clarification_question,
        notes=list(query.notes),
        intent=query.intent,
    )

