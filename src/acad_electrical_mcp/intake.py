"""Inventory a folder of project inputs: what each file is, which floor it belongs to, what is
missing, and whether the floors agree on units and scale. Read-only; nothing is modified."""

from __future__ import annotations

import os
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import ezdxf

from .model import sha256_file

DWG_VERSIONS = {"AC1009": "R12", "AC1012": "R13", "AC1014": "R14", "AC1015": "2000", "AC1018": "2004",
                "AC1021": "2007", "AC1024": "2010", "AC1027": "2013", "AC1032": "2018 or newer"}
KINDS = {".dwg": "dwg", ".dxf": "dxf", ".xlsx": "excel", ".xlsm": "excel", ".xls": "excel",
         ".csv": "table", ".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image",
         ".docx": "word", ".md": "text", ".txt": "text", ".evo": "dialux_project",
         ".rsl": "dialux_internal", ".ies": "photometry", ".ldt": "photometry"}
WORDS = {"ground": "GF", "first": "1F", "second": "2F", "third": "3F", "fourth": "4F",
         "fifth": "5F", "sixth": "6F", "1st": "1F", "2nd": "2F", "3rd": "3F", "4th": "4F",
         "5th": "5F", "6th": "6F"}
DLX_LAYERS = ("DLX_OBJ", "DLX_CALC", "DLX_APERT")
HASH_LIMIT = 400 * 1024 * 1024
DXF_LIMIT = 90 * 1024 * 1024
_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"


def floor_of(name: str) -> str | None:
    n = name.replace("_", " ")
    if re.search(_B + r"(GF|G\.F\.?|ground)" + _E, n, re.I):
        return "GF"
    m = re.search(_B + r"([0-9])\s?F" + _E, n, re.I)
    if m:
        return f"{m.group(1)}F"
    m = re.search(_B + r"floor\s*([0-9])" + _E, n, re.I)
    if m:
        return f"{m.group(1)}F"
    m = re.search(_B + r"(first|second|third|fourth|fifth|sixth|[1-6](?:st|nd|rd|th))" + _E, n, re.I)
    return WORDS.get(m.group(1).lower()) if m else None


def role_hint(rel: str, kind: str) -> str | None:
    low = rel.lower()
    if kind in ("dwg", "dxf"):
        if "dialux" in low or "dlx" in low:
            return "dialux_export"
        if "layout" in low or "ac_socket" in low or "socket" in low:
            return "layout"
        if "base" in low or "architect" in low or "cad for" in low or "floor plan" in low:
            return "architectural_base"
        if "electrical" in low or "lighting" in low:
            return "electrical_drawing"
    if kind == "excel":
        for key in ("demand", "cable", "short", "ac_", "socket", "load", "boq"):
            if key in low:
                return f"calc_{key.strip('_')}"
    return None


def _dxf_facts(path: Path) -> dict[str, Any]:
    doc = ezdxf.readfile(str(path))
    ins = int(doc.header.get("$INSUNITS", 0))
    ext = None
    try:
        lo, hi = doc.header.get("$EXTMIN"), doc.header.get("$EXTMAX")
        if lo and hi and abs(hi[0]) < 1e19 and hi[0] >= lo[0]:
            ext = [round(lo[0], 2), round(lo[1], 2), round(hi[0], 2), round(hi[1], 2)]
    except Exception:  # noqa: BLE001
        ext = None
    if ext is None:
        from ezdxf import bbox
        try:
            b = bbox.extents(doc.modelspace(), fast=True)
            if b.has_data:
                ext = [round(b.extmin.x, 2), round(b.extmin.y, 2), round(b.extmax.x, 2), round(b.extmax.y, 2)]
        except Exception:  # noqa: BLE001
            pass
    layers = Counter(e.dxf.layer for e in doc.modelspace())
    types = Counter(e.dxftype() for e in doc.modelspace())
    return {"insunits": ins, "extents": ext, "entities": sum(layers.values()),
            "span": round(max(ext[2] - ext[0], ext[3] - ext[1]), 2) if ext else None,
            "top_layers": dict(layers.most_common(6)),
            "dialux_layers": {k: v for k, v in layers.items() if k.upper() in DLX_LAYERS},
            "meshes": types.get("MESH", 0), "blocks_inserted": types.get("INSERT", 0)}


def _excel_sheets(path: Path) -> list[str] | None:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True)
        names = list(wb.sheetnames)
        wb.close()
        return names
    except Exception:  # noqa: BLE001
        return None


def scan(folder: str, expected_floors: list[str] | None = None, max_files: int = 400,
         max_depth: int = 4) -> dict[str, Any]:
    root = Path(folder)
    if not root.is_dir():
        raise ValueError(f"Folder not found: {folder}")
    expected = expected_floors or ["GF", "1F", "2F", "3F", "4F", "5F", "6F"]
    files: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped = 0
    for dp, dn, fn in os.walk(root):
        depth = len(Path(dp).relative_to(root).parts)
        dn[:] = [d for d in sorted(dn) if not d.startswith((".", "$")) and depth < max_depth]
        for f in sorted(fn):
            if f.startswith(("~$", ".")) or f.lower().endswith((".bak", ".sv$", ".tmp", ".dwl", ".dwl2")):
                continue
            if len(files) >= max_files:
                skipped += 1
                continue
            p = Path(dp) / f
            kind = KINDS.get(p.suffix.lower(), "other")
            rel = str(p.relative_to(root))
            size = p.stat().st_size
            rec: dict[str, Any] = {"file": rel, "kind": kind, "size_mb": round(size / 1048576, 2),
                                   "floor": floor_of(p.stem) or floor_of(str(p.parent.name)),
                                   "role_hint": role_hint(rel, kind)}
            rec["sha256"] = sha256_file(p)[:16] if size <= HASH_LIMIT else None
            if kind == "dwg":
                head = p.read_bytes()[:6].decode("ascii", "replace")
                rec["dwg_version"] = DWG_VERSIONS.get(head, head)
            elif kind == "dxf":
                if size <= DXF_LIMIT:
                    try:
                        rec.update(_dxf_facts(p))
                    except Exception as exc:  # noqa: BLE001
                        rec["read_error"] = f"{type(exc).__name__}: {exc}"
                else:
                    rec["note"] = "too large to read here; use dxf_sanitize or open it in AutoCAD"
            elif kind == "excel":
                rec["sheets"] = _excel_sheets(p)
            files.append(rec)
    by_kind = Counter(f["kind"] for f in files)
    # floors per role
    floors_by_role: dict[str, set[str]] = defaultdict(set)
    for f in files:
        if f["floor"] and f["role_hint"]:
            floors_by_role[f["role_hint"]].add(f["floor"])
    missing = {role: [x for x in expected if x not in got]
               for role, got in floors_by_role.items() if len(got) >= 2}
    missing = {k: v for k, v in missing.items() if v}
    # duplicates
    seen: dict[str, list[str]] = defaultdict(list)
    for f in files:
        if f["sha256"]:
            seen[f["sha256"]].append(f["file"])
    dup = [v for v in seen.values() if len(v) > 1]
    # unit/scale agreement between floors of the same role
    scale_flags: list[str] = []
    by_role: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        if f.get("span") and f["floor"] and f["role_hint"]:
            by_role[f["role_hint"]].append(f)
    for role, group in by_role.items():
        if len(group) < 3:
            continue
        med = statistics.median(g["span"] for g in group)
        for g in group:
            r = g["span"] / med if med else 1
            if r < 0.8 or r > 1.25:
                scale_flags.append(f"{g['file']} ({role}): span {g['span']} is {r:.2f}x the median "
                                   f"({med}) of its floors: wrong scale or units?")
        units = {g["insunits"] for g in group}
        if len(units) > 1:
            scale_flags.append(f"{role}: floors declare different units {sorted(units)} in their headers")
    heavy = [f["file"] for f in files if f.get("dialux_layers") or f.get("meshes", 0) > 5000]
    if heavy:
        warnings.append(f"Heavy DIALux content (mesh/calculation layers) in {heavy}: work on a "
                        "sanitised copy (dxf_sanitize / live_sanitize), never on the raw export.")
    if any(f["kind"] == "dwg" for f in files):
        warnings.append("DWG contents cannot be read here without AutoCAD or the ODA File Converter: "
                        "only name, size, version and floor are listed. Open them in AutoCAD and use "
                        "live_inventory, or save as DXF.")
    if any(f["kind"] == "dialux_project" for f in files):
        warnings.append(".evo is a DIALux project container; it cannot be imported. Export the layout "
                        "as DWG/DXF and the luminaire list as Excel/CSV from DIALux.")
    if any(f["kind"] == "dialux_internal" for f in files):
        warnings.append(".rsl files are DIALux internal result indexes with no usable luminaire data.")
    if skipped:
        warnings.append(f"{skipped} more file(s) not listed (max_files={max_files}).")
    return {"folder": str(root.resolve()), "file_count": len(files), "by_kind": dict(by_kind),
            "files": files, "floors_found": {k: sorted(v) for k, v in floors_by_role.items()},
            "expected_floors": expected, "missing_floors": missing, "duplicates": dup,
            "scale_flags": scale_flags, "warnings": warnings,
            "ask_the_user": (["Missing floors: " + "; ".join(f"{k}: {v}" for k, v in missing.items())]
                             if missing else []) + (scale_flags[:3]) + (
                                 [f"Duplicate files: {dup[:3]}"] if dup else []),
            "status": "DRAFT inventory: nothing was changed"}
