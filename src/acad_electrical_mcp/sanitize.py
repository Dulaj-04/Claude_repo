"""Slim a heavy DXF (for example a raw DIALux export) into a working copy: erase calculation/mesh
layers and optionally scale the rest. The source is only read."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox
from ezdxf.math import Matrix44

from .bases import norm
from .model import sha256_file

FROM_UNITS = {"m": 1000.0, "metres": 1000.0, "cm": 10.0, "inch": 25.4, "inches": 25.4, "mm": 1.0}


def _stats(doc) -> dict[str, Any]:
    msp = doc.modelspace()
    layers: dict[str, int] = {}
    for e in msp:
        layers[e.dxf.layer] = layers.get(e.dxf.layer, 0) + 1
    ext = None
    try:
        b = bbox.extents(msp, fast=True)
        if b.has_data:
            ext = [round(b.extmin.x, 2), round(b.extmin.y, 2), round(b.extmax.x, 2), round(b.extmax.y, 2)]
    except Exception:  # noqa: BLE001
        pass
    return {"entities": sum(layers.values()), "extents": ext,
            "top_layers": dict(sorted(layers.items(), key=lambda kv: -kv[1])[:8])}


def sanitize_dxf(src: str, dest: str, strip_layers: list[str], scale: float | None = None,
                 from_units: str | None = None, overwrite: bool = False,
                 is_protected=lambda p: False) -> dict[str, Any]:
    s, d = Path(src), Path(dest)
    if not s.is_file():
        raise ValueError(f"File not found: {src}")
    if s.suffix.lower() != ".dxf":
        raise ValueError("dxf_sanitize reads DXF only. For a DWG: open it in AutoCAD and use "
                         "live_sanitize on a copy, or save it as DXF.")
    if norm(s) == norm(d) or is_protected(d):
        raise ValueError("The destination is the source or a registered base file: choose a new name.")
    if d.exists() and not overwrite:
        raise ValueError(f"{d} already exists. Pass overwrite=true only to start again.")
    if scale is not None and from_units:
        raise ValueError("Give scale or from_units, not both.")
    if from_units:
        if from_units.lower() not in FROM_UNITS:
            raise ValueError(f"from_units must be one of {sorted(FROM_UNITS)}")
        scale = FROM_UNITS[from_units.lower()]
    if scale is not None and not (math.isfinite(scale) and scale > 0):
        raise ValueError("scale must be a positive number")
    src_hash = sha256_file(s)
    doc = ezdxf.readfile(str(s))
    before = _stats(doc)
    msp = doc.modelspace()
    drop = {x.lower() for x in strip_layers}
    victims = [e for e in msp if e.dxf.layer.lower() in drop]
    for e in victims:
        msp.delete_entity(e)
    skipped = 0
    if scale is not None and scale != 1.0:
        m = Matrix44.scale(scale)
        for e in list(msp):
            try:
                e.transform(m)
            except (NotImplementedError, TypeError, ValueError):
                skipped += 1
    doc.header["$INSUNITS"] = 4
    d.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(d))
    after = _stats(ezdxf.readfile(str(d)))
    return {"working_copy": str(d.resolve()), "source": str(s.resolve()),
            "source_unchanged": sha256_file(s) == src_hash, "erased_entities": len(victims),
            "scale_applied": scale, "not_scaled": skipped, "units_now": "millimetres (INSUNITS=4)",
            "before": before, "after": after}
