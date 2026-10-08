from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

NODE_FILES = [
    "Plant.csv",
    "PlantPart.csv",
    "Phytochemicals.csv",
    "TherapeuticUses.csv",
    "Bioactivities.csv",
    "Targets.csv",
    "Documents.csv",
]
REL_FILES = [
    "HAS_PART.csv",
    "CONTAINS.csv",
    "USED_FOR.csv",
    "HAS_BIOACTIVITY.csv",
    "MEASURED_ON.csv",
    "REPORTED_IN.csv",
]
ALL_GRAPH_FILES = NODE_FILES + REL_FILES


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class CanonicalStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.current = self.root / "current"
        self.snapshots = self.root / "snapshots"
        self.current.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)

    def snapshot(self, files: Iterable[str] = ALL_GRAPH_FILES) -> Path:
        stamp = utc_stamp()
        dst = self.snapshots / stamp
        dst.mkdir(parents=True, exist_ok=False)
        copied = []
        for name in files:
            src = self.current / name
            if src.exists():
                shutil.copy2(src, dst / name)
                copied.append(name)
        (dst / "snapshot_manifest.json").write_text(
            json.dumps({"created_at": stamp, "files": copied}, indent=2), encoding="utf-8"
        )
        return dst

    def available(self) -> list[str]:
        return sorted(p.name for p in self.current.glob("*.csv"))
