# Phase 1 Operations

Run the existing collector/master exactly as it is today. When a completed output folder exists, pass that folder to the new updater.

```bash
python -m data_manager.updater "path/to/NewPlant.xlsx" --store data/canonical --dry-run
python -m data_manager.updater "path/to/NewPlant.xlsx" --store data/canonical
```

Then, when the `neo4j` package and credentials are configured:

```bash
python -c "from data_manager.neo4j_loader import load_from_env; load_from_env('data/canonical')"
```

Phase 1 is deliberately conservative: existing canonical node rows win; new identities are added; relationships are unioned; Bioactivity IDs are remapped semantically; collector and checkpoint files are never edited.
