"""Protected base drawings: registered once, then never written to.

A base drawing (architectural plan, template) is the user's own clean file. Every floor is drawn in
its own copy made with ``copy_for_floor``; the live tools refuse to write while a registered base is
the active drawing, and ``check`` proves the originals are byte-for-byte unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .config import safe_name
from .model import sha256_file


def norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class BaseRegistry:
    def __init__(self, workspace: Path):
        self.file = workspace / ".bases.json"

    def _load(self) -> dict[str, Any]:
        return json.loads(self.file.read_text()) if self.file.exists() else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(data, indent=2, sort_keys=True))

    def register(self, path: str, role: str = "base") -> dict[str, Any]:
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"File not found: {path}")
        data = self._load()
        entry = {"path": str(p.resolve()), "role": role, "sha256": sha256_file(p),
                 "size": p.stat().st_size, "registered": time.strftime("%Y-%m-%d %H:%M")}
        data[norm(p)] = entry
        self._save(data)
        return entry

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._load().values(), key=lambda e: e["path"])

    def is_protected(self, path: str | Path | None) -> bool:
        return bool(path) and norm(path) in self._load()

    def check(self) -> dict[str, Any]:
        out = {"unchanged": [], "changed": [], "missing": []}
        for e in self.list():
            p = Path(e["path"])
            if not p.exists():
                out["missing"].append(e["path"])
            elif sha256_file(p) != e["sha256"]:
                out["changed"].append(e["path"])
            else:
                out["unchanged"].append(e["path"])
        out["ok"] = not out["changed"] and not out["missing"]
        return out

    def copy_for_floor(self, base: str, floor: str, dest_dir: str, name: str | None = None,
                       overwrite: bool = False) -> dict[str, Any]:
        """Copy a base drawing to a new working file for one floor. The base is only read."""
        safe_name(floor, "floor")
        src = Path(base)
        if not src.is_file():
            raise ValueError(f"Base file not found: {base}")
        if not self.is_protected(src):
            self.register(str(src), "base")  # reading a file as a base protects it from now on
        stem, ext = src.stem, src.suffix
        fname = name or f"{floor}_{stem}{ext}"
        if "/" in fname or "\\" in fname:
            raise ValueError("name must be a file name, not a path")
        dest = Path(dest_dir) / fname
        if norm(dest) == norm(src) or self.is_protected(dest):
            raise ValueError("The destination is a registered base file: choose a new name.")
        if dest.exists() and not overwrite:
            raise ValueError(f"{dest} already exists (it may hold work). Pass overwrite=true only "
                             "to start again.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        same = sha256_file(src) == sha256_file(dest)
        return {"working_copy": str(dest.resolve()), "copied_from": str(src.resolve()),
                "identical_to_base": same, "base_unchanged": self.check()["ok"],
                "next": "Open the working copy in AutoCAD and draw there. The base stays untouched."}
