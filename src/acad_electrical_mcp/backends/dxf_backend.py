"""Offline backend built on ezdxf (cross-platform, no AutoCAD needed)."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.addons import odafc

from .base import BackendError, DrawingBackend

BLOCK_PREFIX = "ACADE_"


def load_dxf(path: str):
    """Read a DXF (or a DWG when the ODA File Converter is installed)."""
    p = Path(path)
    if not p.exists():
        raise BackendError(f"File not found: {path}")
    if p.suffix.lower() == ".dwg":
        if not odafc.is_installed():
            raise BackendError(
                "DWG input needs the ODA File Converter (https://www.opendesign.com/guestfiles/oda_file_converter) "
                "or the 'autocad' backend. Alternatively, save the drawing as DXF in AutoCAD and "
                "use that file."
            )
        return odafc.readfile(str(p))
    try:
        return ezdxf.readfile(str(p))
    except (OSError, ezdxf.DXFError) as exc:
        raise BackendError(f"Cannot read {path}: {exc}") from exc


def build_symbol(blk, dtype: str, r: float, th: float) -> None:
    """Draw the symbol geometry (on layer 0 so it inherits the device layer colour)."""
    a = {"layer": "0"}
    if dtype == "luminaire":
        blk.add_circle((0, 0), r, dxfattribs=a)
        k = r * math.sqrt(0.5)
        blk.add_line((-k, -k), (k, k), dxfattribs=a)
        blk.add_line((-k, k), (k, -k), dxfattribs=a)
    elif dtype == "switch":
        blk.add_circle((0, 0), r * 0.5, dxfattribs=a)
        blk.add_line((r * 0.35, r * 0.35), (r * 1.3, r * 1.3), dxfattribs=a)
        blk.add_line((r * 1.3, r * 1.3), (r * 1.3 + r * 0.3, r * 1.3 - r * 0.3), dxfattribs=a)
    elif dtype == "socket":
        blk.add_circle((0, 0), r, dxfattribs=a)
        blk.add_line((-r, 0), (r, 0), dxfattribs=a)
        blk.add_line((0, 0), (0, -r), dxfattribs=a)
    elif dtype == "data":
        blk.add_lwpolyline([(-r, -r * 0.8), (r, -r * 0.8), (0, r)], close=True, dxfattribs=a)
    elif dtype == "ac":
        blk.add_lwpolyline([(-r, -r), (r, -r), (r, r), (-r, r)], close=True, dxfattribs=a)
        blk.add_circle((0, 0), r * 0.55, dxfattribs=a)
    elif dtype == "db":
        w, h = r * 1.6, r
        blk.add_lwpolyline([(-w, -h), (w, -h), (w, h), (-w, h)], close=True, dxfattribs=a)
        blk.add_line((-w, -h), (w, h), dxfattribs=a)
        blk.add_line((-w, h), (w, -h), dxfattribs=a)
    elif dtype == "emergency":
        w, h = r * 1.5, r * 0.8
        blk.add_lwpolyline([(-w, -h), (w, -h), (w, h), (-w, h)], close=True, dxfattribs=a)
        blk.add_line((-w * 0.6, 0), (w * 0.6, 0), dxfattribs=a)
        blk.add_line((w * 0.6, 0), (w * 0.2, h * 0.5), dxfattribs=a)
        blk.add_line((w * 0.6, 0), (w * 0.2, -h * 0.5), dxfattribs=a)
    else:
        raise BackendError(f"No symbol for device type {dtype!r}")
    off = r * 1.2
    blk.add_attdef("ID", (off, -off - th), "ID", dxfattribs={"height": th, "layer": "0"})
    blk.add_attdef("CIRCUIT", (off, -off - 2 * th), "", dxfattribs={"height": th, "flags": 1})
    blk.add_attdef("DB", (off, -off - 3 * th), "", dxfattribs={"height": th, "flags": 1})
    blk.add_attdef("TYPE", (off, -off - 4 * th), dtype, dxfattribs={"height": th, "flags": 1})


class DxfBackend(DrawingBackend):
    name = "dxf"

    def __init__(self) -> None:
        self.doc = None
        self.msp = None

    def begin(self, base: str | None) -> None:
        if base:
            self.doc = load_dxf(base)
        else:
            self.doc = ezdxf.new("R2018", setup=True)
            self.doc.units = 0
        self.msp = self.doc.modelspace()

    def units(self) -> int:
        return int(self.doc.header.get("$INSUNITS", 0))

    def bounds(self):
        try:
            box = bbox.extents(self.msp, fast=True)
        except Exception:  # noqa: BLE001 - odd entities must not abort generation
            return None
        if not box.has_data:
            return None
        return (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)

    def ensure_layer(self, name: str, color: int, lineweight: int) -> None:
        if name not in self.doc.layers:  # never restyle layers that exist in the base
            self.doc.layers.add(name, color=color, lineweight=lineweight)

    def define_symbols(self, size: float, text_height: float) -> None:
        for dtype in ("luminaire", "switch", "socket", "data", "ac", "db", "emergency"):
            name = BLOCK_PREFIX + dtype.upper()
            if name in self.doc.blocks:
                self.doc.blocks.delete_block(name, safe=False)
            build_symbol(self.doc.blocks.new(name), dtype, size / 2, text_height)

    def insert_device(self, dtype, layer, x, y, rotation, attribs):
        ref = self.msp.add_blockref(
            BLOCK_PREFIX + dtype.upper(), (x, y), dxfattribs={"layer": layer, "rotation": rotation}
        )
        ref.add_auto_attribs(attribs)
        for att in ref.attribs:
            att.dxf.layer = layer
            att.dxf.rotation = 0.0  # labels stay horizontal even when the symbol is rotated

    def add_polyline(self, layer, points):
        self.msp.add_lwpolyline(points, dxfattribs={"layer": layer})

    def add_rect(self, layer, b):
        x0, y0, x1, y1 = b
        self.msp.add_lwpolyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": layer}
        )

    def add_text(self, layer, text, x, y, height, bold=False):
        t = self.msp.add_text(text, height=height, dxfattribs={"layer": layer})
        t.set_placement((x, y))

    def save_dxf(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.saveas(str(path))

    def save_dwg(self, path: Path) -> None:
        if not odafc.is_installed():
            raise BackendError(
                "DWG export needs the ODA File Converter on this machine or the 'autocad' backend."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        odafc.export_dwg(self.doc, str(path), replace=True)


def copy_file(src: str, dst: Path) -> None:  # used by tests/tools for read-only reference copies
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
