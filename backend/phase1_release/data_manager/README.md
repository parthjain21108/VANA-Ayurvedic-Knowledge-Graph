# Data Manager

This package is the first addition outside the existing collector pipeline.

Usage:

```bash
python -m data_manager.updater path/to/master_output --store data/canonical --dry-run
python -m data_manager.updater path/to/master_output --store data/canonical
```

Then load the canonical dataset into Neo4j using environment variables:

```bash
set NEO4J_URI=...
set NEO4J_USER=...
set NEO4J_PASSWORD=...
python -c "from data_manager.neo4j_loader import load_from_env; load_from_env('data/canonical')"
```

The updater never calls the collectors and never edits collector files.
