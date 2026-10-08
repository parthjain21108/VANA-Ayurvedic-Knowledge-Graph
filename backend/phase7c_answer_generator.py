from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_MODEL = os.getenv(
    "LLM_MODEL",
    os.getenv("NLP_MODEL", "gemini-3.5-flash-lite"),
)


class AnswerGenerationError(RuntimeError):
    """Raised when a grounded Phase 7C answer cannot be generated safely."""


GROUNDING_POLICY = [
    "Use only facts present in the supplied Phase 7B context.",
    "Do not invent entities, relationships, measurements, mechanisms, efficacy, or citations.",
    "Do not infer a stronger biological or clinical claim than the graph explicitly represents.",
    "Describe graph associations as associations; do not turn them into treatment or causation claims.",
    "Preserve measurement values, units, relation operators, and confidence tiers exactly as supplied when mentioned.",
    "When evidence is absent or insufficient, say that the supplied knowledge-graph evidence does not establish it.",
    "Do not claim to have read a paper or source document unless its actual content is supplied.",
    "Do not use outside knowledge to fill gaps in the graph evidence.",
    "Use deterministic statistics supplied by the backend; do not invent, estimate, or recalculate unsupported counts.",
    "For plant-use queries, organize the answer by plant part when plant-part evidence is present.",
    "Start with a concise plant/subject overview before discussing detailed associations.",
    "Do not imply that every graph association is a traditional-use claim, experimental finding, or clinically proven indication unless the evidence explicitly says so.",
]


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnswerGenerationError(f"{path} must be an object.")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise AnswerGenerationError(f"{path} must be an array.")
    return value


def _require_string(value: Any, path: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AnswerGenerationError(f"{path} must be a non-empty string.")
    return value


def _validate_context(context: Any) -> dict[str, Any]:
    obj = _require_dict(context, "llm_context")
    _require_string(obj.get("contract_version"), "llm_context.contract_version")
    _require_string(obj.get("user_query", ""), "llm_context.user_query", allow_empty=True)
    _require_dict(obj.get("query_context"), "llm_context.query_context")
    summary = _require_dict(obj.get("summary"), "llm_context.summary")
    facts = _require_list(obj.get("facts"), "llm_context.facts")
    stats = _require_dict(obj.get("context_stats"), "llm_context.context_stats")

    if stats.get("fact_count") != len(facts):
        raise AnswerGenerationError(
            f"llm_context.context_stats.fact_count={stats.get('fact_count')} "
            f"!= len(facts)={len(facts)}."
        )

    if summary.get("fact_count") != len(facts):
        raise AnswerGenerationError(
            f"llm_context.summary.fact_count={summary.get('fact_count')} "
            f"!= len(facts)={len(facts)}."
        )

    for index, fact in enumerate(facts):
        fact_obj = _require_dict(fact, f"llm_context.facts[{index}]")
        if fact_obj.get("row_index") != index:
            raise AnswerGenerationError(
                f"llm_context.facts[{index}].row_index="
                f"{fact_obj.get('row_index')} does not match position {index}."
            )
        _require_dict(fact_obj.get("entities"), f"llm_context.facts[{index}].entities")
        _require_dict(fact_obj.get("path"), f"llm_context.facts[{index}].path")
        _require_list(fact_obj["path"].get("nodes"), f"llm_context.facts[{index}].path.nodes")
        _require_list(
            fact_obj["path"].get("relationships"),
            f"llm_context.facts[{index}].path.relationships",
        )

    _require_string(obj.get("context_text"), "llm_context.context_text")

    try:
        json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise AnswerGenerationError(f"7B context is not JSON serializable: {exc}") from exc

    return obj


def _clean_name(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _fact_entities(fact: dict[str, Any]) -> dict[str, Any]:
    entities = fact.get("entities") or {}
    return entities if isinstance(entities, dict) else {}


def _deterministic_statistics(context: dict[str, Any]) -> dict[str, Any]:
    summary = context["summary"]
    return {
        "row_count": summary.get("row_count"),
        "fact_count": summary.get("fact_count"),
        "node_count": summary.get("node_count"),
        "relationship_count": summary.get("relationship_count"),
        "node_counts_by_type": summary.get("node_counts_by_type", {}),
        "relationship_counts_by_type": summary.get("relationship_counts_by_type", {}),
        "bioactivity_counts_by_standard_type": summary.get(
            "bioactivity_counts_by_standard_type", {}
        ),
        "bioactivity_counts_by_confidence": summary.get(
            "bioactivity_counts_by_confidence", {}
        ),
    }


def _deterministic_subject_profile(context: dict[str, Any]) -> dict[str, Any]:
    """Extract compact, backend-derived organization hints for the LLM."""
    facts = context["facts"]

    plant_names: set[str] = set()
    subject_names: set[str] = set()
    plant_part_counts: defaultdict[str, int] = defaultdict(int)
    plant_part_uses: defaultdict[str, set[str]] = defaultdict(set)
    use_names: set[str] = set()

    for fact in facts:
        entities = _fact_entities(fact)

        for key, value in entities.items():
            name = _clean_name(value)
            if not name:
                continue

            key_lower = str(key).lower()
            if key_lower == "plant":
                plant_names.add(name)
            if key_lower in {
                "plant",
                "phytochemical",
                "therapeutic_use",
                "target",
                "bioactivity",
                "document",
            }:
                subject_names.add(name)
            if key_lower == "therapeutic_use":
                use_names.add(name)

        plant_part = _clean_name(entities.get("plant_part"))
        therapeutic_use = _clean_name(entities.get("therapeutic_use"))

        if plant_part:
            plant_part_counts[plant_part] += 1
            if therapeutic_use:
                plant_part_uses[plant_part].add(therapeutic_use)

    return {
        "plants": sorted(plant_names),
        "subject_candidates_from_facts": sorted(subject_names),
        "plant_part_fact_counts": dict(sorted(plant_part_counts.items())),
        "plant_part_use_counts": {
            part: len(uses) for part, uses in sorted(plant_part_uses.items())
        },
        "plant_part_associations": {
            part: sorted(uses)
            for part, uses in sorted(plant_part_uses.items())
        },
        "all_therapeutic_use_names": sorted(use_names),
    }


def _response_model():
    from pydantic import BaseModel, Field

    class AnswerResponse(BaseModel):
        # Gemini generates narrative fields only.
        # All graph-derived structured evidence fields are reconstructed by
        # the backend after Gemini returns.
        title: str
        overview: str
        plant_facts: list[str] = Field(default_factory=list)
        key_takeaways: list[str] = Field(default_factory=list)
        interpretation: str
        caveats: list[str] = Field(default_factory=list)
        unsupported_claims: list[str] = Field(default_factory=list)
        grounded: bool

    return AnswerResponse


def build_grounded_prompt(context: Any) -> str:
    obj = _validate_context(context)
    stats = _deterministic_statistics(obj)
    profile = _deterministic_subject_profile(obj)

    output_shape = {
        "title": "Human-friendly subject title; for a plant include scientific name.",
        "overview": "3-5 sentence plain-language overview of the supplied evidence.",
        "plant_facts": ["2-6 directly supported facts about the subject/plant."],
        "key_takeaways": ["3-6 concise takeaways based only on the supplied evidence."],
        "interpretation": (
            "Explain what the graph establishes and distinguish graph "
            "association from efficacy or treatment."
        ),
        "caveats": ["Important evidence limitations."],
        "unsupported_claims": [],
        "grounded": True,
    }

    style_rules = [
        "Write for a normal user, not like a database dump.",
        "Start with the subject/plant name, scientific name when present, and a clear overview.",
        "For a plant, give useful directly supported plant facts before discussing associations.",
        "For plant-use queries, discuss plant parts and their evidence coverage using ONLY the deterministic evidence supplied below.",
        "Do not generate, rename, omit, count, or reorder canonical plant-part fields. The backend adds those fields after generation.",
        "Do not generate therapeutic-use names, counts, identifiers, measurements, node IDs, relationship IDs, or other canonical graph fields as structured data.",
        "Use wording such as 'the knowledge graph associates', 'the graph contains', or 'the evidence records'.",
        "Never turn an association into a claim that a plant treats, cures, prevents, or is effective for a condition.",
        "Do not invent mechanisms, dosage, safety advice, traditional-use explanations, clinical recommendations, or citations.",
        "Use deterministic statistics exactly as supplied by the backend.",
    ]

    return (
        "You are the detailed, user-friendly answer-generation layer of a grounded knowledge-graph system.\n"
        "Answer the user's question ONLY from the supplied Phase 7B context.\n\n"
        "STRICT GROUNDING POLICY:\n"
        + "\n".join(f"- {rule}" for rule in GROUNDING_POLICY)
        + "\n\n"
        "REQUIRED ANSWER STYLE:\n"
        + "\n".join(f"- {rule}" for rule in style_rules)
        + "\n\n"
        "IMPORTANT OUTPUT BOUNDARY:\n"
        "The backend, not Gemini, is authoritative for all graph-derived "
        "structured evidence. Do not return plant_part, fact_count, "
        "unique_use_count, associated_uses, or representative_uses fields. "
        "The backend will construct those deterministically from Phase 7B evidence.\n\n"
        "REQUIRED JSON SHAPE:\n"
        + json.dumps(output_shape, ensure_ascii=False, indent=2)
        + "\n\n"
        "AUTHORITATIVE DETERMINISTIC STATISTICS (DO NOT CHANGE):\n"
        + json.dumps(stats, ensure_ascii=False, indent=2)
        + "\n\n"
        "AUTHORITATIVE PLANT-PART EVIDENCE (DO NOT RECREATE AS OUTPUT FIELDS):\n"
        + json.dumps(profile, ensure_ascii=False, indent=2)
        + "\n\n"
        "SUPPLIED PHASE 7B CONTEXT:\n"
        + obj["context_text"]
    )


def _validate_generated_answer(
    result: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """
    Keep Gemini authoritative only for narrative fields.

    All graph-derived structured fields are reconstructed deterministically
    from the supplied Phase 7B evidence. This prevents model-generated
    plant-part names, counts, or use lists from causing false validation
    failures or evidence drift.
    """
    profile = _deterministic_subject_profile(context)

    validated_sections = []
    for part, uses in sorted(
        profile["plant_part_associations"].items(),
        key=lambda item: (-len(item[1]), item[0].casefold()),
    ):
        fact_count = profile["plant_part_fact_counts"].get(part, 0)
        use_list = list(uses)
        use_count = len(use_list)

        if use_list:
            explanation = (
                f"The knowledge graph records {fact_count} association"
                f"{'s' if fact_count != 1 else ''} for the {part}, "
                f"covering {use_count} distinct therapeutic use"
                f"{'s' if use_count != 1 else ''}."
            )
        else:
            explanation = (
                f"The knowledge graph records {fact_count} association"
                f"{'s' if fact_count != 1 else ''} for the {part}."
            )

        validated_sections.append(
            {
                "plant_part": part,
                "fact_count": fact_count,
                "unique_use_count": use_count,
                "representative_uses": use_list[:8],
                "associated_uses": use_list,
                "explanation": explanation,
            }
        )

    result["plant_part_sections"] = validated_sections
    result["grounded"] = bool(result.get("grounded", False))

    if result.get("unsupported_claims") is None:
        result["unsupported_claims"] = []

    return result


def generate_answer(
    context: Any,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    client: Any | None = None,
) -> dict[str, Any]:
    obj = _validate_context(context)

    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        raise AnswerGenerationError(
            "GEMINI_API_KEY is not set. Set it in the environment before a live 7C call."
        )

    AnswerResponse = _response_model()

    if client is None:
        try:
            from google import genai
        except ImportError as exc:
            raise AnswerGenerationError(
                "Install the Google GenAI SDK first: pip install google-genai"
            ) from exc
        client = genai.Client(api_key=key)

    try:
        from google import genai
    except ImportError:
        genai = None

    try:
        if genai is not None:
            config = genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AnswerResponse,
                temperature=0,
            )
        else:
            class _TestConfig:
                response_mime_type = "application/json"
                response_schema = AnswerResponse
                temperature = 0

            config = _TestConfig()

        response = client.models.generate_content(
            model=model,
            contents=build_grounded_prompt(obj),
            config=config,
        )
    except Exception as exc:
        raise AnswerGenerationError(f"Gemini API request failed: {exc}") from exc

    response_text = getattr(response, "text", None)
    if not isinstance(response_text, str) or not response_text.strip():
        raise AnswerGenerationError("Gemini returned no structured answer.")

    try:
        parsed = AnswerResponse.model_validate_json(response_text)
        result = parsed.model_dump()
    except Exception as exc:
        raise AnswerGenerationError(
            f"Gemini structured answer failed schema validation: {exc}"
        ) from exc

    result = _validate_generated_answer(result, obj)

    return {
        "contract_version": "7C-3",
        "answer": {
            "title": result["title"],
            "overview": result["overview"],
            "plant_facts": result["plant_facts"],
            "plant_part_sections": result["plant_part_sections"],
            "interpretation": result["interpretation"],
            "grounded": result["grounded"],
            "caveats": result["caveats"],
            "unsupported_claims": result["unsupported_claims"],
        },
        "statistics": _deterministic_statistics(obj),
        "model": model,
        "context_contract_version": obj["contract_version"],
        "fact_count": len(obj["facts"]),
    }


def _build_context_from_query(
    user_query: str,
    *,
    neo4j_uri: str | None = None,
    neo4j_user: str | None = None,
    neo4j_password: str | None = None,
    neo4j_database: str | None = None,
) -> dict[str, Any]:
    try:
        from query_orchestrator import run_query
    except ImportError as exc:
        raise AnswerGenerationError(
            "Could not import query_orchestrator.py from the project root."
        ) from exc

    try:
        result = run_query(
            user_query,
            neo4j_uri=neo4j_uri,
            neo4j_user=neo4j_user,
            neo4j_password=neo4j_password,
            neo4j_database=neo4j_database,
        )
    except Exception as exc:
        raise AnswerGenerationError(
            f"Existing query pipeline failed before 7C: {exc}"
        ) from exc

    if not isinstance(result, dict) or result.get("status") != "success":
        raise AnswerGenerationError(
            f"Query pipeline did not produce a successful response. "
            f"status={getattr(result, 'get', lambda _k: None)('status')!r}"
        )

    context = result.get("llm_context")
    if context is None:
        raise AnswerGenerationError(
            "query_orchestrator result does not contain llm_context. "
            "Confirm Phase 7B is integrated."
        )

    return _validate_context(context)


def _load_context_file(path: str) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise AnswerGenerationError(f"Context file was not found: {file_path}")

    try:
        with file_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise AnswerGenerationError(
            f"Could not read 7B context JSON from {file_path}: {exc}"
        ) from exc

    return _validate_context(data)


def _minimal_context() -> dict[str, Any]:
    fact = {
        "row_index": 0,
        "path": {
            "nodes": [
                {"id": "P1", "type": "plant", "name": "Mangifera indica"},
                {"id": "PP1", "type": "plant_part", "name": "leaf"},
                {"id": "TU1", "type": "therapeutic_use", "name": "Diabetes"},
            ],
            "relationships": [
                {"id": "R1", "type": "Has Part", "source": "P1", "target": "PP1"},
                {"id": "R2", "type": "Used For", "source": "PP1", "target": "TU1"},
            ],
        },
        "entities": {
            "plant": "Mangifera indica",
            "plant_part": "leaf",
            "therapeutic_use": "Diabetes",
        },
        "measurements": [],
        "provenance": {
            "graph_row_index": 0,
            "node_ids": ["P1", "PP1", "TU1"],
            "relationship_ids": ["R1", "R2"],
        },
    }

    return {
        "contract_version": "7B-1",
        "user_query": "Tell me the use of mango",
        "query_context": {
            "intent": "plant_therapeutic_uses",
            "params": {"plant": "Mangifera indica", "limit": 5000},
        },
        "grounding_instructions": list(GROUNDING_POLICY),
        "summary": {
            "row_count": 1,
            "fact_count": 1,
            "node_count": 3,
            "relationship_count": 2,
            "node_counts_by_type": {
                "plant": 1,
                "plant_part": 1,
                "therapeutic_use": 1,
            },
            "relationship_counts_by_type": {
                "Has Part": 1,
                "Used For": 1,
            },
            "bioactivity_counts_by_standard_type": {},
            "bioactivity_counts_by_confidence": {},
        },
        "facts": [fact],
        "provenance": {"source": "phase6_graph_response"},
        "context_stats": {
            "fact_count": 1,
            "context_char_count": 200,
            "context_utf8_bytes": 200,
        },
        "context_text": (
            "GROUNDING POLICY\n"
            "- Use only facts present in the supplied evidence context.\n\n"
            "USER QUERY\n"
            "Tell me the use of mango\n\n"
            "DETERMINISTIC SUMMARY\n"
            "{\"row_count\": 1, \"fact_count\": 1, \"node_count\": 3, "
            "\"relationship_count\": 2}\n\n"
            "EVIDENCE FACTS\n"
            "- row_index=0; path=plant=Mangifera indica | "
            "plant_part=leaf | therapeutic_use=Diabetes; "
            "relationships=P1 -[Has Part]-> PP1 ; "
            "PP1 -[Used For]-> TU1"
        ),
    }


def _self_test() -> None:
    context = _minimal_context()

    class FakeResponse:
        text = json.dumps({
            "title": "Mango (Mangifera indica)",
            "overview": (
                "The supplied knowledge graph identifies Mangifera indica "
                "and records an association between its leaf and Diabetes."
            ),
            "plant_facts": [
                "Scientific name: Mangifera indica."
            ],
            "key_takeaways": [
                "The graph contains one plant-part association in this test."
            ],
            "interpretation": (
                "The evidence establishes a graph association, not a "
                "clinical treatment claim."
            ),
            "caveats": [],
            "unsupported_claims": [],
            "grounded": True,
        })

    class FakeModels:
        def generate_content(self, **kwargs):
            assert kwargs["model"]
            assert kwargs["config"].response_mime_type == "application/json"
            assert "plant part" in kwargs["contents"].lower()
            assert "deterministic" in kwargs["contents"].lower()
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    result = generate_answer(
        context,
        api_key="test-key",
        client=FakeClient(),
    )

    assert result["contract_version"] == "7C-3"
    assert result["answer"]["grounded"] is True
    assert result["fact_count"] == 1
    assert result["statistics"]["fact_count"] == 1
    assert result["answer"]["plant_part_sections"][0]["plant_part"] == "leaf"
    assert result["answer"]["plant_part_sections"][0]["associated_uses"] == ["Diabetes"]
    assert result["answer"]["plant_part_sections"][0]["representative_uses"] == ["Diabetes"]

    print("7B context validation: PASS")
    print("Detailed grounded prompt construction: PASS")
    print("Gemini structured-output configuration: PASS")
    print("Statistics consistency validation: PASS")
    print("Plant-part evidence validation: PASS")
    print("Structured answer parsing: PASS")
    print("PHASE 7C DETAILED ANSWER TEST: PASS")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 7C detailed grounded answer generator."
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--query",
        help=(
            "Run the existing query pipeline through Phase 7B, "
            "then call Gemini for detailed Phase 7C output."
        ),
    )
    parser.add_argument(
        "--context-file",
        help="Run 7C directly from a Phase 7B llm_context JSON file.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI"))
    parser.add_argument("--user", default=os.getenv("NEO4J_USER"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    args = parser.parse_args()

    if args.self_test:
        if args.query or args.context_file:
            parser.error("--self-test cannot be combined with --query or --context-file.")
        _self_test()
        return

    if args.query and args.context_file:
        parser.error("Use either --query or --context-file, not both.")
    if not args.query and not args.context_file:
        parser.error("Provide --query, --context-file, or --self-test.")

    try:
        context = (
            _load_context_file(args.context_file)
            if args.context_file
            else _build_context_from_query(
                args.query,
                neo4j_uri=args.uri,
                neo4j_user=args.user,
                neo4j_password=args.password,
                neo4j_database=args.database,
            )
        )

        result = generate_answer(context, model=args.model)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"PHASE 7C FAILED: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
