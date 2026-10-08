# Vana Backend — Production Package

This directory contains the production backend for the Ayurvedic knowledge-graph application.

## Runtime flow

```text
HTTP API
  -> query orchestration
  -> natural-language parsing
  -> graph-backed entity resolution
  -> deterministic Cypher generation
  -> Neo4j
  -> evidence/context
  -> grounded answer
```

## Start

Set these environment variables:

```text
NEO4J_URI=<neo4j-bolt-uri>
NEO4J_USER=<neo4j-user>
NEO4J_PASSWORD=<neo4j-password>
NEO4J_DATABASE=neo4j
GEMINI_API_KEY=<gemini-api-key>
FRONTEND_ORIGINS=<frontend-origin>
```

Then install dependencies and start the API:

```powershell
pip install -r requirements.txt
python api.py --host 127.0.0.1 --port 8000
```

Health check:

```text
GET /health
```

Query endpoint:

```text
POST /query
{"query":"Tell me about quercetin."}
```

## Data locations

- `phase1_release/data/canonical/current/` — canonical graph dataset used by the data-management layer.
- `phase1_release/data/shared_cache/` — persistent operational ingestion caches.
- `query/` — natural-language parsing, entity resolution, schema and deterministic query planning.
- `phase1_release/data_manager/` — canonical-store update and Neo4j synchronization.
- `phase1_release/ingestion/` — production one-plant ingestion orchestration.

The ingestion path creates temporary per-plant output directories at runtime and does not require historical plant-run folders to be shipped with the backend.

## Production notes

Neo4j is the graph source of truth. Gemini is used for language understanding and answer generation; graph-derived facts are produced deterministically from Neo4j.

Historical test suites, benchmark outputs, logs, Python bytecode, patch archives, stale backups, and previous plant-run output folders are intentionally excluded from this package.
