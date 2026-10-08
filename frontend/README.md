# Vana — Frontend

A complete React + TypeScript frontend for the existing Vana botanical knowledge-graph API. It does not talk to Neo4j directly. It only calls `POST /query`, adapts the returned Phase 8 response, and renders the answer, plant-part explorer, evidence trail and an interactive graph.

## Run

1. Start the existing Python API on `http://127.0.0.1:8000`.
2. In this folder run `npm install` then `npm run dev`.
3. Open `http://localhost:5173`.

Set `VITE_API_BASE_URL` when the API is elsewhere.

## Backend expectations

The frontend is designed for the current response contract containing `status`, `query`, `intent`, `entities`, `graph`, `evidence`, `answer`, and `frontend`. The adapter is defensive around a few optional fields so minor backend additions don't break rendering.

## Important

The graph remains authoritative in `response.graph`. The frontend does not invent relationships or measurements. Selection/highlighting is derived from graph IDs and evidence provenance returned by the API.
