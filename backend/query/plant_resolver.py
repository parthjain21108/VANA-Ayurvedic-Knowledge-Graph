from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .models import EntityCandidate, EntityType, ResolutionStatus


GBIF_MATCH_URL = "https://api.gbif.org/v2/species/match"
GBIF_SEARCH_URL = "https://api.gbif.org/v1/species/search"
GBIF_VERNACULAR_URL = "https://api.gbif.org/v1/species/{key}/vernacularNames"
DEFAULT_GEMINI_MODEL = os.getenv(
    "PLANT_RESOLVER_MODEL",
    "gemini-3.5-flash-lite",
)

# Deterministic graph guard rails.
# These are intentionally strict: fuzzy matching is used only for
# likely spelling mistakes, not as a general semantic resolver.
FUZZY_GRAPH_MATCH_THRESHOLD = 0.90
FUZZY_GRAPH_MATCH_MARGIN = 0.05
FUZZY_GRAPH_MIN_TERM_LENGTH = 5


@dataclass
class PlantResolution:
    user_term: str
    resolution_status: ResolutionStatus
    scientific_name: str | None = None
    candidates: list[str] | None = None
    source: str | None = None
    confidence: float | None = None


class PlantResolutionError(RuntimeError):
    """Raised when safe plant-name resolution cannot be completed."""


def _clean(value: str) -> str:
    return " ".join(str(value).strip().split())


def _normalize_alias(value: str) -> str:
    """Normalize a human-facing plant name for exact alias comparison."""
    value = _clean(value).casefold()
    value = re.sub(r"[\u2010\u2011\u2012\u2013\u2014-]+", " ", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _canonical_species_name(value: str, *, rank: str | None = None) -> str:
    """Return the stable species identity used by the graph.

    Taxonomy services are inconsistent about botanical author citations. For
    example, the same species may appear as ``Curcuma longa`` in one response
    and ``Curcuma longa L.`` in another. Those must be treated as ONE taxon
    before ambiguity is calculated.

    We only collapse to genus + species when GBIF explicitly says SPECIES, or
    when the third token is clearly an abbreviated botanical author citation.
    Subspecies/variety/form names are therefore not silently collapsed.
    """
    text = _clean(value)
    if not text:
        return ""

    parts = text.split()
    if len(parts) < 2:
        return text

    normalized_rank = str(rank or "").strip().upper()

    # Species-ranked GBIF results are safe to reduce to their binomial (or
    # hybrid trinomial) identity.
    if normalized_rank == "SPECIES":
        if len(parts) >= 3 and parts[1] in {"x", "×"}:
            return " ".join(parts[:3])
        return " ".join(parts[:2])

    # Some GBIF responses omit rank. Detect an author abbreviation instead of
    # assuming every three-token name is a species. Examples: ``L.``,
    # ``Mill.``, ``DC.``, ``Hook.f.``. Do not collapse rank markers such as
    # ``subsp.``, ``var.``, or ``f.``.
    if not normalized_rank and len(parts) >= 3:
        author_start = 3 if len(parts) >= 3 and parts[1] in {"x", "×"} else 2
        if len(parts) > author_start:
            third = parts[author_start]
            rank_markers = {"subsp.", "ssp.", "var.", "forma", "f."}
            if third.casefold() not in rank_markers and "." in third:
                return " ".join(parts[:author_start + 1 - 1])

    return text


def _graph_alias_query() -> str:
    # common_names in the current graph is one comma-separated string.
    return """
MATCH (p:`Plant Name`)
WHERE toLower(toString(p.scientific_name)) = toLower($term)
   OR any(
        v IN split(coalesce(toString(p.common_names), ''), ',')
        WHERE toLower(trim(v)) = toLower($term)
      )
RETURN p.scientific_name AS scientific_name
LIMIT $limit
"""


def resolve_from_graph(
    user_term: str,
    session: Any,
    *,
    max_candidates: int = 5,
) -> PlantResolution:
    """Resolve a plant name against the current Neo4j graph only."""
    term = _clean(user_term)
    if not term:
        raise ValueError("user_term must not be empty.")

    rows = list(
        session.run(
            _graph_alias_query(),
            term=term,
            limit=max_candidates,
        )
    )

    names: list[str] = []
    for row in rows:
        name = (
            row.get("scientific_name")
            if hasattr(row, "get")
            else row["scientific_name"]
        )
        if name:
            names.append(str(name))

    names = list(dict.fromkeys(names))

    if len(names) == 1:
        return PlantResolution(
            user_term=term,
            resolution_status=ResolutionStatus.RESOLVED,
            scientific_name=names[0],
            source="neo4j_alias",
            confidence=1.0,
        )

    if len(names) > 1:
        return PlantResolution(
            user_term=term,
            resolution_status=ResolutionStatus.AMBIGUOUS,
            candidates=names,
            source="neo4j_alias",
        )

    return PlantResolution(
        user_term=term,
        resolution_status=ResolutionStatus.UNRESOLVED,
        source="neo4j_alias",
    )


def _gbif_json_get(
    url: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise PlantResolutionError(f"GBIF lookup failed: {exc}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PlantResolutionError("GBIF returned invalid JSON.") from exc

    if not isinstance(data, dict):
        raise PlantResolutionError("GBIF returned an invalid JSON object.")

    return data


def _gbif_common_name_search(
    user_term: str,
    *,
    gbif_get: Callable[[str, float], dict[str, Any]],
    timeout: float = 15.0,
    max_results: int = 20,
) -> PlantResolution:
    """Find an exact vernacular-name match in GBIF taxonomy.

    GBIF species search is used to find likely taxa, then each result's
    vernacular names are checked for an exact normalized match. This avoids
    passing a model-generated scientific name downstream without validation.
    """
    term = _clean(user_term)
    normalized = _normalize_alias(term)

    search_url = (
        f"{GBIF_SEARCH_URL}?"
        f"{urllib.parse.urlencode({'q': term, 'rank': 'SPECIES', 'limit': max_results})}"
    )
    search_data = gbif_get(search_url, timeout)
    results = search_data.get("results") or []

    matches: dict[str, float] = {}

    for item in results:
        if not isinstance(item, dict):
            continue

        key = item.get("key")
        scientific_name = _clean(
            str(
                item.get("scientificName")
                or item.get("canonicalName")
                or ""
            )
        )

        if not key or not scientific_name:
            continue

        vernacular_url = GBIF_VERNACULAR_URL.format(
            key=urllib.parse.quote(str(key), safe="")
        )
        vernacular_data = gbif_get(vernacular_url, timeout)
        vernaculars = vernacular_data.get("results") or []

        for vernacular in vernaculars:
            if not isinstance(vernacular, dict):
                continue

            name = _clean(str(vernacular.get("vernacularName") or ""))
            if name and _normalize_alias(name) == normalized:
                canonical_name = _canonical_species_name(
                    scientific_name,
                    rank=str(item.get("rank") or item.get("taxonRank") or "SPECIES"),
                )
                if canonical_name:
                    matches[canonical_name] = 1.0
                break

    if len(matches) == 1:
        name = next(iter(matches))
        return PlantResolution(
            user_term=term,
            resolution_status=ResolutionStatus.RESOLVED,
            scientific_name=name,
            source="gbif_vernacular_exact",
            confidence=1.0,
        )

    if len(matches) > 1:
        return PlantResolution(
            user_term=term,
            resolution_status=ResolutionStatus.AMBIGUOUS,
            candidates=sorted(matches),
            source="gbif_vernacular_exact",
        )

    return PlantResolution(
        user_term=term,
        resolution_status=ResolutionStatus.UNRESOLVED,
        source="gbif_vernacular_exact",
    )


def _gemini_prompt(user_term: str) -> str:
    return f"""
Identify the most likely scientific name(s) for this plant name.

PLANT NAME:
{user_term}

Rules:
- Return only botanical scientific names.
- Prefer species-level taxa.
- If genuinely ambiguous, return all plausible candidates.
- Do not provide explanations.
- Return JSON exactly in this shape:
{{
  "candidates": [
    {{"scientific_name": "...", "confidence": 0.0}}
  ]
}}
"""


def _call_gemini(
    user_term: str,
    api_key: str,
    *,
    model: str,
) -> list[dict[str, Any]]:
    try:
        from google import genai
    except ImportError as exc:
        raise PlantResolutionError(
            "Install google-genai before resolving an unknown plant name."
        ) from exc

    client = genai.Client(api_key=api_key)

    try:
        response = client.models.generate_content(
            model=model,
            contents=_gemini_prompt(user_term),
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema={
                    "type": "object",
                    "properties": {
                        "candidates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "scientific_name": {"type": "string"},
                                    "confidence": {"type": "number"},
                                },
                                "required": [
                                    "scientific_name",
                                    "confidence",
                                ],
                            },
                        }
                    },
                    "required": ["candidates"],
                },
                temperature=0,
            ),
        )
    except Exception as exc:
        raise PlantResolutionError(
            f"Gemini plant-name resolution failed: {exc}"
        ) from exc

    raw = getattr(response, "text", None)
    if not isinstance(raw, str) or not raw.strip():
        raise PlantResolutionError(
            "Gemini returned no plant-name candidates."
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlantResolutionError(
            "Gemini returned invalid plant-name JSON."
        ) from exc

    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        raise PlantResolutionError(
            "Gemini plant-name output has no candidates list."
        )

    cleaned: list[dict[str, Any]] = []

    for item in candidates[:5]:
        if not isinstance(item, dict):
            continue

        name = _clean(item.get("scientific_name", ""))

        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        if name:
            cleaned.append(
                {
                    "scientific_name": name,
                    "confidence": max(0.0, min(1.0, confidence)),
                }
            )

    return cleaned


def _gbif_match(
    scientific_name: str,
    *,
    gbif_get: Callable[[str, float], dict[str, Any]],
    timeout: float = 15.0,
) -> dict[str, Any]:
    url = (
        f"{GBIF_MATCH_URL}?"
        f"{urllib.parse.urlencode({'scientificName': scientific_name})}"
    )
    return gbif_get(url, timeout)


def resolve_new_common_name(
    user_term: str,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_GEMINI_MODEL,
    gbif_get: Callable[[str, float], dict[str, Any]] | None = None,
    gemini_resolver: Callable[..., list[dict[str, Any]]] | None = None,
) -> PlantResolution:
    """Resolve a genuinely new common plant name directly with Gemini.

    GBIF is intentionally not used to validate, rename, or canonicalize the
    Gemini result. The highest-confidence Gemini scientific name is preserved
    exactly as returned by the resolver. ``gbif_get`` remains in the function
    signature for backward compatibility with existing callers/tests.
    """
    del gbif_get

    term = _clean(user_term)
    if not term:
        raise ValueError("user_term must not be empty.")

    key = api_key or os.getenv("GEMINI_API_KEY")

    if not key and gemini_resolver is None:
        return PlantResolution(
            user_term=term,
            resolution_status=ResolutionStatus.UNRESOLVED,
            source="gemini_direct",
        )

    candidates = (
        gemini_resolver(term, api_key=key, model=model)
        if gemini_resolver is not None
        else _call_gemini(term, key, model=model)
    )

    valid_candidates: list[tuple[str, float]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        name = _clean(str(candidate.get("scientific_name", "")))
        if not name:
            continue

        try:
            confidence = float(candidate.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        valid_candidates.append((name, max(0.0, min(1.0, confidence))))

    if not valid_candidates:
        return PlantResolution(
            user_term=term,
            resolution_status=ResolutionStatus.UNRESOLVED,
            source="gemini_direct",
        )

    top_name, top_confidence = max(
        valid_candidates,
        key=lambda item: item[1],
    )

    return PlantResolution(
        user_term=term,
        resolution_status=ResolutionStatus.RESOLVED,
        scientific_name=top_name,
        source="gemini_direct",
        confidence=top_confidence,
    )


def _graph_semantic_candidates(
    user_term: str,
    session: Any,
    *,
    max_candidates: int = 25,
) -> list[str]:
    """
    Rank EXISTING Plant Name records for natural-language plant mentions.

    Candidate generation intentionally retrieves actual graph records rather
    than inventing taxonomy. Matching is broad at the token level, then the
    final semantic choice is constrained to the ranked graph candidates.
    """
    cleaned = _clean(user_term)
    raw_tokens = [
        token
        for token in _normalize_alias(cleaned).split()
        if len(token) >= 3
    ]

    target_tokens: set[str] = set(raw_tokens)
    for token in raw_tokens:
        if len(token) > 4 and token.endswith("ies"):
            target_tokens.add(token[:-3] + "y")
        if len(token) > 4 and token.endswith("ves"):
            target_tokens.add(token[:-3] + "f")
        if len(token) > 3 and token.endswith("s"):
            target_tokens.add(token[:-1])
    if not target_tokens:
        return []

    query = """
MATCH (p:`Plant Name`)
RETURN p.scientific_name AS scientific_name,
       p.common_names AS common_names
ORDER BY toLower(toString(p.scientific_name))
"""

    try:
        rows = list(session.run(query))
    except Exception as exc:
        raise PlantResolutionError(
            "Could not retrieve Plant Name candidate set from Neo4j."
        ) from exc

    scored: list[tuple[float, str]] = []

    for row in rows:
        try:
            scientific_name = row.get("scientific_name")
            common_names = row.get("common_names")
        except AttributeError:
            scientific_name = row["scientific_name"]
            common_names = row["common_names"]

        name = _clean(str(scientific_name or ""))
        common = _clean(str(common_names or ""))
        if not name:
            continue

        searchable = _normalize_alias(f"{name} {common}")
        searchable_tokens = set(searchable.split())

        overlap = len(target_tokens & searchable_tokens) / max(
            1,
            len(target_tokens),
        )

        substring_hits = sum(
            1
            for token in target_tokens
            if token in searchable
        ) / max(1, len(target_tokens))

        sequence = SequenceMatcher(
            None,
            _normalize_alias(cleaned),
            searchable,
        ).ratio()

        # Token overlap is the main recall signal. Substring hits preserve
        # useful matching for names stored in comma-separated common_names.
        score = (
            0.55 * overlap
            + 0.30 * substring_hits
            + 0.15 * sequence
        )

        if score > 0:
            scored.append((score, name))

    scored.sort(key=lambda item: (-item[0], item[1].casefold()))

    return list(
        dict.fromkeys(
            name
            for _, name in scored[:max_candidates]
        )
    )


def _gemini_choose_graph_plant(
    user_term: str,
    candidates: list[str],
    *,
    api_key: str,
    model: str,
) -> tuple[str | None, float | None]:
    """Choose only among real Neo4j scientific names."""
    if not candidates:
        return None, None

    try:
        from google import genai
        from pydantic import BaseModel
    except ImportError as exc:
        raise PlantResolutionError(
            "Gemini graph-candidate resolution requires google-genai and pydantic."
        ) from exc

    class Choice(BaseModel):
        index: int | None = None
        confidence: float | None = None

    candidate_lines = "\n".join(
        f"{i}: {name}" for i, name in enumerate(candidates)
    )

    prompt = f"""
Resolve this plant mention against EXISTING plant records from a Neo4j knowledge graph.

User mention:
{user_term}

Select the best matching scientific name from the candidates below.
Do not invent a plant. Do not invent a scientific name. Do not return an
entry that is not in the candidate list. Common-name wording and descriptive
phrasing are allowed (for example, a regional/common name can refer to a
scientific name), but choose only from the provided records.
Return null when none is defensible.

Candidates:
{candidate_lines}
""".strip()

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Choice,
            temperature=0,
        ),
    )

    raw = getattr(response, "text", "")
    if hasattr(Choice, "model_validate_json"):
        choice = Choice.model_validate_json(raw)
    else:
        choice = Choice.parse_raw(raw)

    if choice.index is None:
        return None, None
    if not isinstance(choice.index, int) or not 0 <= choice.index < len(candidates):
        raise PlantResolutionError("Gemini returned an invalid plant candidate index.")

    confidence = float(choice.confidence) if choice.confidence is not None else 0.85
    confidence = max(0.0, min(1.0, confidence))
    return candidates[choice.index], confidence


_COMMON_NAME_WORDS = {
    "african",
    "american",
    "asian",
    "basil",
    "banana",
    "babool",
    "babul",
    "bitter",
    "black",
    "chinese",
    "common",
    "curry",
    "drumstick",
    "ginger",
    "green",
    "holy",
    "indian",
    "japanese",
    "mango",
    "neem",
    "orange",
    "pepper",
    "plant",
    "red",
    "sweet",
    "tree",
    "turmeric",
    "water",
    "white",
    "wild",
}


def _graph_fuzzy_match(
    user_term: str,
    session: Any,
    *,
    threshold: float = FUZZY_GRAPH_MATCH_THRESHOLD,
    margin: float = FUZZY_GRAPH_MATCH_MARGIN,
) -> tuple[str, float] | None:
    """
    Find a very-close EXISTING Plant Name record.

    This is a typo guard, not a general semantic resolver.
    It compares the user term against scientific names and stored
    common-name aliases already present in Neo4j.
    """
    normalized_term = _normalize_alias(user_term)

    if len(normalized_term) < FUZZY_GRAPH_MIN_TERM_LENGTH:
        return None

    query = """
MATCH (p:`Plant Name`)
RETURN p.scientific_name AS scientific_name,
       p.common_names AS common_names
"""

    try:
        rows = list(session.run(query))
    except Exception as exc:
        raise PlantResolutionError(
            "Could not retrieve Plant Name records for fuzzy guard-rail matching."
        ) from exc

    best_by_scientific: dict[str, float] = {}

    for row in rows:
        try:
            scientific_name = row.get("scientific_name")
            common_names = row.get("common_names")
        except AttributeError:
            scientific_name = row["scientific_name"]
            common_names = row["common_names"]

        scientific = _clean(str(scientific_name or ""))
        if not scientific:
            continue

        aliases = [scientific]

        if isinstance(common_names, (list, tuple)):
            aliases.extend(
                _clean(str(value))
                for value in common_names
                if _clean(str(value))
            )
        else:
            aliases.extend(
                _clean(part)
                for part in str(common_names or "").split(",")
                if _clean(part)
            )

        best_score = 0.0

        for alias in aliases:
            normalized_alias = _normalize_alias(alias)

            if len(normalized_alias) < FUZZY_GRAPH_MIN_TERM_LENGTH:
                continue

            # Do not match a short term against a much longer unrelated name.
            if abs(len(normalized_term) - len(normalized_alias)) > 2:
                continue

            score = SequenceMatcher(
                None,
                normalized_term,
                normalized_alias,
            ).ratio()

            if score > best_score:
                best_score = score

        if best_score > 0:
            previous = best_by_scientific.get(scientific, 0.0)
            best_by_scientific[scientific] = max(previous, best_score)

    ranked = sorted(
        best_by_scientific.items(),
        key=lambda item: (-item[1], item[0].casefold()),
    )

    if not ranked:
        return None

    best_name, best_score = ranked[0]

    if best_score < threshold:
        return None

    # Refuse fuzzy resolution when two graph plants are nearly tied.
    if len(ranked) > 1:
        second_score = ranked[1][1]
        if best_score - second_score < margin:
            return None

    return best_name, best_score


def _looks_like_scientific_name(value: str) -> bool:
    """Return True for an explicitly formatted botanical binomial/trinomial."""
    text = _clean(value)
    if not text:
        return False

    parts = text.split()
    if len(parts) < 2:
        return False

    first, second = parts[0], parts[1]
    if not re.fullmatch(r"[A-Z][A-Za-z-]*", first):
        return False
    if not re.fullmatch(r"[a-z][a-z-]*", second):
        return False

    # Common-name phrases can also use a capitalized first word, e.g.
    # "Indian neem". Keep those on the normal common-name path.
    if first.casefold() in _COMMON_NAME_WORDS:
        return False
    if second.casefold() in _COMMON_NAME_WORDS:
        return False

    # Require a plausible genus/species shape. This deliberately targets
    # explicit scientific-name formatting while leaving ordinary common names
    # (for example, "black pepper" or "neem tree") on the existing path.
    return len(first) >= 3 and len(second) >= 3


def _resolve_exact_scientific_name(
    user_term: str,
    session: Any,
    *,
    max_candidates: int = 5,
) -> PlantResolution:
    """Resolve only against the graph's scientific_name field."""
    query = """
MATCH (p:`Plant Name`)
WHERE toLower(toString(p.scientific_name)) = toLower($term)
RETURN p.scientific_name AS scientific_name
LIMIT $limit
"""

    rows = list(
        session.run(
            query,
            term=_clean(user_term),
            limit=max_candidates,
        )
    )

    names: list[str] = []
    for row in rows:
        name = (
            row.get("scientific_name")
            if hasattr(row, "get")
            else row["scientific_name"]
        )
        if name:
            names.append(str(name))

    names = list(dict.fromkeys(names))

    if len(names) == 1:
        return PlantResolution(
            user_term=_clean(user_term),
            resolution_status=ResolutionStatus.RESOLVED,
            scientific_name=names[0],
            source="neo4j_scientific_name",
            confidence=1.0,
        )

    if len(names) > 1:
        return PlantResolution(
            user_term=_clean(user_term),
            resolution_status=ResolutionStatus.AMBIGUOUS,
            candidates=names,
            source="neo4j_scientific_name",
        )

    return PlantResolution(
        user_term=_clean(user_term),
        resolution_status=ResolutionStatus.UNRESOLVED,
        source="neo4j_scientific_name",
    )


COMMON_NAME_OVERRIDES = {
    "tulsi": "Ocimum tenuiflorum",
    "acacia": "Acacia leucophloea",
    "acacias": "Acacia leucophloea",
    "vachellia leucophloea": "Acacia leucophloea",
}


def resolve_plant(
    entity: EntityCandidate,
    session: Any,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_GEMINI_MODEL,
    gbif_get: Callable[[str, float], dict[str, Any]] | None = None,
    gemini_resolver: Callable[..., list[dict[str, Any]]] | None = None,
) -> EntityCandidate:
    """
    Resolve a plant safely.

    Resolution order:
      1. Exact graph scientific/common-name match.
      2. Semantic choice among existing graph Plant Name records.
      3. Existing external GBIF/Gemini fallback for a genuinely new plant.

    A graph candidate always remains the preferred source of truth.
    """
    if entity.entity_type != EntityType.PLANT:
        raise ValueError("resolve_plant requires EntityType.PLANT.")

    # Explicit scientific names are authoritative user input. Always try the
    # graph's scientific_name field FIRST, regardless of capitalization. This
    # prevents a scientific name from accidentally matching another plant
    # through common_names (for example, Acacia leucophloea -> Vachellia
    # leucophloea).

    normalized_term = _normalize_alias(entity.user_term)

    override = COMMON_NAME_OVERRIDES.get(normalized_term)

    if override:
        return EntityCandidate(
            entity_type=EntityType.PLANT,
            user_term=entity.user_term,
            resolution_status=ResolutionStatus.RESOLVED,
            canonical_value=override,
            canonical_id=override,
            candidates=[],
            aliases=[entity.user_term],
            confidence=1.0,
        )

    exact_scientific = _resolve_exact_scientific_name(
        entity.user_term,
        session,
    )
    if exact_scientific.resolution_status != ResolutionStatus.UNRESOLVED:
        return EntityCandidate(
            entity_type=EntityType.PLANT,
            user_term=entity.user_term,
            resolution_status=exact_scientific.resolution_status,
            canonical_value=exact_scientific.scientific_name,
            canonical_id=exact_scientific.scientific_name,
            candidates=list(exact_scientific.candidates or []),
            aliases=list(entity.aliases),
            confidence=exact_scientific.confidence,
        )

    # If the user explicitly supplied a botanical-looking binomial but it is
    # not yet in Neo4j, preserve that exact name for conditional ingestion. Do
    # NOT semantic-rerank it, synonymize it, or validate/rename it through an
    # external taxonomy service.
    # ---------------------------------------------------------
    # Deterministic typo guard.
    #
    # Before treating a term as a new plant, check whether it is
    # a very-close spelling of an EXISTING graph plant.
    #
    # Example:
    #   Hibicus -> Hibiscus
    #   Hibiscus rosas-sinensis -> Hibiscus rosa-sinensis
    # ---------------------------------------------------------
    fuzzy_match = _graph_fuzzy_match(
        entity.user_term,
        session,
    )

    if fuzzy_match is not None:
        fuzzy_name, fuzzy_confidence = fuzzy_match
        return EntityCandidate(
            entity_type=EntityType.PLANT,
            user_term=entity.user_term,
            resolution_status=ResolutionStatus.RESOLVED,
            canonical_value=fuzzy_name,
            canonical_id=fuzzy_name,
            candidates=[],
            aliases=list(entity.aliases),
            confidence=fuzzy_confidence,
        )

    if _looks_like_scientific_name(entity.user_term):
        exact_name = _clean(entity.user_term)
        return EntityCandidate(
            entity_type=EntityType.PLANT,
            user_term=entity.user_term,
            resolution_status=ResolutionStatus.RESOLVED,
            canonical_value=exact_name,
            canonical_id=exact_name,
            candidates=[],
            aliases=list(entity.aliases),
            confidence=1.0,
        )

    graph = resolve_from_graph(entity.user_term, session)

    if graph.resolution_status == ResolutionStatus.RESOLVED:
        return EntityCandidate(
            entity_type=EntityType.PLANT,
            user_term=entity.user_term,
            resolution_status=ResolutionStatus.RESOLVED,
            canonical_value=graph.scientific_name,
            canonical_id=graph.scientific_name,
            aliases=list(entity.aliases),
            candidates=[],
            confidence=graph.confidence,
        )

    if graph.resolution_status == ResolutionStatus.AMBIGUOUS:
        return EntityCandidate(
            entity_type=EntityType.PLANT,
            user_term=entity.user_term,
            resolution_status=ResolutionStatus.AMBIGUOUS,
            canonical_value=None,
            canonical_id=None,
            candidates=list(graph.candidates or []),
            aliases=list(entity.aliases),
            confidence=graph.confidence,
        )

    # ---------------------------------------------------------
    # Graph-first semantic recall.
    #
    # This specifically prevents phrases such as "Indian neem" from
    # unnecessarily leaving the graph when "Neem" is stored in common_names.
    # Gemini may only choose among real graph Plant Name records.
    # ---------------------------------------------------------
    graph_candidates = _graph_semantic_candidates(
        entity.user_term,
        session,
        max_candidates=25,
    )

    key = api_key or os.getenv("GEMINI_API_KEY")

    if graph_candidates:
        if key:
            try:
                chosen, confidence = _gemini_choose_graph_plant(
                    entity.user_term,
                    graph_candidates,
                    api_key=key,
                    model=model,
                )
            except Exception as exc:
                raise PlantResolutionError(
                    f"Gemini graph-plant resolution failed for "
                    f"'{entity.user_term}': {exc}"
                ) from exc

            if chosen:
                return EntityCandidate(
                    entity_type=EntityType.PLANT,
                    user_term=entity.user_term,
                    resolution_status=ResolutionStatus.RESOLVED,
                    canonical_value=chosen,
                    canonical_id=chosen,
                    candidates=[],
                    aliases=list(entity.aliases),
                    confidence=confidence,
                )

    # ---------------------------------------------------------
    # Only now use the existing external resolution pipeline for plants that
    # are not safely identifiable from the current graph.
    # ---------------------------------------------------------
    external = resolve_new_common_name(
        entity.user_term,
        api_key=api_key,
        model=model,
        gbif_get=gbif_get,
        gemini_resolver=gemini_resolver,
    )

    # ---------------------------------------------------------
    # Deterministic post-external guard.
    #
    # If external/model resolution returns a name that is actually
    # a tiny typo of an existing graph plant, use the graph record
    # instead of allowing a duplicate plant to be ingested.
    # ---------------------------------------------------------
    if external.scientific_name:
        fuzzy_external = _graph_fuzzy_match(
            external.scientific_name,
            session,
        )

        if fuzzy_external is not None:
            fuzzy_name, fuzzy_confidence = fuzzy_external
            return EntityCandidate(
                entity_type=EntityType.PLANT,
                user_term=entity.user_term,
                resolution_status=ResolutionStatus.RESOLVED,
                canonical_value=fuzzy_name,
                canonical_id=fuzzy_name,
                candidates=[],
                aliases=list(entity.aliases),
                confidence=fuzzy_confidence,
            )

    return EntityCandidate(
        entity_type=EntityType.PLANT,
        user_term=entity.user_term,
        resolution_status=external.resolution_status,
        canonical_value=external.scientific_name,
        canonical_id=external.scientific_name,
        candidates=list(external.candidates or []),
        aliases=list(entity.aliases),
        confidence=external.confidence,
    )