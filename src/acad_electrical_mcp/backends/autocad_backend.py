"""Live backend: drives a running AutoCAD Electrical session through COM (Windows only).

NOT certified without a licensed desktop host. Layer/geometry/block/save calls use the
standard AutoCAD ActiveX object model; verify on your installation before relying on it.
"""

from __future__ import annotations

import math
from pathlib import Path

from .base import BackendError, DrawingBackend
from .dxf_backend import BLOCK_PREFIX, build_symbol  # noqa: F401  (geometry reference)

# AcSaveAsType values for 2018-format files.
AC_R2018_DWG = 64
AC_R2018_DXF = 65


def _com():
    try:
        import pythoncom
        import win32com.client as wc
    except ImportError as exc:
        raise BackendError(
            "The 'autocad' backend needs Windows with pywin32 installed "
            "(pip install 'acad-electrical-mcp[autocad]') and a running AutoCAD Electrical."
        ) from exc
    return pythoncom, wc


def _app(create: bool = False):
    _, wc = _com()
    try:
        return wc.GetActiveObject("AutoCAD.Application")
    except Exception:  # noqa: BLE001
        if not create:
            raise BackendError(
                "No running AutoCAD instance found. Start AutoCAD Electrical first."
            ) from None
        return wc.Dispatch("AutoCAD.Application")


def _pt(*xyz: float):
    pythoncom, wc = _com()
    vals = list(xyz) + [0.0] * (3 - len(xyz))
    return wc.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, vals)


def _flat(points: list[tuple[float, float]]):
    pythoncom, wc = _com()
    return wc.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [c for p in points for c in p])


def status() -> dict:
    app = _app()
    doc = app.ActiveDocument if app.Documents.Count else None
    return {
        "autocad_version": app.Version,
        "caption": app.Caption,
        "open_documents": [app.Documents.Item(i).Name for i in range(app.Documents.Count)],
        "active_document": doc.FullName if doc else None,
    }


def open_document(path: str) -> dict:
    app = _app()
    doc = app.Documents.Open(str(Path(path).resolve()), True)  # read-only
    return {"opened": doc.FullName, "read_only": True}


def run_command(command: str) -> dict:
    app = _app()
    if not app.Documents.Count:
        raise BackendError("No open document to run the command in.")
    app.ActiveDocument.SendCommand(command if command.endswith("\n") else command + "\n")
    return {"sent": command}


class AutoCadBackend(DrawingBackend):
    name = "autocad"

    def __init__(self) -> None:
        self.app = None
        self.doc = None
        self.ms = None

    def begin(self, base: str | None) -> None:
        self.app = _app(create=True)
        if base:
            self.doc = self.app.Documents.Open(str(Path(base).resolve()), True)
        else:
            self.doc = self.app.Documents.Add()
        self.ms = self.doc.ModelSpace

    def units(self) -> int:
        return int(self.doc.GetVariable("INSUNITS"))

    def bounds(self):
        try:
            self.app.ZoomExtents()
            lo = self.doc.GetVariable("EXTMIN")
            hi = self.doc.GetVariable("EXTMAX")
        except Exception:  # noqa: BLE001
            return None
        if hi[0] < lo[0] or hi[1] < lo[1] or abs(hi[0]) > 1e19:
            return None
        return (lo[0], lo[1], hi[0], hi[1])

    def ensure_layer(self, name: str, color: int, lineweight: int) -> None:
        layers = self.doc.Layers
        try:
            layers.Item(name)
            return
        except Exception:  # noqa: BLE001
            pass
        lyr = layers.Add(name)
        lyr.color = color
        try:
            lyr.Lineweight = lineweight
        except Exception:  # noqa: BLE001 - non-standard lineweight
            pass

    def define_symbols(self, size: float, text_height: float) -> None:
        r, th = size / 2, text_height
        k = r * math.sqrt(0.5)
        blocks = self.doc.Blocks
        for dtype in ("luminaire", "switch", "socket", "data", "ac", "db", "emergency"):
            name = BLOCK_PREFIX + dtype.upper()
            try:
                blocks.Item(name)
                continue  # keep existing definition
            except Exception:  # noqa: BLE001
                pass
            blk = blocks.Add(_pt(0, 0), name)
            if dtype == "luminaire":
                blk.AddCircle(_pt(0, 0), r)
                blk.AddLine(_pt(-k, -k), _pt(k, k))
                blk.AddLine(_pt(-k, k), _pt(k, -k))
            elif dtype == "switch":
                blk.AddCircle(_pt(0, 0), r * 0.5)
                blk.AddLine(_pt(r * 0.35, r * 0.35), _pt(r * 1.3, r * 1.3))
            elif dtype == "socket":
                blk.AddCircle(_pt(0, 0), r)
                blk.AddLine(_pt(-r, 0), _pt(r, 0))
                blk.AddLine(_pt(0, 0), _pt(0, -r))
            elif dtype == "data":
                p = blk.AddLightWeightPolyline(_flat([(-r, -r * .8), (r, -r * .8), (0, r)]))
                p.Closed = True
            elif dtype == "ac":
                p = blk.AddLightWeightPolyline(_flat([(-r, -r), (r, -r), (r, r), (-r, r)]))
                p.Closed = True
                blk.AddCircle(_pt(0, 0), r * 0.55)
            elif dtype == "db":
                w, h = r * 1.6, r
                p = blk.AddLightWeightPolyline(_flat([(-w, -h), (w, -h), (w, h), (-w, h)]))
                p.Closed = True
                blk.AddLine(_pt(-w, -h), _pt(w, h))
                blk.AddLine(_pt(-w, h), _pt(w, -h))
            else:  # emergency
                w, h = r * 1.5, r * .8
                p = blk.AddLightWeightPolyline(_flat([(-w, -h), (w, -h), (w, h), (-w, h)]))
                p.Closed = True
                blk.AddLine(_pt(-w * .6, 0), _pt(w * .6, 0))
            off = r * 1.2
            for i, (tag, mode) in enumerate((("ID", 0), ("CIRCUIT", 1), ("DB", 1), ("TYPE", 1))):
                blk.AddAttribute(th, mode, tag, _pt(off, -off - (i + 1) * th), tag, tag)

    def insert_device(self, dtype, layer, x, y, rotation, attribs):
        ref = self.ms.InsertBlock(
            _pt(x, y), BLOCK_PREFIX + dtype.upper(), 1, 1, 1, math.radians(rotation)
        )
        ref.Layer = layer
        for att in ref.GetAttributes():
            if att.TagString in attribs:
                att.TextString = str(attribs[att.TagString])
            att.Layer = layer
            try:
                att.Rotation = 0.0  # labels stay horizontal even when the symbol is rotated
            except Exception:  # noqa: BLE001 - cosmetic
                pass

    def add_polyline(self, layer, points):
        p = self.ms.AddLightWeightPolyline(_flat(points))
        p.Layer = layer

    def add_rect(self, layer, b):
        x0, y0, x1, y1 = b
        p = self.ms.AddLightWeightPolyline(_flat([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))
        p.Closed = True
        p.Layer = layer

    def add_text(self, layer, text, x, y, height, bold=False):
        t = self.ms.AddText(text, _pt(x, y), height)
        t.Layer = layer

    def save_dxf(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.SaveAs(str(path), AC_R2018_DXF)

    def save_dwg(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.SaveAs(str(path), AC_R2018_DWG)

    def close(self) -> None:
        try:
            self.doc.Close(False)
        except Exception:  # noqa: BLE001
            pass
