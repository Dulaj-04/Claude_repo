"""Import lighting layouts exported from DIALux (DWG/DXF) and build lighting schedules.

DIALux's drawing export is read generically: luminaires are found as block inserts, a mapping from
block names to device types is chosen explicitly (``inspect`` lists what the file contains), and an
optional coordinate transform aligns the export with the architectural drawing. Wattages and lumens
are only ever taken from data you supply (a luminaire list, or per-type values); a luminaire without
a known wattage is reported as missing and never given an invented value.
"""

from __future__ import annotations

import csv
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .backends.base import BackendError
from .backends.dxf_backend import load_dxf
from .model import sha256_file

LUMINAIRE_HINT = re.compile(r"lum|light|leuchte|downlight|panel|troffer|strip|spot|luminar", re.I)
UNIT_TO_M = {"millimetres": 0.001, "centimetres": 0.01, "metres": 1.0, "inches": 0.0254,
             "feet": 0.3048}


# ----------------------------------------------------------------------------- transform
def make_transform(dx: float = 0.0, dy: float = 0.0, rotation: float = 0.0,
                   scale: float = 1.0) -> dict[str, float]:
    for n, v in (("dx", dx), ("dy", dy), ("rotation", rotation), ("scale", scale)):
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{n} must be a finite number, got {v!r}")
    if scale <= 0:
        raise ValueError("scale must be positive")
    return {"dx": dx, "dy": dy, "rotation": rotation, "scale": scale}


def apply_transform(t: dict[str, float], x: float, y: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(t["rotation"])), math.sin(math.radians(t["rotation"]))
    return (t["scale"] * (x * c - y * s) + t["dx"], t["scale"] * (x * s + y * c) + t["dy"])


def fit_two_points(src_a, src_b, dst_a, dst_b) -> dict[str, Any]:
    """Transform (rotation + uniform scale + shift) that maps src_a->dst_a and src_b->dst_b.
    Pick two points you can identify in both drawings, e.g. two column or wall corners."""
    sv = (src_b[0] - src_a[0], src_b[1] - src_a[1])
    dv = (dst_b[0] - dst_a[0], dst_b[1] - dst_a[1])
    sl, dl = math.hypot(*sv), math.hypot(*dv)
    if sl < 1e-9 or dl < 1e-9:
        raise ValueError("The two points of each pair must be different")
    scale = dl / sl
    rot = math.degrees(math.atan2(dv[1], dv[0]) - math.atan2(sv[1], sv[0]))
    t = make_transform(0.0, 0.0, rot, scale)
    ax, ay = apply_transform(t, *src_a)
    t["dx"], t["dy"] = dst_a[0] - ax, dst_a[1] - ay
    bx, by = apply_transform(t, *src_b)
    t["residual"] = math.hypot(bx - dst_b[0], by - dst_b[1])
    return t


# ----------------------------------------------------------------------------- reading
def _inserts(doc, include_nested: bool, depth: int = 3):
    def walk(entities, level):
        for e in entities:
            if e.dxftype() != "INSERT":
                continue
            yield e
            if include_nested and level < depth:
                try:
                    inner = list(e.virtual_entities())
                except Exception:  # noqa: BLE001 - odd block definitions must not abort the import
                    continue
                yield from walk(inner, level + 1)

    yield from walk(doc.modelspace(), 0)


def _attribs(e) -> dict[str, str]:
    try:
        return {a.dxf.tag.upper(): a.dxf.text for a in e.attribs}
    except Exception:  # noqa: BLE001
        return {}


def inspect(path: str, include_nested: bool = False) -> dict[str, Any]:
    """What a DIALux DWG/DXF export contains: block names with counts, layers, attributes, text."""
    doc = load_dxf(path)
    blocks: dict[str, dict[str, Any]] = {}
    for e in _inserts(doc, include_nested):
        name = e.dxf.name
        if name.startswith("*"):
            continue
        b = blocks.setdefault(name, {"count": 0, "layers": Counter(), "sample_attributes": {},
                                     "x": [], "y": []})
        b["count"] += 1
        b["layers"][e.dxf.layer] += 1
        if not b["sample_attributes"]:
            b["sample_attributes"] = _attribs(e)
        b["x"].append(e.dxf.insert.x)
        b["y"].append(e.dxf.insert.y)
    out_blocks = []
    for name, b in sorted(blocks.items(), key=lambda kv: -kv[1]["count"]):
        out_blocks.append({
            "block": name, "count": b["count"], "layers": dict(b["layers"]),
            "sample_attributes": b["sample_attributes"],
            "extent": [round(min(b["x"]), 2), round(min(b["y"]), 2),
                       round(max(b["x"]), 2), round(max(b["y"]), 2)],
            "looks_like_luminaire": bool(LUMINAIRE_HINT.search(name)),
        })
    layer_counts: Counter = Counter(e.dxf.layer for e in doc.modelspace())
    texts = [t.dxf.text for t in doc.modelspace().query("TEXT")][:40]
    ins = int(doc.header.get("$INSUNITS", 0))
    return {
        "path": str(Path(path).resolve()), "sha256": sha256_file(path),
        "dxf_version": doc.dxfversion, "header_insunits": ins,
        "blocks": out_blocks, "block_total": sum(b["count"] for b in out_blocks),
        "entities_per_layer": dict(layer_counts), "sample_text": texts,
        "warnings": ([] if out_blocks else [
            "No block inserts found. If luminaires are drawn as loose lines, import is not "
            "possible from this file: export again with luminaires as blocks, or try "
            "include_nested=true."]) + (["Header units undeclared; give scale/transform "
                                        "explicitly."] if ins == 0 else []),
    }


def extract(path: str, block_names: list[str] | None = None, layers: list[str] | None = None,
            include_nested: bool = False, transform: dict[str, float] | None = None
            ) -> list[dict[str, Any]]:
    """Block inserts matching the filters, positions transformed into drawing coordinates."""
    if not block_names and not layers:
        raise ValueError("Say what to import: give block_names and/or layers (see dialux_inspect).")
    t = transform or make_transform()
    wanted = {b.lower() for b in block_names or []}
    lay = {x.lower() for x in layers or []}
    doc = load_dxf(path)
    out = []
    for e in _inserts(doc, include_nested):
        name = e.dxf.name
        if name.startswith("*"):
            continue
        if wanted and name.lower() not in wanted:
            continue
        if lay and e.dxf.layer.lower() not in lay:
            continue
        x, y = apply_transform(t, e.dxf.insert.x, e.dxf.insert.y)
        out.append({"block": name, "layer": e.dxf.layer, "x": round(x, 4), "y": round(y, 4),
                    "rotation": round((e.dxf.get("rotation", 0.0) + t["rotation"]) % 360, 4),
                    "attributes": _attribs(e)})
    out.sort(key=lambda d: (round(d["y"], 1), d["x"], d["block"]))
    return out


# ----------------------------------------------------------------------------- luminaire list
_SYN = {
    "type": ("type", "name", "luminaire", "article", "designation", "description", "product",
             "block", "bezeichnung", "typ"),
    "qty": ("qty", "quantity", "count", "number", "no", "pcs", "amount", "anzahl", "stuck"),
    "watts": ("watts", "watt", "power", "p", "wattage", "luminaire power", "p [w]", "w"),
    "lumens": ("lumens", "lumen", "flux", "luminous flux", "phi", "φ", "lm", "luminaire flux"),
}


def _norm(h: str) -> str:
    return re.sub(r"[\s*]+", " ", str(h or "").strip().lower()).strip()


def _pick(headers: list[str], role: str, override: str | None) -> str | None:
    if override:
        for h in headers:
            if _norm(h) == _norm(override):
                return h
        raise ValueError(f"Column {override!r} not found. Columns: {headers}")
    for syn in _SYN[role]:
        for h in headers:
            if _norm(h) == syn or _norm(h).split(" [")[0] == syn:
                return h
    return None


def _num(v: Any) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v).replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def read_luminaire_list(path: str, sheet: str | None = None, columns: dict[str, str] | None = None
                        ) -> dict[str, Any]:
    """Read a luminaire list (CSV or XLSX) exported from DIALux/Excel into normalised rows."""
    p = Path(path)
    if not p.exists():
        raise BackendError(f"File not found: {path}")
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as exc:
            raise BackendError("Reading .xlsx needs openpyxl: pip install openpyxl "
                               "(or reinstall the package). Or save the sheet as CSV.") from exc
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    else:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        lines = text.splitlines()
        # delimiter = the one that splits the most of the first lines into 2+ fields (DIALux
        # exports often have a title line above the table, which defeats csv.Sniffer)
        delim = max(",;\t", key=lambda d: sum(len(ln.split(d)) >= 2 for ln in lines[:20]))
        rows = list(csv.reader(lines, delimiter=delim))
    rows = [r for r in rows if any(str(c).strip() for c in r if c is not None)]
    if not rows:
        raise ValueError("The file has no rows.")
    # header = first row that contains a recognisable type column
    hi = 0
    for i, r in enumerate(rows[:15]):
        if _pick([str(c) for c in r], "type", None):
            hi = i
            break
    headers = [str(c) if c is not None else "" for c in rows[hi]]
    cols = columns or {}
    ct, cq = _pick(headers, "type", cols.get("type")), _pick(headers, "qty", cols.get("qty"))
    cw, cl = _pick(headers, "watts", cols.get("watts")), _pick(headers, "lumens", cols.get("lumens"))
    if ct is None:
        raise ValueError(f"Cannot find the luminaire type/name column in {headers}. Pass "
                         "columns={'type': '<header>'}.")
    out = []
    for r in rows[hi + 1:]:
        d = dict(zip(headers, r, strict=False))
        name = str(d.get(ct) or "").strip()
        if not name:
            continue
        out.append({"type": name, "qty": _num(d.get(cq)) if cq else None,
                    "watts": _num(d.get(cw)) if cw else None,
                    "lumens": _num(d.get(cl)) if cl else None})
    return {"path": str(p.resolve()), "sha256": sha256_file(p),
            "columns_used": {"type": ct, "qty": cq, "watts": cw, "lumens": cl},
            "rows": out,
            "missing_watts": [r["type"] for r in out if r["watts"] is None]}


def specs_from_list(listing: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """type -> {'watts','lumens'} lookup from a read_luminaire_list result."""
    return {r["type"].lower(): {"watts": r["watts"], "lumens": r["lumens"]}
            for r in listing["rows"]}


# ----------------------------------------------------------------------------- model mapping
def room_of(model: dict[str, Any], x: float, y: float) -> str | None:
    hits = [r for r in model["rooms"]
            if r["bounds"][0] <= x <= r["bounds"][2] and r["bounds"][1] <= y <= r["bounds"][3]]
    if not hits:
        return None
    hits.sort(key=lambda r: (r["bounds"][2] - r["bounds"][0]) * (r["bounds"][3] - r["bounds"][1]))
    return hits[0]["id"]  # smallest containing room wins


def build_device_specs(model: dict[str, Any], found: list[dict[str, Any]],
                       type_map: dict[str, str] | None, specs: dict[str, dict],
                       watts_by_type: dict[str, float] | None, circuit: str | None,
                       source_tag: str, label_attr: str | None = None
                       ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tmap = {k.lower(): v for k, v in (type_map or {}).items()}
    wmap = {k.lower(): v for k, v in (watts_by_type or {}).items()}
    out, outside, unknown_watts = [], 0, Counter()
    for f in found:
        dtype = tmap.get(f["block"].lower(), "luminaire")
        spec = specs.get(f["block"].lower(), {})
        watts = wmap.get(f["block"].lower(), spec.get("watts"))
        if watts is None and dtype in ("luminaire", "emergency"):
            unknown_watts[f["block"]] += 1
        room = room_of(model, f["x"], f["y"])
        outside += room is None
        d = {"type": dtype, "x": f["x"], "y": f["y"], "rotation": f["rotation"], "room": room,
             "circuit": circuit, "luminaire_type": f["block"], "watts": watts,
             "lumens": spec.get("lumens"), "source": source_tag}
        if label_attr and f["attributes"].get(label_attr.upper()):
            d["dialux_label"] = f["attributes"][label_attr.upper()]
        out.append(d)
    return out, {"outside_all_rooms": outside, "types_without_watts": dict(unknown_watts)}


def extent_check(model: dict[str, Any], devs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compare the imported luminaires' span with the rooms' span. A wrong unit or scale shows up
    as a ratio far from 1 even when points happen to fall inside some room."""
    if not devs or not model.get("rooms"):
        return None
    xs, ys = [d["x"] for d in devs], [d["y"] for d in devs]
    span_i = max(max(xs) - min(xs), max(ys) - min(ys))
    b = [r["bounds"] for r in model["rooms"]]
    span_r = max(max(v[2] for v in b) - min(v[0] for v in b),
                 max(v[3] for v in b) - min(v[1] for v in b))
    if span_r <= 0:
        return None
    ratio = span_i / span_r
    return {"imported_span": round(span_i, 3), "rooms_span": round(span_r, 3),
            "ratio": round(ratio, 4), "suspicious": span_i > 0 and (ratio < 0.02 or ratio > 5)}


# ----------------------------------------------------------------------------- schedule
def lighting_schedule(model: dict[str, Any], watts_by_type: dict[str, float] | None = None,
                      unit_to_m: float | None = None) -> dict[str, Any]:
    """Counts and installed lighting load per room and floor from the model's luminaires.
    Wattage comes from the device, then ``watts_by_type``; otherwise it is reported as missing."""
    wmap = {k.lower(): v for k, v in (watts_by_type or {}).items()}
    if unit_to_m is None and model.get("units") in UNIT_TO_M:
        unit_to_m = UNIT_TO_M[model["units"]]
    rooms = {r["id"]: r for r in model["rooms"]}
    per_room: dict[str, dict[str, Any]] = {}
    missing: Counter = Counter()
    for d in model["devices"]:
        if d["type"] not in ("luminaire", "emergency"):
            continue
        rid = d.get("room") or "(no room)"
        row = per_room.setdefault(rid, {"room": rid, "name": rooms.get(rid, {}).get("name"),
                                        "count": 0, "types": Counter(), "watts": 0.0,
                                        "luminaires_without_watts": 0})
        row["count"] += 1
        lt = d.get("luminaire_type") or d["type"]
        row["types"][lt] += 1
        w = d.get("watts")
        if w is None:
            w = wmap.get(lt.lower())
        if w is None:
            row["luminaires_without_watts"] += 1
            missing[lt] += 1
        else:
            row["watts"] += float(w)
    rows = []
    for rid, row in sorted(per_room.items()):
        r = rooms.get(rid)
        area = None
        if r and unit_to_m:
            b = r["bounds"]
            area = round((b[2] - b[0]) * (b[3] - b[1]) * unit_to_m ** 2, 2)
        row["types"] = dict(row["types"])
        row["watts"] = round(row["watts"], 2)
        row["area_m2"] = area
        row["w_per_m2"] = round(row["watts"] / area, 2) if area and not row[
            "luminaires_without_watts"] else None
        rows.append(row)
    total_w = round(sum(r["watts"] for r in rows), 2)
    return {
        "floor": model["floor"], "model_revision": model["revision"], "rooms": rows,
        "total_luminaires": sum(r["count"] for r in rows), "installed_watts": total_w,
        "installed_kw": round(total_w / 1000, 3),
        "types_without_watts": dict(missing),
        "complete": not missing,
        "notes": ["Installed load only: connected wattage of the luminaires listed, with no "
                  "diversity or demand factor, no driver/ballast loss unless included in the "
                  "supplied wattage, and no lux calculation."] + ([
                      f"Wattage missing for: {sorted(missing)}. The total is incomplete until "
                      "you supply watts_by_type or a luminaire list."] if missing else []) + ([
                      "Room areas need unit_to_m (metres per drawing unit); the model's unit text "
                      "was not usable." ] if not unit_to_m else []),
        "status": "DRAFT",
    }
