"""Shared cross-plant ingestion cache.

This module is deliberately limited to operational caching. It does not define
entity identity, normalize scientific data, or alter collector semantics.
Existing per-plant cache files are merged into a persistent backend-wide cache
before a plant run and merged back after a successful master run.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

CACHE_FILENAMES = (
    "imppat_phytochemical_cache.json",
    "document_cache.json",
    "mesh_cache.json",
    "target_uniprot_cache.json",
)

EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".master_cache",
    "canonical",
    "snapshots",
}


def _clean_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Merge cache mappings without changing their existing value types."""
    for key, value in src.items():
        if key not in dst:
            dst[key] = value
        elif isinstance(dst[key], dict) and isinstance(value, dict):
            _deep_merge(dst[key], value)
        # Same key with both scalar/list values: keep the first value. Cache
        # entries are keyed by the request identity, so conflicting responses
        # should never be silently alternated between runs.


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return _clean_mapping(value)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temp, path)


def _candidate_cache_files(project_root: Path, cache_name: str, excluded: Path) -> list[Path]:
    results: list[Path] = []
    try:
        iterator = project_root.rglob(cache_name)
    except OSError:
        return results

    excluded_resolved = excluded.resolve()
    for path in iterator:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == excluded_resolved:
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in resolved.parts):
            continue
        if not resolved.is_file():
            continue
        results.append(resolved)
    return sorted(set(results), key=lambda value: str(value).lower())


def shared_cache_dir(project_root: Path) -> Path:
    """Return the persistent operational cache location."""
    override = os.getenv("VANA_SHARED_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return project_root / "phase1_release" / "data" / "shared_cache"


def shared_cache_path(project_root: Path, cache_name: str) -> Path:
    if cache_name not in CACHE_FILENAMES:
        raise ValueError(f"Unsupported shared cache: {cache_name}")
    return shared_cache_dir(project_root) / cache_name


def prepare_plant_caches(project_root: Path, output_dir: Path) -> dict[str, int]:
    """Populate the new plant's cache files from prior plant caches."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stats: dict[str, int] = {}

    for cache_name in CACHE_FILENAMES:
        merged: dict[str, Any] = {}
        source_count = 0
        destination = output_dir / cache_name

        # A non-empty persistent cache is already the cross-plant index.
        # Historical plant folders are scanned only to bootstrap that index
        # the first time this update is installed.
        persistent = shared_cache_path(project_root, cache_name)
        persistent_data = load_json(persistent)
        if persistent_data:
            _deep_merge(merged, persistent_data)
            source_count += 1
        else:
            for source in _candidate_cache_files(project_root, cache_name, destination):
                if source == persistent.resolve():
                    continue
                data = load_json(source)
                if data:
                    _deep_merge(merged, data)
                    source_count += 1
            if merged:
                atomic_write_json(persistent, merged)

        if merged:
            atomic_write_json(destination, merged)
        stats[cache_name] = len(merged)

    return stats


def publish_plant_caches(project_root: Path, output_dir: Path) -> dict[str, int]:
    """Merge successful plant cache additions into persistent shared caches."""
    stats: dict[str, int] = {}
    for cache_name in CACHE_FILENAMES:
        source = output_dir / cache_name
        if not source.exists():
            stats[cache_name] = 0
            continue

        persistent = shared_cache_path(project_root, cache_name)
        merged = load_json(persistent)
        before = len(json.dumps(merged, ensure_ascii=False))
        _deep_merge(merged, load_json(source))
        atomic_write_json(persistent, merged)
        after = len(json.dumps(merged, ensure_ascii=False))
        stats[cache_name] = max(0, after - before)
    return stats


def http_cache_enabled() -> bool:
    return os.getenv("VANA_SHARED_HTTP_CACHE_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def http_cache_dir(project_root: Path) -> Path:
    return shared_cache_dir(project_root) / "bioactivity_http_cache"


def http_cache_key(url: str, params: dict[str, Any] | None = None) -> str:
    payload = json.dumps(
        {"url": url, "params": params or {}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _http_cache_path(project_root: Path, url: str, params: dict[str, Any] | None = None) -> Path:
    return http_cache_dir(project_root) / f"{http_cache_key(url, params)}.json"


def http_cache_get(project_root: Path, url: str, params: dict[str, Any] | None = None) -> tuple[bool, Any]:
    path = _http_cache_path(project_root, url, params)
    record = load_json(path)
    if not record:
        return False, None
    if record.get("ok") is False:
        return True, None
    if record.get("ok") is True:
        return True, record.get("data")
    return False, None


def http_cache_put(project_root: Path, url: str, params: dict[str, Any] | None, data: Any) -> None:
    atomic_write_json(
        _http_cache_path(project_root, url, params),
        {"ok": True, "data": data},
    )


def http_cache_put_negative(project_root: Path, url: str, params: dict[str, Any] | None, status: int) -> None:
    atomic_write_json(
        _http_cache_path(project_root, url, params),
        {"ok": False, "status": status},
    )
