# Production cleanup manifest

Retained: runtime API/query stack, ingestion pipeline, canonical current dataset, shared operational caches, and existing Phase 1 documentation/configuration.

Removed: benchmark/test suites and reports, logs, bytecode/cache directories, historical plant-run outputs, generated master output, incremental backups, stale staging/snapshot history, obsolete duplicate query modules, patch archives/scripts, and unused diagnostic scripts.

No production Python logic was edited.
