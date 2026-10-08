# Phase 1 — Canonical Data Store + Incremental Database Updater

## Purpose

Keep the existing collectors and `master.py` unchanged. Add a separate data-management layer that turns collector outputs into a canonical dataset and incrementally synchronizes Neo4j.

## Source of truth

`data/canonical/current/` is the canonical graph dataset. Neo4j is the query/serving layer.

The same folder layout can later be mirrored to S3 / GCS / Azure Blob without changing the updater contract.

## Folder contract

```text
data/
  canonical/
    current/
      Plant.csv
      PlantPart.csv
      Phytochemicals.csv
      TherapeuticUses.csv
      Bioactivities.csv
      Targets.csv
      Documents.csv
      HAS_PART.csv
      CONTAINS.csv
      USED_FOR.csv
      HAS_BIOACTIVITY.csv
      MEASURED_ON.csv
      REPORTED_IN.csv
    snapshots/
      <UTC timestamp>/...
  staging/
    <run-id>/...
  manifests/
    <run-id>.json
  logs/
  reference/
```

## Identity rules

| Entity | Global identity |
|---|---|
| Plant | normalized `scientific_name` |
| PlantPart | `plant_part_id` |
| Phytochemical | `inchi_key` |
| Therapeutic Use | `mesh_id` |
| Target | `target_chembl_id` |
| Document | `document_chembl_id` |
| Bioactivity | exact measurement + target + document |

For non-Bioactivity nodes, an incoming row with an existing identity reuses the existing canonical node. The existing canonical row wins; Phase 1 does not overwrite populated canonical fields.

## Bioactivity identity

A Bioactivity is reusable only when all six values are identical:

```text
standard_type
standard_relation
standard_value
standard_units
target_id
document_id
```

Target comes from `MEASURED_ON` and document comes from `REPORTED_IN`.

Missing target or missing document means **do not merge**.

Incoming collector BA IDs are not treated as globally authoritative. The updater maps them to a canonical BA ID by semantic identity, assigning the next unused `BA_######` only for genuinely new measurements.

## Relationship policy

Relationships are set-unioned:

- Existing edges stay.
- New edges are added.
- Exact duplicate rows are collapsed.
- `HAS_BIOACTIVITY` preserves every unique phytochemical association.
- Incoming Bioactivity IDs are remapped to canonical IDs only in the three Bioactivity-facing relationship tables.

## Update transaction policy

1. Read the incoming master output.
2. Read canonical current data.
3. Validate required columns.
4. Compute entity identity matches.
5. Compute Bioactivity semantic matches.
6. Build an in-memory merged dataset.
7. Validate endpoint and association invariants.
8. Snapshot the current canonical dataset.
9. Atomically replace the canonical current CSVs.
10. Write a run manifest.
11. Only after canonical data succeeds should the Neo4j loader run.

If validation fails, canonical current files are not changed.

## Explicit non-goals

Phase 1 does not modify:

- collectors
- `master.py`
- checkpoints
- scraping logic
- existing field names
- graph schema
- NLP
- frontend

## Production storage

For the first development version, the canonical directory can be local. For the deployed application, place `data/canonical/` in durable object storage (for example S3-compatible storage) and treat timestamped snapshots/manifests as immutable.

## Neo4j role

Neo4j is the serving/query layer. It should be reproducible from the canonical dataset, so a full rebuild remains possible without re-scraping external sources.
