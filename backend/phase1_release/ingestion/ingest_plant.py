"""Production-oriented one-plant ingestion orchestrator.

Composition only; it does not modify master.py, collectors, Phase 1 updater,
Phase 2 sync, or the Neo4j schema.

Pipeline:
    existing master.py -> Phase 1 Data Manager -> Phase 2 Neo4j sync

Important compatibility behavior:
- master.py in this project catches its own exceptions and may exit with code 0
  even after printing ``MASTER PIPELINE FAILED``.  This orchestrator therefore
  treats that banner as a real failure and surfaces the actual master output.
- Collector output stays outside phase1_release, matching the project's
  existing layout and the direct master.py --output behavior.
- Phase 1 and Phase 2 are invoked only after their predecessors succeed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROJECT_ROOT_FOR_CACHE = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT_FOR_CACHE) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT_FOR_CACHE))

from shared_ingestion_cache import (  # noqa: E402
    prepare_plant_caches,
    publish_plant_caches,
)


@dataclass
class StageResult:
    name: str
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


MASTER_FAILURE_MARKER = "MASTER PIPELINE FAILED"

# These are the final CSVs produced by master.py only after the entire graph
# pipeline reaches FINAL - CSV EXPORT + FINAL - GRAPH VALIDATION.
MASTER_FINAL_OUTPUTS = [
    "Plant.csv",
    "PlantPart.csv",
    "Phytochemicals.csv",
    "TherapeuticUses.csv",
    "Bioactivities.csv",
    "Targets.csv",
    "Documents.csv",
    "HAS_PART.csv",
    "CONTAINS.csv",
    "USED_FOR.csv",
    "HAS_BIOACTIVITY.csv",
    "MEASURED_ON.csv",
    "REPORTED_IN.csv",
]


def _project_root() -> Path:
    # ingestion/ingest_plant.py lives at:
    # <project_root>/phase1_release/ingestion/ingest_plant.py
    return Path(__file__).resolve().parents[2]


def _phase1_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _safe_plant_name(name: str) -> str:
    text = "_".join(name.strip().split())
    text = re.sub(r"[^A-Za-z0-9._-]", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    if not text:
        raise ValueError("Plant name produces an empty output directory name.")
    return text


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> StageResult:
    """Run a child process while streaming its output and retaining logs."""
    import threading

    started = time.perf_counter()
    child_env = dict(env) if env is not None else os.environ.copy()
    # The child must flush progress while stdout is connected to a pipe.
    child_env.setdefault("PYTHONUNBUFFERED", "1")

    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=child_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def stream_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_chunks.append(line)
            print(line, end="", flush=True)

    def stream_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)
            print(line, end="", file=sys.stderr, flush=True)

    stdout_thread = threading.Thread(target=stream_stdout, daemon=True)
    stderr_thread = threading.Thread(target=stream_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    returncode = proc.wait()
    stdout_thread.join()
    stderr_thread.join()

    return StageResult(
        name=" ".join(command),
        returncode=returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        duration_seconds=round(time.perf_counter() - started, 3),
    )


def _parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _write_stage_logs(output_dir: Path, result: StageResult, stem: str) -> tuple[Path, Path]:
    """Persist captured subprocess output so failures remain diagnosable."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / f"{stem}.stdout.log"
    stderr_path = output_dir / f"{stem}.stderr.log"
    stdout_path.write_text(result.stdout or "", encoding="utf-8")
    stderr_path.write_text(result.stderr or "", encoding="utf-8")
    return stdout_path, stderr_path


def _master_failed(result: StageResult) -> bool:
    """Detect both normal subprocess failures and master.py's swallowed failures."""
    if not result.ok:
        return True
    return MASTER_FAILURE_MARKER in result.stdout or MASTER_FAILURE_MARKER in result.stderr


def _master_failure_report(result: StageResult, output_dir: Path) -> str:
    stdout_path, stderr_path = _write_stage_logs(output_dir, result, "master")
    parts = [
        f"MASTER COLLECTOR FAILED (exit code {result.returncode}).",
        "The existing master.py can print a failure banner while still returning exit code 0; the orchestrator detected that banner.",
    ]
    if result.stdout.strip():
        parts.append(f"MASTER STDOUT:\n{result.stdout.strip()}")
    if result.stderr.strip():
        parts.append(f"MASTER STDERR:\n{result.stderr.strip()}")
    parts.append(f"Full stdout saved to: {stdout_path}")
    parts.append(f"Full stderr saved to: {stderr_path}")
    return "\n\n".join(parts)


def _require_output_folder(path: Path, master_result: StageResult) -> None:
    if not path.exists() or not path.is_dir():
        raise RuntimeError(f"Master output directory was not created: {path}")

    missing = [name for name in MASTER_FINAL_OUTPUTS if not (path / name).exists()]
    if missing:
        stdout_path, stderr_path = _write_stage_logs(path, master_result, "master")
        raise RuntimeError(
            "Master did not produce the complete final graph output. "
            f"Missing: {missing}\n"
            f"Partial collector output was kept at: {path}\n"
            f"Master stdout log: {stdout_path}\n"
            f"Master stderr log: {stderr_path}\n"
            "The Phase 1 updater was NOT started."
        )


def ingest_plant(
    plant_name: str,
    *,
    output_dir: Path | None = None,
    store_root: Path | None = None,
    neo4j_uri: str | None = None,
    neo4j_user: str | None = None,
    neo4j_password: str | None = None,
    neo4j_database: str | None = None,
    python_executable: str | None = None,
    no_cache: bool = False,
) -> dict[str, Any]:
    """Run master -> Phase 1 -> Phase 2 for one plant.

    Raises RuntimeError on any failed stage. Returns a JSON-serializable
    report on success.
    """
    plant_name = plant_name.strip()
    if not plant_name:
        raise ValueError("plant_name must not be empty")

    project_root = _project_root()
    phase1_root = _phase1_root()
    python_exec = python_executable or sys.executable

    # Keep collector output outside phase1_release, exactly like a direct
    # master.py invocation from the project root.
    if output_dir is None:
        output_dir = project_root / _safe_plant_name(plant_name)
    else:
        output_dir = Path(output_dir).resolve()

    if store_root is None:
        store_root = phase1_root / "data" / "canonical"
    else:
        store_root = Path(store_root).resolve()

    # Never reuse a partial or stale collector folder. This prevents a failed
    # previous run from masquerading as a successful fresh collection.
    if output_dir.exists():
        raise RuntimeError(
            f"Output directory already exists: {output_dir}\n"
            "Remove the failed/stale collector folder or choose a different --output path before retrying."
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(project_root), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    env["VANA_SHARED_CACHE_DIR"] = str(
        phase1_root / "data" / "shared_cache"
    )
    env["VANA_SHARED_HTTP_CACHE_ENABLED"] = "0" if no_cache else "1"

    cache_prepare_stats = {}
    if not no_cache:
        cache_prepare_stats = prepare_plant_caches(
            project_root,
            output_dir,
        )
    if neo4j_uri is not None:
        env["NEO4J_URI"] = neo4j_uri
    if neo4j_user is not None:
        env["NEO4J_USER"] = neo4j_user
    if neo4j_password is not None:
        env["NEO4J_PASSWORD"] = neo4j_password
    if neo4j_database is not None:
        env["NEO4J_DATABASE"] = neo4j_database

    stages: list[StageResult] = []

    # ------------------------------------------------------------------
    # Stage 1: existing master.py. DO NOT MODIFY MASTER.
    # ------------------------------------------------------------------
    master_cmd = [
        python_exec,
        str(project_root / "master.py"),
        "--plant",
        plant_name,
        "--output",
        str(output_dir),
    ]
    if no_cache:
        master_cmd.append("--no-cache")

    result = _run(master_cmd, cwd=project_root, env=env)
    stages.append(result)

    if _master_failed(result):
        raise RuntimeError(_master_failure_report(result, output_dir))

    _require_output_folder(output_dir, result)

    cache_publish_stats = {}
    if not no_cache:
        cache_publish_stats = publish_plant_caches(
            project_root,
            output_dir,
        )

    # ------------------------------------------------------------------
    # Stage 2: existing Phase 1 updater.
    # ------------------------------------------------------------------
    updater_cmd = [
        python_exec,
        "-m",
        "data_manager.updater",
        str(output_dir),
        "--store",
        str(store_root),
    ]
    result = _run(updater_cmd, cwd=phase1_root, env=env)
    stages.append(result)
    if not result.ok:
        stdout_path, stderr_path = _write_stage_logs(output_dir, result, "phase1")
        raise RuntimeError(
            _format_stage_failure(
                "PHASE 1 DATA MANAGER",
                result,
                extra=[
                    f"Phase 1 stdout log: {stdout_path}",
                    f"Phase 1 stderr log: {stderr_path}",
                ],
            )
        )

    phase1_report = _parse_json_stdout(result.stdout)
    if phase1_report is None:
        stdout_path, stderr_path = _write_stage_logs(output_dir, result, "phase1")
        raise RuntimeError(
            "Phase 1 completed but did not return the expected JSON report.\n"
            f"Phase 1 stdout log: {stdout_path}\n"
            f"Phase 1 stderr log: {stderr_path}\n"
            "Canonical data may have been updated; do not retry blindly until the store is inspected."
        )

    # ------------------------------------------------------------------
    # Stage 3: existing Phase 2 Neo4j sync.
    # ------------------------------------------------------------------
    if not env.get("NEO4J_URI") or not env.get("NEO4J_USER") or env.get("NEO4J_PASSWORD") is None:
        raise RuntimeError(
            "Phase 1 completed, but Neo4j credentials are not available. "
            "Set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, and NEO4J_DATABASE."
        )

    database = env.get("NEO4J_DATABASE") or "neo4j"
    sync_cmd = [
        python_exec,
        "-m",
        "data_manager.neo4j_sync",
        "--store",
        str(store_root),
        "--once",
        "--database",
        database,
    ]
    result = _run(sync_cmd, cwd=phase1_root, env=env)
    stages.append(result)
    if not result.ok:
        stdout_path, stderr_path = _write_stage_logs(output_dir, result, "phase2")
        raise RuntimeError(
            _format_stage_failure(
                "PHASE 2 NEO4J SYNC",
                result,
                extra=[
                    f"Phase 2 stdout log: {stdout_path}",
                    f"Phase 2 stderr log: {stderr_path}",
                ],
            )
        )

    phase2_report = _parse_json_stdout(result.stdout)
    if phase2_report is None:
        stdout_path, stderr_path = _write_stage_logs(output_dir, result, "phase2")
        raise RuntimeError(
            "Phase 2 completed but did not return the expected JSON report.\n"
            f"Phase 2 stdout log: {stdout_path}\n"
            f"Phase 2 stderr log: {stderr_path}\n"
            "Inspect Neo4j before considering ingestion complete."
        )

    return {
        "status": "success",
        "plant": plant_name,
        "collector_output": str(output_dir),
        "canonical_store": str(store_root),
        "shared_cache": {
            "prepared": cache_prepare_stats,
            "published": cache_publish_stats,
        },
        "phase1": phase1_report,
        "phase2": phase2_report,
        "stages": [
            {
                "name": stage.name,
                "returncode": stage.returncode,
                "duration_seconds": stage.duration_seconds,
            }
            for stage in stages
        ],
    }


def _format_stage_failure(
    label: str,
    result: StageResult,
    extra: list[str] | None = None,
) -> str:
    parts = [f"{label} failed (exit code {result.returncode})."]
    if result.stdout.strip():
        parts.append(f"STDOUT:\n{result.stdout.strip()}")
    if result.stderr.strip():
        parts.append(f"STDERR:\n{result.stderr.strip()}")
    if extra:
        parts.extend(extra)
    return "\n\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Production ingestion orchestrator: master.py -> Phase 1 -> Neo4j sync."
    )
    parser.add_argument(
        "--plant",
        required=True,
        help='Plant name, e.g. "Phyllanthus reticulatus"',
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional collector output directory. Relative paths are resolved from the project root (outside phase1_release).",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=Path("data/canonical"),
        help="Phase 1 canonical store path, relative to phase1_release unless absolute.",
    )
    parser.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI"))
    parser.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER"))
    parser.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE"))
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    try:
        output_dir = args.output
        if output_dir is not None and not output_dir.is_absolute():
            output_dir = (_project_root() / output_dir).resolve()

        store_root = args.store
        if not store_root.is_absolute():
            store_root = (_phase1_root() / store_root).resolve()

        report = ingest_plant(
            args.plant,
            output_dir=output_dir,
            store_root=store_root,
            neo4j_uri=args.neo4j_uri,
            neo4j_user=args.neo4j_user,
            neo4j_password=args.neo4j_password,
            neo4j_database=args.database,
            no_cache=args.no_cache,
        )
        print(json.dumps(report, indent=2))
    except Exception as exc:
        print(f"INGESTION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
