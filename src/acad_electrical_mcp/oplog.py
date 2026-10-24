"""Operation log: what the tools did, in order, so work can be resumed after a dropped link."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class OpLog:
    def __init__(self, workspace: Path, project_fn):
        self.workspace = workspace
        self.project_fn = project_fn  # returns the active plan's project or None

    def _file(self) -> Path:
        project = self.project_fn() or "_unassigned"
        return self.workspace / project / "ops.jsonl"

    def append(self, entry: dict[str, Any]) -> None:
        f = self._file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}) + "\n")

    def tail(self, n: int = 30) -> list[dict[str, Any]]:
        f = self._file()
        if not f.exists():
            return []
        lines = f.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
