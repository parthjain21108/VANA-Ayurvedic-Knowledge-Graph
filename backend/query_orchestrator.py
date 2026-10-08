"""
Phase 4 query orchestration for the Ayurvedic knowledge graph.

This layer coordinates the already-validated components:

    3B query understanding
        -> 3C entity resolution
        -> plant existence / conditional ingestion
        -> deterministic graph query
        -> frontend-ready response

It intentionally does not modify the collector, ingestion pipeline,
Phase 1, Phase 2, Neo4j sync, or graph schema.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Callable

from graph_query_engine import execute_query
from phase6_response_contract import validate_graph_response
from phase7a_evidence_builder import build_evidence
from phase7b_context_builder import build_llm_context
from phase7c_answer_generator import generate_answer
from plant_query_router import route_plant
from query.entity_resolver import resolve_entities
from query.intents import is_plant_intent
from query.resolver import resolve_query


class QueryOrchestrationError(RuntimeError):
    """Raised when the end-to-end query flow cannot safely continue."""


QueryResolver = Callable[..., Any]
EntityResolver = Callable[..., Any]
PlantRouter = Callable[..., dict[str, Any]]
GraphExecutor = Callable[..., dict[str, Any]]
AnswerGenerator = Callable[..., dict[str, Any]]


def _entity_type_value(entity: Any) -> str:
    value = getattr(entity.entity_type, "value", entity.entity_type)
    return str(value)


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _get_plant_entity(query: Any) -> Any | None:
    plants = [
        entity
        for entity in query.entities
        if _entity_type_value(entity) == "plant"
    ]
    if len(plants) > 1:
        raise QueryOrchestrationError(
            f"Expected at most one plant entity; found {len(plants)}."
        )
    return plants[0] if plants else None


def _get_unresolved_entities(query: Any) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []

    for entity in query.entities:
        status = _status_value(entity.resolution_status)
        if status != "resolved":
            unresolved.append(
                {
                    "entity_type": _entity_type_value(entity),
                    "user_term": getattr(entity, "user_term", ""),
                    "resolution_status": status,
                    "canonical_value": getattr(entity, "canonical_value", None),
                    "canonical_id": getattr(entity, "canonical_id", None),
                    "candidates": list(getattr(entity, "candidates", []) or []),
                }
            )

    return unresolved


def _serialize_entities(query: Any) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []

    for entity in query.entities:
        entities.append(
            {
                "entity_type": _entity_type_value(entity),
                "user_term": getattr(entity, "user_term", ""),
                "resolution_status": _status_value(entity.resolution_status),
                "canonical_value": getattr(entity, "canonical_value", None),
                "canonical_id": getattr(entity, "canonical_id", None),
                "aliases": list(getattr(entity, "aliases", []) or []),
                "candidates": list(getattr(entity, "candidates", []) or []),
                "confidence": getattr(entity, "confidence", None),
            }
        )

    return entities


def _serialize_filters(query: Any) -> dict[str, Any]:
    filters = getattr(query, "filters", None)
    if filters is None:
        return {}

    if hasattr(filters, "model_dump"):
        return filters.model_dump()

    if hasattr(filters, "dict"):
        return filters.dict()

    return {
        key: getattr(filters, key)
        for key in dir(filters)
        if not key.startswith("_") and not callable(getattr(filters, key))
    }


def run_query(
    user_query: str,
    *,
    neo4j_uri: str | None = None,
    neo4j_user: str | None = None,
    neo4j_password: str | None = None,
    neo4j_database: str | None = None,
    max_entity_candidates: int = 5,
    max_ingestion_retries: int = 3,
    query_resolver: QueryResolver = resolve_query,
    entity_resolver: EntityResolver = resolve_entities,
    plant_router: PlantRouter = route_plant,
    graph_executor: GraphExecutor = execute_query,
    answer_generator: AnswerGenerator = generate_answer,
    driver_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """
    Run one user query through the complete Phase 4 orchestration.

    Rules:
    - 3B extracts intent/entities only.
    - 3C resolves entities.
    - A plant is sent to the ingestion router only when the intent is
      plant-related and the plant resolves to a scientific name.
    - The existing plant router decides whether ingestion is required.
    - Graph querying happens only after entity resolution is usable.
    """
    if not isinstance(user_query, str) or not user_query.strip():
        raise ValueError("user_query must be a non-empty string")

    uri = neo4j_uri if neo4j_uri is not None else os.getenv("NEO4J_URI")
    user = neo4j_user if neo4j_user is not None else os.getenv("NEO4J_USER")
    password = (
        neo4j_password if neo4j_password is not None else os.getenv("NEO4J_PASSWORD")
    )
    database = (
        neo4j_database
        if neo4j_database is not None
        else os.getenv("NEO4J_DATABASE", "neo4j")
    )

    if not uri or not user or password is None:
        raise QueryOrchestrationError(
            "Provide NEO4J_URI, NEO4J_USER, and NEO4J_PASSWORD. "
            "NEO4J_DATABASE is optional and defaults to neo4j."
        )

    phase3b = query_resolver(user_query)

    b_status = _status_value(phase3b.status)
    if b_status in {"ambiguous", "unresolved", "invalid"}:
        return {
            "status": b_status,
            "query": phase3b.original_query,
            "intent": phase3b.intent,
            "entities": _serialize_entities(phase3b),
            "filters": _serialize_filters(phase3b),
            "ingestion": {
                "eligible": False,
                "required": False,
                "started": False,
                "status": "not_started",
            },
            "graph": None,
            "clarification_question": getattr(
                phase3b, "clarification_question", None
            ),
            "notes": list(getattr(phase3b, "notes", []) or []),
        }

    # Open one read session for 3C resolution and the final graph query.
    if driver_factory is None:
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise QueryOrchestrationError(
                "Install the neo4j package before running Phase 4: "
                "pip install neo4j"
            ) from exc

        driver_factory = GraphDatabase.driver

    driver = driver_factory(uri, auth=(user, password))

    try:
        # Resolve entities in a dedicated session. Close this session BEFORE
        # the ingestion router can run, because the existing ingestion/sync
        # pipeline opens its own Neo4j connections and the original session
        # can become defunct while that pipeline is running.
        with driver.session(database=database) as resolution_session:
            phase3c = entity_resolver(
                phase3b,
                resolution_session,
                max_candidates=max_entity_candidates,
            )

        unresolved = _get_unresolved_entities(phase3c)
        if unresolved:
            return {
                "status": "entity_resolution_incomplete",
                "query": phase3c.original_query,
                "intent": phase3c.intent,
                "entities": _serialize_entities(phase3c),
                "filters": _serialize_filters(phase3c),
                "ingestion": {
                    "eligible": bool(is_plant_intent(phase3c.intent)),
                    "required": False,
                    "started": False,
                    "status": "not_started",
                },
                "graph": None,
                "clarification_question": getattr(
                    phase3c, "clarification_question", None
                ),
                "unresolved_entities": unresolved,
                "notes": list(getattr(phase3c, "notes", []) or []),
            }

        plant_router_result: dict[str, Any] = {
            "status": "not_eligible",
            "scientific_name": None,
            "ingestion_started": False,
        }

        plant_related = is_plant_intent(phase3c.intent)
        plant_entity = _get_plant_entity(phase3c) if plant_related else None

        if plant_related and plant_entity is None:
            raise QueryOrchestrationError(
                f"Plant intent {phase3c.intent!r} requires a resolved plant entity."
            )

        if plant_related:
            scientific_name = (
                getattr(plant_entity, "canonical_value", None)
                or getattr(plant_entity, "canonical_id", None)
            )

            if not scientific_name:
                raise QueryOrchestrationError(
                    "Resolved plant has no scientific_name canonical identity."
                )

            plant_router_result = plant_router(
                str(scientific_name),
                max_retries=max_ingestion_retries,
                uri=uri,
                user=user,
                password=password,
                database=database,
            )

            # Conditional ingestion/sync may invalidate connections that
            # were already held by this driver's connection pool.
            # Recreate the driver before the final graph query.
            if plant_router_result.get("ingestion_started", False):
                driver.close()
                driver = driver_factory(
                    uri,
                    auth=(user, password),
                )

        # Use a fresh session for the final graph query.
        with driver.session(database=database) as graph_session:
            graph_result = graph_executor(graph_session, phase3c)

        # Phase 6: validate the graph contract before exposing it downstream.
        validate_graph_response(graph_result)

        # Phase 7A: deterministically convert the validated graph into
        # LLM-ready evidence. This does not call an LLM or query Neo4j.
        evidence = build_evidence(graph_result)

        # Phase 7B: build a deterministic, inspectable LLM context. No LLM
        # call occurs here; the complete evidence fact list is preserved.
        llm_context = build_llm_context(
            evidence,
            user_query=phase3c.original_query,
        )

        # Phase 7C: generate the user-facing grounded explanation from the
        # deterministic 7B context. The graph/evidence remain authoritative.
        answer = answer_generator(llm_context)

        return {
            "status": "success",
            "query": phase3c.original_query,
            "intent": phase3c.intent,
            "entities": _serialize_entities(phase3c),
            "filters": _serialize_filters(phase3c),
            "ingestion": {
                "eligible": plant_related,
                "required": (
                    plant_router_result.get("status") == "ingested"
                ),
                "started": bool(
                    plant_router_result.get("ingestion_started", False)
                ),
                "router": plant_router_result,
            },
            "graph": graph_result,
            "evidence": evidence,
            "llm_context": llm_context,
            "answer": answer,
            "clarification_question": getattr(
                phase3c, "clarification_question", None
            ),
            "notes": list(getattr(phase3c, "notes", []) or []),
        }
    finally:
        driver.close()


def _offline_test() -> None:
    """
    Offline orchestration tests.

    These tests inject every external dependency, so they do not require:
    - Gemini
    - Neo4j
    - ingestion
    """
    from types import SimpleNamespace

    class FakeFilters:
        def model_dump(self) -> dict[str, Any]:
            return {"standard_type": "IC50"}

    def entity(
        entity_type: str,
        term: str,
        status: str = "resolved",
        canonical_value: str | None = None,
        canonical_id: str | None = None,
    ) -> Any:
        return SimpleNamespace(
            entity_type=SimpleNamespace(value=entity_type),
            user_term=term,
            resolution_status=SimpleNamespace(value=status),
            canonical_value=canonical_value,
            canonical_id=canonical_id,
            aliases=[],
            candidates=[],
            confidence=None,
        )

    def structured(
        *,
        intent: str,
        entities: list[Any],
        status: str = "resolved",
    ) -> Any:
        return SimpleNamespace(
            status=SimpleNamespace(value=status),
            intent=intent,
            original_query="test query",
            entities=entities,
            filters=FakeFilters(),
            clarification_question=None,
            notes=[],
        )

    class FakeSession:
        pass

    class FakeSessionContext:
        def __enter__(self) -> FakeSession:
            return FakeSession()

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class FakeDriver:
        def session(self, *, database: str) -> FakeSessionContext:
            assert database == "neo4j"
            return FakeSessionContext()

        def close(self) -> None:
            pass

    def answer_fake(context: Any) -> dict[str, Any]:
        return {
            "contract_version": "7C-3",
            "answer": {
                "title": "Test",
                "overview": "Grounded test answer.",
                "plant_facts": [],
                "key_takeaways": [],
                "plant_part_sections": [],
                "interpretation": "Test graph association.",
                "caveats": [],
                "unsupported_claims": [],
                "grounded": True,
            },
            "statistics": context["summary"],
            "model": "fake",
            "context_contract_version": context["contract_version"],
            "fact_count": context["context_stats"]["fact_count"],
        }

    # 1. Existing plant -> router says exists -> no ingestion.
    ingestion_calls: list[str] = []

    def resolver_existing(user_query: str) -> Any:
        return structured(
            intent="plant_phytochemicals",
            entities=[
                entity(
                    "plant",
                    "Drumstick Tree",
                    canonical_value="Moringa oleifera",
                    canonical_id="Moringa oleifera",
                )
            ],
        )

    def resolver_passthrough(query: Any, session: Any, *, max_candidates: int) -> Any:
        return query

    def router_existing(scientific_name: str, **kwargs: Any) -> dict[str, Any]:
        ingestion_calls.append(scientific_name)
        assert scientific_name == "Moringa oleifera"
        return {
            "status": "exists",
            "scientific_name": scientific_name,
            "ingestion_started": False,
            "existence": {"scientific_name": scientific_name, "exists": True},
        }

    def graph_fake(session: Any, query: Any) -> dict[str, Any]:
        return {
            "intent": query.intent,
            "cypher": "MATCH (n) RETURN n LIMIT $limit",
            "params": {"limit": 5000},
            "row_count": 0,
            "nodes": [],
            "relationships": [],
            "rows": [],
        }

    result = run_query(
        "existing plant",
        neo4j_uri="bolt://fake",
        neo4j_user="neo4j",
        neo4j_password="test",
        neo4j_database="neo4j",
        query_resolver=resolver_existing,
        entity_resolver=resolver_passthrough,
        plant_router=router_existing,
        graph_executor=graph_fake,
        answer_generator=answer_fake,
        driver_factory=lambda *args, **kwargs: FakeDriver(),
    )

    assert result["status"] == "success"
    assert result["ingestion"]["eligible"] is True
    assert result["ingestion"]["started"] is False
    answer_obj = result.get("answer")
    grounded = None
    if isinstance(answer_obj, dict):
        grounded = answer_obj.get("grounded")
        if grounded is None and isinstance(answer_obj.get("answer"), dict):
            grounded = answer_obj["answer"].get("grounded")
    if grounded is None:
        grounded = result.get("grounded")
    assert grounded is True
    assert ingestion_calls == ["Moringa oleifera"]
    print("Existing plant -> no ingestion: PASS")

    # 2. Missing plant -> router says missing then ingested.
    def router_missing(scientific_name: str, **kwargs: Any) -> dict[str, Any]:
        assert scientific_name == "Acacia leucophloea"
        return {
            "status": "ingested",
            "scientific_name": scientific_name,
            "ingestion_started": True,
            "ingestion": {"status": "success"},
            "existence": {"scientific_name": scientific_name, "exists": True},
        }

    def resolver_missing(user_query: str) -> Any:
        return structured(
            intent="plant_information",
            entities=[
                entity(
                    "plant",
                    "White Babool",
                    canonical_value="Acacia leucophloea",
                    canonical_id="Acacia leucophloea",
                )
            ],
        )

    result = run_query(
        "missing plant",
        neo4j_uri="bolt://fake",
        neo4j_user="neo4j",
        neo4j_password="test",
        neo4j_database="neo4j",
        query_resolver=resolver_missing,
        entity_resolver=resolver_passthrough,
        plant_router=router_missing,
        graph_executor=graph_fake,
        answer_generator=answer_fake,
        driver_factory=lambda *args, **kwargs: FakeDriver(),
    )

    assert result["status"] == "success"
    assert result["ingestion"]["eligible"] is True
    assert result["ingestion"]["required"] is True
    assert result["ingestion"]["started"] is True
    print("Missing plant -> ingestion path: PASS")

    # 3. Non-plant query -> router is never called.
    def resolver_nonplant(user_query: str) -> Any:
        return structured(
            intent="phytochemical_targets",
            entities=[
                entity(
                    "phytochemical",
                    "quercetin",
                    canonical_value="REFJWTPEDVJJIY-UHFFFAOYSA-N",
                    canonical_id="REFJWTPEDVJJIY-UHFFFAOYSA-N",
                )
            ],
        )

    def router_should_not_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Plant router must not run for non-plant intent.")

    result = run_query(
        "quercetin targets",
        neo4j_uri="bolt://fake",
        neo4j_user="neo4j",
        neo4j_password="test",
        neo4j_database="neo4j",
        query_resolver=resolver_nonplant,
        entity_resolver=resolver_passthrough,
        plant_router=router_should_not_run,
        graph_executor=graph_fake,
        answer_generator=answer_fake,
        driver_factory=lambda *args, **kwargs: FakeDriver(),
    )

    assert result["status"] == "success"
    assert result["ingestion"]["eligible"] is False
    assert result["ingestion"]["started"] is False
    print("Non-plant query -> no ingestion: PASS")

    # 4. Ambiguous/unresolved entity -> graph and ingestion do not run.
    def resolver_unresolved(user_query: str) -> Any:
        return structured(
            intent="plant_phytochemicals",
            entities=[
                entity(
                    "plant",
                    "babul",
                    status="unresolved",
                )
            ],
        )

    result = run_query(
        "ambiguous plant",
        neo4j_uri="bolt://fake",
        neo4j_user="neo4j",
        neo4j_password="test",
        neo4j_database="neo4j",
        query_resolver=resolver_unresolved,
        entity_resolver=resolver_passthrough,
        plant_router=router_should_not_run,
        graph_executor=graph_fake,
        answer_generator=answer_fake,
        driver_factory=lambda *args, **kwargs: FakeDriver(),
    )

    assert result["status"] == "entity_resolution_incomplete"
    assert result["ingestion"]["started"] is False
    assert result["graph"] is None
    print("Unresolved plant -> no ingestion/no graph: PASS")

    # 5. Neo4j/driver creation failure happens before ingestion.
    def failing_driver(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated Neo4j failure")

    try:
        run_query(
            "db failure",
            neo4j_uri="bolt://fake",
            neo4j_user="neo4j",
            neo4j_password="test",
            neo4j_database="neo4j",
            query_resolver=resolver_existing,
            plant_router=router_should_not_run,
            driver_factory=failing_driver,
        )
    except RuntimeError as exc:
        assert "simulated Neo4j failure" in str(exc)
    else:
        raise AssertionError("Expected simulated Neo4j failure.")

    print("Neo4j failure -> no ingestion: PASS")

    # 6. Full EGFR-shaped query contract.
    def resolver_egfr(user_query: str) -> Any:
        return structured(
            intent="plant_phytochemical_bioactivities",
            entities=[
                entity(
                    "plant",
                    "Acacia leucophloea",
                    canonical_value="Acacia leucophloea",
                    canonical_id="Acacia leucophloea",
                ),
                entity(
                    "target",
                    "EGFR",
                    canonical_value="CHEMBL203",
                    canonical_id="CHEMBL203",
                ),
            ],
        )

    def graph_egfr(session: Any, query: Any) -> dict[str, Any]:
        return {
            "intent": query.intent,
            "cypher": "MATCH (n) RETURN n LIMIT $limit",
            "params": {
                "limit": 5000,
                "plant": "Acacia leucophloea",
                "target": "CHEMBL203",
                "standard_type": "IC50",
            },
            "row_count": 0,
            "nodes": [],
            "relationships": [],
            "rows": [],
        }

    def router_egfr(scientific_name: str, **kwargs: Any) -> dict[str, Any]:
        assert scientific_name == "Acacia leucophloea"
        return {
            "status": "exists",
            "scientific_name": scientific_name,
            "ingestion_started": False,
            "existence": {"scientific_name": scientific_name, "exists": True},
        }

    result = run_query(
        "Which phytochemicals in Acacia leucophloea have IC50 activity against EGFR?",
        neo4j_uri="bolt://fake",
        neo4j_user="neo4j",
        neo4j_password="test",
        neo4j_database="neo4j",
        query_resolver=resolver_egfr,
        entity_resolver=resolver_passthrough,
        plant_router=router_egfr,
        graph_executor=graph_egfr,
        answer_generator=answer_fake,
        driver_factory=lambda *args, **kwargs: FakeDriver(),
    )

    assert result["status"] == "success"
    assert result["graph"]["params"]["plant"] == "Acacia leucophloea"
    assert result["graph"]["params"]["target"] == "CHEMBL203"
    assert result["graph"]["params"]["standard_type"] == "IC50"
    answer_obj = result.get("answer")
    grounded = None
    if isinstance(answer_obj, dict):
        grounded = answer_obj.get("grounded")
        if grounded is None and isinstance(answer_obj.get("answer"), dict):
            grounded = answer_obj["answer"].get("grounded")
    if grounded is None:
        grounded = result.get("grounded")
    assert grounded is True
    print("EGFR query orchestration -> graph-ready: PASS")

    print("PHASE 4 ORCHESTRATOR OFFLINE TEST: PASS")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 4 end-to-end query orchestrator."
    )
    parser.add_argument("--query", help="Natural-language user query.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI"))
    parser.add_argument("--user", default=os.getenv("NEO4J_USER"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--entity-candidates", type=int, default=5)
    parser.add_argument("--ingestion-retries", type=int, default=3)
    args = parser.parse_args()

    if args.self_test:
        _offline_test()
        return

    if not args.query:
        parser.error("Provide --query or --self-test.")

    try:
        result = run_query(
            args.query,
            neo4j_uri=args.uri,
            neo4j_user=args.user,
            neo4j_password=args.password,
            neo4j_database=args.database,
            max_entity_candidates=args.entity_candidates,
            max_ingestion_retries=args.ingestion_retries,
        )
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        print(f"PHASE 4 QUERY FAILED: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()