"""
Phase 5 HTTP API for the Ayurvedic knowledge graph.

Thin HTTP boundary around query_orchestrator.run_query().
It does not modify the collector, ingestion pipeline, Phase 1, Phase 2,
Neo4j sync, entity resolver, or graph query engine.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from query_orchestrator import run_query
from frontend_response_contract import build_frontend_response


try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Phase 5 requires FastAPI and Pydantic. "
        "Install with: pip install fastapi uvicorn"
    ) from exc


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Natural-language user query.")


class QueryResponse(BaseModel):
    status: str
    query: str | None = None
    intent: str | None = None
    entities: list[dict[str, Any]] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    ingestion: dict[str, Any] = Field(default_factory=dict)
    graph: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    llm_context: dict[str, Any] | None = None
    answer: dict[str, Any] | None = None
    frontend: dict[str, Any] | None = None
    clarification_question: str | None = None
    unresolved_entities: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Ayurvedic Knowledge Graph API",
        version="0.2.0",
        description=(
            "HTTP API around the validated query orchestration, grounded answer, "
            "and deterministic frontend response-contract pipeline."
        ),
    )

    # Development-friendly CORS. Restrict this with FRONTEND_ORIGINS in production.
    origins_raw = os.getenv("FRONTEND_ORIGINS", "*")
    allow_origins = [x.strip() for x in origins_raw.split(",") if x.strip()]
    allow_credentials = allow_origins != ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/query", response_model=QueryResponse)
    def query_endpoint(payload: QueryRequest) -> dict[str, Any]:
        text = payload.query.strip()
        if not text:
            raise HTTPException(status_code=400, detail="query must not be empty")

        try:
            result = run_query(text)
            result["frontend"] = build_frontend_response(result)
            return result
        except Exception as exc:
            # Keep internal details server-side rather than returning credentials,
            # connection strings, or full stack traces to the browser.
            print(f"PHASE 5 /query FAILED: {type(exc).__name__}: {exc}")
            raise HTTPException(
                status_code=500,
                detail="Query processing failed. Check the API server logs.",
            ) from exc

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Phase 5 HTTP API.")
    parser.add_argument("--host", default=os.getenv("API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8000")))
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "Install Uvicorn first: pip install uvicorn"
        ) from exc

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
