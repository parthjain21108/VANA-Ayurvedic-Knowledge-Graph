"""Route a resolved scientific name through Neo4j and ingestion.

This is a new orchestration layer. It does not modify any existing collector,
ingestion, Phase 1, Phase 2, or Neo4j sync code.

Decision rule:
- Neo4j says exists=True  -> stop; do not ingest.
- Neo4j says exists=False -> call the existing ingestion endpoint, then re-check.
- Neo4j check raises    -> stop; never treat the error as missing.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Callable

from neo4j_existence import check_plant_exists
from plant_ingestion_endpoint import ingest_missing_plant


ExistenceChecker = Callable[..., dict[str, Any]]
IngestionCallable = Callable[..., dict[str, Any]]


def route_plant(
    scientific_name: str,
    *,
    existence_checker: ExistenceChecker = check_plant_exists,
    ingestion_callable: IngestionCallable = ingest_missing_plant,
    max_retries: int = 3,
    **neo4j_kwargs: Any,
) -> dict[str, Any]:
    """Check a scientific name, ingest only when definitively missing, then re-check.

    A Neo4j connection/query error is raised and ingestion is never attempted.
    """
    scientific_name = scientific_name.strip()
    if not scientific_name:
        raise ValueError("scientific_name must not be empty")
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            existence = existence_checker(scientific_name, **neo4j_kwargs)
            break
        except Exception as exc:
            last_error = exc
            if attempt == max_retries:
                raise RuntimeError(
                    "Neo4j existence check failed after "
                    f"{max_retries} attempt(s). Ingestion was NOT started."
                ) from exc
    else:  # pragma: no cover - loop always breaks or raises
        raise RuntimeError("Neo4j existence check failed") from last_error

    if existence.get("exists") is True:
        return {
            "status": "exists",
            "scientific_name": scientific_name,
            "ingestion_started": False,
            "existence": existence,
        }

    if existence.get("exists") is not False:
        raise RuntimeError(
            "Neo4j existence checker returned an invalid result. "
            "Ingestion was NOT started."
        )

    ingestion_kwargs = {
        "neo4j_uri": neo4j_kwargs.get("uri"),
        "neo4j_user": neo4j_kwargs.get("user"),
        "neo4j_password": neo4j_kwargs.get("password"),
        "neo4j_database": neo4j_kwargs.get("database"),
    }

    ingestion = ingestion_callable(scientific_name, **ingestion_kwargs)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            final_existence = existence_checker(scientific_name, **neo4j_kwargs)
            break
        except Exception as exc:
            last_error = exc
            if attempt == max_retries:
                raise RuntimeError(
                    "Neo4j re-check failed after "
                    f"{max_retries} attempt(s) following ingestion."
                ) from exc
    else:  # pragma: no cover - loop always breaks or raises
        raise RuntimeError("Neo4j re-check failed") from last_error

    if final_existence.get("exists") is not True:
        raise RuntimeError(
            "Ingestion completed, but the plant is still not present in Neo4j. "
            "The final database check did not return exists=true."
        )

    return {
        "status": "ingested",
        "scientific_name": scientific_name,
        "ingestion_started": True,
        "ingestion": ingestion,
        "existence": final_existence,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Check an exact scientific_name in Neo4j; ingest only when the "
            "database definitively reports that it is missing."
        )
    )
    parser.add_argument(
        "--plant",
        required=True,
        help='Scientific plant name, e.g. "Acacia leucophloea"',
    )
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI"))
    parser.add_argument("--user", default=os.getenv("NEO4J_USER"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    try:
        result = route_plant(
            args.plant,
            max_retries=args.retries,
            uri=args.uri,
            user=args.user,
            password=args.password,
            database=args.database,
        )
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"PLANT QUERY ROUTER FAILED: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
