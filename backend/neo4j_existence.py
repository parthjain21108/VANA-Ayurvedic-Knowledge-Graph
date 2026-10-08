from __future__ import annotations

import argparse
import json
import os
from typing import Any


def check_plant_exists(
    scientific_name: str,
    *,
    uri: str | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """Check whether a Plant Name node exists for the exact scientific name."""
    scientific_name = scientific_name.strip()
    if not scientific_name:
        raise ValueError("scientific_name must not be empty")

    uri = uri if uri is not None else os.getenv("NEO4J_URI")
    user = user if user is not None else os.getenv("NEO4J_USER")
    password = password if password is not None else os.getenv("NEO4J_PASSWORD")
    database = database if database is not None else os.getenv("NEO4J_DATABASE", "neo4j")

    if not uri or not user or password is None:
        raise RuntimeError(
            "Provide NEO4J_URI, NEO4J_USER, and NEO4J_PASSWORD. "
            "NEO4J_DATABASE is optional and defaults to neo4j."
        )

    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError(
            "Install the neo4j package before using the existence checker: "
            "pip install neo4j"
        ) from exc

    query = """
    MATCH (p:`Plant Name`)
    WHERE toLower(trim(toString(p.scientific_name))) = toLower(trim($scientific_name))
    RETURN count(p) > 0 AS exists
    """

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            record = session.run(query, scientific_name=scientific_name).single()
            exists = bool(record["exists"]) if record is not None else False
    finally:
        driver.close()

    return {
        "scientific_name": scientific_name,
        "exists": exists,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check whether an exact scientific_name exists in Neo4j."
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
    args = parser.parse_args()

    try:
        result = check_plant_exists(
            args.plant,
            uri=args.uri,
            user=args.user,
            password=args.password,
            database=args.database,
        )
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"NEO4J EXISTENCE CHECK FAILED: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
