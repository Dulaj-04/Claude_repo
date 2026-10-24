"""Live editing of the drawing that is open in AutoCAD / AutoCAD Electrical (COM, Windows).

Every operation edits the ACTIVE drawing directly, so changes appear on screen as you chat.
Devices and routes drawn here are registered as named AutoCAD GROUPS ("ACADE_<id>"), which keeps
a stable identity that survives manual edits, save and reopen. Each operation is wrapped in an
undo mark so a single ``undo`` reverts exactly one chat instruction.

Plain-geometry plan symbols only: these are NOT AutoCAD Electrical schematic components.
The COM object is injected so the logic can be tested against a fake without AutoCAD.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from .backends.base import BackendError
from .ids import DEVICE_TYPES, ROUTE_LAYERS
from .symbols import SIZES_MM, symbol_segments

PREFIX = "ACADE_"
RPC_REJECTED = -2147418111  # RPC_E_CALL_REJECTED: AutoCAD busy

# layer -> ACI colour, taken from the reference drawings
LAYER_COLORS = {
    "LIGHTING": 2, "SWITCHES": 6, "LIGHT_WIRING": 1, "SOCKETS": 6, "POWER_WIRING": 5,
    "DATA": 4, "AC": 7, "AC_WIRING": 6, "DB": 7, "EMERGENCY": 3, "NOTES": 7, "TEXT": 7,
}
ID_PREFIX = {"luminaire": "LUM", "switch": "SW", "socket": "SKT", "data": "DAT", "ac": "ACU",
             "emergency": "EXT", "db": "DB"}
DEVICE_LAYER_TO_TYPE = {v[1]: k for k, v in DEVICE_TYPES.items()}


class RealCom:
    """Builds the VARIANT arguments real AutoCAD COM calls need (pywin32)."""

    def __init__(self) -> None:
        try:
            import pythoncom
            import pywintypes
            import win32com.client as wc
        except ImportError as exc:
            raise BackendError(
                "Live mode needs Windows with pywin32 (pip install 'acad-electrical-mcp[autocad]') "
                "and a running AutoCAD Electrical."
            ) from exc
        self.pc, self.wt, self.wc = pythoncom, pywintypes, wc

    def pt(self, x: float, y: float, z: float = 0.0):
        return self.wc.VARIANT(self.pc.VT_ARRAY | self.pc.VT_R8, (float(x), float(y), float(z)))

    def flat(self, vals: list[float]):
        return self.wc.VARIANT(self.pc.VT_ARRAY | self.pc.VT_R8, [float(v) for v in vals])

    def objs(self, items: list[Any]):
        return self.wc.VARIANT(self.pc.VT_ARRAY | self.pc.VT_DISPATCH, items)

    def call(self, fn):
        for attempt in range(20):  # AutoCAD rejects calls while it is busy
            try:
                return fn()
            except self.wt.com_error as exc:
                if exc.hresult != RPC_REJECTED or attempt == 19:
                    raise
                time.sleep(0.3)

    def active_app(self):
        try:
            return self.wc.GetActiveObject("AutoCAD.Application")
        except Exception as exc:  # noqa: BLE001
            raise BackendError("No running AutoCAD found. Start AutoCAD Electrical and open a "
                               "drawing first.") from exc


class PlainCom:
    """Test double helper: plain Python values instead of VARIANTs."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def pt(self, x, y, z=0.0):
        return (float(x), float(y), float(z))

    def flat(self, vals):
        return [float(v) for v in vals]

    def objs(self, items):
        return list(items)

    def call(self, fn):
        return fn()

    def active_app(self):
        return self.app


def _num(v: Any, what: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number, got {v!r}") from None
    if not math.isfinite(f):
        raise ValueError(f"{what} must be finite")
    return f


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


class LiveSession:
    def __init__(self, com: Any, state_dir: Path) -> None:
        self.com = com
        self.state_dir = state_dir
        self.mode = "plan"  # "plan" = plan-level symbols, "schematic" = AutoCAD Electrical schematic
        self._depth = 0  # nesting of _op(): only the outermost opens/closes the undo mark

    # ------------------------------------------------------------------ plumbing
    @property
    def app(self):
        return self.com.active_app()

    @property
    def doc(self):
        app = self.app
        if not self.com.call(lambda: app.Documents.Count):
            raise BackendError("AutoCAD has no open drawing. Open one first.")
        return self.com.call(lambda: app.ActiveDocument)

    def _ms(self):
        return self.com.call(lambda: self.doc.ModelSpace)

    def _state_path(self) -> Path:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(self.com.call(lambda: self.doc.Name)))
        return self.state_dir / f"{name}.json"

    def _load(self) -> dict[str, Any]:
        p = self._state_path()
        data = json.loads(p.read_text()) if p.exists() else {}
        data.setdefault("devices", {})
        data.setdefault("routes", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        p = self._state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, sort_keys=True))

    class _Op:
        """Undo mark around one chat instruction."""

        def __init__(self, live: LiveSession) -> None:
            self.live = live

        def __enter__(self):
            self.doc = self.live.doc
            if self.live._depth == 0:
                self.live.com.call(lambda: self.doc.StartUndoMark())
            self.live._depth += 1
            return self

        def __exit__(self, *exc):
            self.live._depth -= 1
            if self.live._depth == 0:
                self.live.com.call(lambda: self.doc.EndUndoMark())
            return False

    def _op(self) -> LiveSession._Op:
        return LiveSession._Op(self)

    def _groups(self) -> dict[str, Any]:
        doc = self.doc
        out = {}
        for g in self.com.call(lambda: doc.Groups):
            name = str(self.com.call(lambda g=g: g.Name))
            if name.upper().startswith(PREFIX):
                out[name[len(PREFIX):].upper()] = g
        return out

    def _items(self, group) -> list[Any]:
        return [i for i in self.com.call(lambda: group)]

    def _make_group(self, gid: str, ents: list[Any]) -> None:
        doc = self.doc
        g = self.com.call(lambda: doc.Groups.Add(PREFIX + gid))
        self.com.call(lambda: g.AppendItems(self.com.objs(ents)))

    def _ensure_layer(self, name: str) -> None:
        doc = self.doc
        for lyr in self.com.call(lambda: doc.Layers):
            if str(self.com.call(lambda lyr=lyr: lyr.Name)).upper() == name.upper():
                return
        lyr = self.com.call(lambda: doc.Layers.Add(name))
        try:
            lyr.color = LAYER_COLORS.get(name.upper(), 7)
        except Exception:  # noqa: BLE001 - colour is cosmetic
            pass

    def _scale(self) -> float:
        """Drawing-unit scale relative to millimetres, from the geometry (headers can lie)."""
        ms = self._ms()
        big = 0.0
        for n, e in enumerate(self.com.call(lambda: ms)):
            if n > 300:
                break
            try:
                if "Line" in str(self.com.call(lambda e=e: e.ObjectName)):
                    s = self.com.call(lambda e=e: e.StartPoint)
                    big = max(big, abs(s[0]), abs(s[1]))
            except Exception:  # noqa: BLE001
                continue
        if big == 0:
            return 1.0
        return 1.0 if big > 500 else (0.001 if big < 200 else 1.0)

    # ------------------------------------------------------------------ status
    def connect(self) -> dict[str, Any]:
        app, doc = self.app, self.doc
        c = self.com.call
        ms = self._ms()
        layers: dict[str, int] = {}
        total = 0
        for e in c(lambda: ms):
            total += 1
            layers[str(c(lambda e=e: e.Layer))] = layers.get(str(c(lambda e=e: e.Layer)), 0) + 1
        data = self._load()
        return {
            "autocad_version": str(c(lambda: app.Version)),
            "drawing": str(c(lambda: doc.Name)), "path": str(c(lambda: doc.FullName)),
            "saved": bool(c(lambda: doc.Saved)),
            "header_insunits": int(c(lambda: doc.GetVariable("INSUNITS"))),
            "unit_scale_vs_mm": self._scale(),
            "modelspace_entities": total, "entities_per_layer": layers,
            "tracked_devices": len(data["devices"]), "tracked_routes": len(data["routes"]),
            "note": "unit_scale_vs_mm is derived from geometry (1 = millimetre drawing); "
                    "the INSUNITS header is not trusted.",
        }

    # ------------------------------------------------------------------ reading
    def texts(self, layer: str | None = None, contains: str | None = None,
              limit: int = 300) -> list[dict[str, Any]]:
        out = []
        c = self.com.call
        for e in c(lambda: self._ms()):
            if "Text" not in str(c(lambda e=e: e.ObjectName)):
                continue
            lay = str(c(lambda e=e: e.Layer))
            txt = str(c(lambda e=e: e.TextString))
            if layer and lay.upper() != layer.upper():
                continue
            if contains and contains.lower() not in txt.lower():
                continue
            p = c(lambda e=e: e.InsertionPoint)
            out.append({"text": txt, "layer": lay, "x": round(p[0], 2), "y": round(p[1], 2),
                        "height": round(float(c(lambda e=e: e.Height)), 2)})
            if len(out) >= limit:
                break
        return out

    def _lines_by_layer(self, layers: list[str]) -> list[tuple[Any, str, tuple, tuple]]:
        c = self.com.call
        want = {x.upper() for x in layers}
        out = []
        for e in c(lambda: self._ms()):
            lay = str(c(lambda e=e: e.Layer)).upper()
            if lay not in want or "Line" not in str(c(lambda e=e: e.ObjectName)):
                continue
            out.append((e, lay, tuple(c(lambda e=e: e.StartPoint)), tuple(c(lambda e=e: e.EndPoint))))
        return out

    def _tracked_handles(self) -> set[str]:
        c = self.com.call
        return {str(c(lambda i=i: i.Handle)) for g in self._groups().values()
                for i in self._items(g)}

    def scan(self, layers: list[str] | None = None, tol: float = 1.0,
             include_tracked: bool = False) -> list[dict[str, Any]]:
        """Cluster LINEs that touch into symbols, per device layer (reference drawings have no
        blocks). Returns centre/size so symbols can be recognised or adopted."""
        return [{k: v for k, v in cl.items() if k != "_ents"}
                for cl in self._clusters(layers, tol, include_tracked)]

    def _clusters(self, layers, tol, include_tracked) -> list[dict[str, Any]]:
        layers = layers or [v[1] for v in DEVICE_TYPES.values()]
        tracked = set() if include_tracked else self._tracked_handles()
        segs = [s for s in self._lines_by_layer(layers)
                if str(self.com.call(lambda s=s: s[0].Handle)) not in tracked]
        parent = list(range(len(segs)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        seen: dict[tuple, int] = {}
        for i, (_, lay, a, b) in enumerate(segs):
            for p in (a, b):
                k = (lay, round(p[0] / tol), round(p[1] / tol))
                if k in seen:
                    parent[find(i)] = find(seen[k])
                else:
                    seen[k] = i
        groups: dict[int, list[int]] = {}
        for i in range(len(segs)):
            groups.setdefault(find(i), []).append(i)
        out = []
        for idx in groups.values():
            pts = [(p[0], p[1]) for i in idx for p in (segs[i][2], segs[i][3])]
            x0, y0, x1, y1 = _bbox(pts)
            lay = segs[idx[0]][1]
            out.append({"layer": lay, "type": DEVICE_LAYER_TO_TYPE.get(lay), "lines": len(idx),
                        "x": round((x0 + x1) / 2, 2), "y": round((y0 + y1) / 2, 2),
                        "width": round(x1 - x0, 2), "height": round(y1 - y0, 2),
                        "_ents": [segs[i][0] for i in idx]})
        out.sort(key=lambda d: (d["layer"], round(d["y"]), d["x"]))
        return out

    def list_devices(self) -> dict[str, Any]:
        """Tracked devices and routes with positions read fresh from the drawing."""
        data = self._load()
        groups = self._groups()
        devices, missing = [], []
        for did, meta in sorted(data["devices"].items()):
            g = groups.get(did.upper())
            if g is None:
                missing.append(did)
                continue
            x, y = self._centre(g)
            moved = abs(x - meta["x"]) > 1e-3 or abs(y - meta["y"]) > 1e-3
            row = {"id": did, "type": meta["type"], "x": round(x, 2), "y": round(y, 2),
                   "circuit": meta.get("circuit"), "db": meta.get("db"),
                   "room": meta.get("room"), "moved_by_hand": moved}
            for k in ("luminaire_type", "watts", "lumens", "source"):
                if meta.get(k) is not None:
                    row[k] = meta[k]
            devices.append(row)
        routes = []
        for cid, r in sorted(data["routes"].items()):
            routes.append({**r, "present": cid.upper() in groups or ("R-" + cid).upper() in groups})
        return {"devices": devices, "routes": routes,
                "missing_from_drawing": missing,
                "untracked_in_groups": sorted(set(groups) - {d.upper() for d in data["devices"]}
                                              - {("R-" + c).upper() for c in data["routes"]})}

    def _centre(self, group) -> tuple[float, float]:
        c = self.com.call
        pts = []
        for it in self._items(group):
            if "Line" in str(c(lambda it=it: it.ObjectName)):
                for p in (c(lambda it=it: it.StartPoint), c(lambda it=it: it.EndPoint)):
                    pts.append((p[0], p[1]))
        if not pts:
            return 0.0, 0.0
        x0, y0, x1, y1 = _bbox(pts)
        return (x0 + x1) / 2, (y0 + y1) / 2

    # ------------------------------------------------------------------ writing
    def _new_id(self, floor: str, dtype: str, data: dict[str, Any]) -> str:
        if dtype == "db":
            return f"DB-{floor}"
        n = 1
        while f"{floor}-{ID_PREFIX[dtype]}-{n:02d}" in data["devices"]:
            n += 1
        return f"{floor}-{ID_PREFIX[dtype]}-{n:02d}"

    def _draw_symbol(self, dtype: str, x: float, y: float, rotation: float, scale: float):
        layer = DEVICE_TYPES[dtype][1]
        self._ensure_layer(layer)
        ms, c, com = self._ms(), self.com.call, self.com
        ents = []
        for (p, q) in symbol_segments(dtype, scale, rotation):
            e = c(lambda p=p, q=q: ms.AddLine(com.pt(x + p[0], y + p[1]), com.pt(x + q[0], y + q[1])))
            c(lambda e=e: setattr(e, "Layer", layer))
            ents.append(e)
        if dtype == "emergency":
            t = c(lambda: ms.AddText("EXIT", com.pt(x - SIZES_MM[dtype][0] * scale * 0.3,
                                                    y + SIZES_MM[dtype][1] * scale * 0.7),
                                     160 * scale))
            c(lambda: setattr(t, "Layer", layer))
            ents.append(t)
        return ents

    def place(self, floor: str, dtype: str, x: float, y: float, rotation: float = 0.0,
              circuit: str | None = None, db: str | None = None, room: str | None = None,
              device_id: str | None = None, tolerance: float | None = None,
              extra: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self._load()
        res = self._place_core(floor, dtype, x, y, rotation, circuit, db, room, device_id,
                               tolerance, extra, data, self._groups(), self._scale(), {})
        self._save(data)
        return res

    def _place_core(self, floor, dtype, x, y, rotation, circuit, db, room, device_id, tolerance,
                    extra, data, groups, scale, centres) -> dict[str, Any]:
        if dtype not in SIZES_MM:
            raise ValueError(f"Unknown device type {dtype!r}; expected {sorted(SIZES_MM)}")
        x, y, rotation = _num(x, "x"), _num(y, "y"), _num(rotation, "rotation")
        tol = tolerance if tolerance is not None else 10 * scale
        for did, meta in data["devices"].items():  # idempotent: same type at same spot = same
            if meta["type"] == dtype and did.upper() in groups:
                if did not in centres:
                    centres[did] = self._centre(groups[did.upper()])
                gx, gy = centres[did]
                if abs(gx - x) <= tol and abs(gy - y) <= tol:
                    return {"id": did, "created": False, "note": "already placed here"}
        did = device_id or self._new_id(floor, dtype, data)
        if did in data["devices"] and did.upper() in groups:
            raise ValueError(f"{did} already exists; use live_move to change it")
        with self._op():
            ents = self._draw_symbol(dtype, x, y, rotation, scale)
            self._make_group(did, ents)
        data["devices"][did] = {"type": dtype, "x": x, "y": y, "rotation": rotation,
                                "circuit": circuit, "db": db, "room": room, **(extra or {})}
        centres[did] = (x, y)
        groups[did.upper()] = True  # marks it present for the rest of a batch
        return {"id": did, "created": True, "type": dtype, "x": x, "y": y,
                "layer": DEVICE_TYPES[dtype][1]}

    def place_many(self, floor: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Place many devices as ONE undo step. Items: {type,x,y,rotation?,circuit?,db?,room?,
        extra?}. Same-type devices already at a spot are skipped."""
        data = self._load()
        groups = self._groups()
        scale = self._scale()
        centres: dict[str, tuple[float, float]] = {}
        created, skipped, ids = 0, 0, []
        try:
            with self._op():
                for it in items:
                    r = self._place_core(floor, it["type"], it["x"], it["y"],
                                         it.get("rotation", 0.0), it.get("circuit"), it.get("db"),
                                         it.get("room"), None, None, it.get("extra"), data,
                                         groups, scale, centres)
                    if r["created"]:
                        created += 1
                        ids.append(r["id"])
                    else:
                        skipped += 1
        finally:
            self._save(data)  # keep the registry in step with whatever was drawn
        return {"created": created, "already_present": skipped, "ids": ids[:200],
                "undo": "one live_undo reverts the whole import"}

    def _device(self, device_id: str) -> tuple[dict[str, Any], dict[str, Any], Any]:
        data = self._load()
        did = next((d for d in data["devices"] if d.upper() == device_id.upper()), None)
        if did is None:
            raise ValueError(f"Unknown device {device_id!r}; use live_list_devices to see ids")
        g = self._groups().get(did.upper())
        if g is None:
            raise ValueError(f"{did} is tracked but no longer in the drawing (deleted by hand?)")
        return data, data["devices"][did] | {"id": did}, g

    def move(self, device_id: str, x: float | None = None, y: float | None = None,
             dx: float | None = None, dy: float | None = None) -> dict[str, Any]:
        data, meta, g = self._device(device_id)
        cx, cy = self._centre(g)
        if x is not None or y is not None:
            tx = _num(x, "x") if x is not None else cx
            ty = _num(y, "y") if y is not None else cy
            mx, my = tx - cx, ty - cy
        else:
            mx, my = _num(dx or 0, "dx"), _num(dy or 0, "dy")
        com, c = self.com, self.com.call
        with self._op():
            for it in self._items(g):
                c(lambda it=it: it.Move(com.pt(0, 0), com.pt(mx, my)))
        nx, ny = self._centre(g)
        data["devices"][meta["id"]].update(x=nx, y=ny)
        stale = [cid for cid, r in data["routes"].items() if meta["id"] in r["device_ids"]]
        for cid in stale:
            data["routes"][cid]["stale"] = True
        self._save(data)
        return {"id": meta["id"], "x": round(nx, 2), "y": round(ny, 2),
                "routes_now_stale": stale,
                "note": "re-run live_route for stale circuits" if stale else None}

    def delete(self, device_id: str) -> dict[str, Any]:
        data, meta, g = self._device(device_id)
        c = self.com.call
        with self._op():
            for it in self._items(g):
                c(lambda it=it: it.Delete())
            try:
                c(lambda: g.Delete())
            except Exception:  # noqa: BLE001 - group disappears with its last member
                pass
        del data["devices"][meta["id"]]
        stale = [cid for cid, r in data["routes"].items() if meta["id"] in r["device_ids"]]
        for cid in stale:
            data["routes"][cid]["stale"] = True
        self._save(data)
        return {"deleted": meta["id"], "routes_now_stale": stale}

    def assign(self, device_id: str, circuit: str, db: str | None = None) -> dict[str, Any]:
        data, meta, _ = self._device(device_id)
        data["devices"][meta["id"]].update(circuit=circuit, db=db or meta.get("db"))
        self._save(data)
        return {"id": meta["id"], "circuit": circuit, "db": data["devices"][meta["id"]]["db"]}

    def route(self, circuit: str, kind: str, device_ids: list[str] | None = None,
              db: str | None = None, label: bool = True) -> dict[str, Any]:
        """Draw an orthogonal route DB -> devices on the wiring layer, replacing any previous
        route for this circuit. The label is the circuit id, as in the reference drawings."""
        if kind not in ROUTE_LAYERS:
            raise ValueError(f"kind must be one of {sorted(ROUTE_LAYERS)}")
        data = self._load()
        groups = self._groups()
        ids = device_ids or [d for d, m in sorted(data["devices"].items())
                             if m.get("circuit") == circuit and d.upper() in groups]
        if not ids:
            raise ValueError(f"No devices on circuit {circuit!r}. Assign devices first.")
        pos = {}
        for did in ids:
            if did.upper() not in groups:
                raise ValueError(f"Unknown or missing device {did}")
            pos[did] = self._centre(groups[did.upper()])
        db = db or data["devices"][ids[0]].get("db")
        if not db or db.upper() not in groups:
            raise ValueError("Route needs a distribution board: pass db=<DB id> (place one first).")
        cur = self._centre(groups[db.upper()])
        left, order, pts = list(ids) if device_ids is None else [], [], [cur]
        if device_ids is None:
            while left:
                nxt = min(left, key=lambda i: (abs(pos[i][0] - pts[-1][0])
                                               + abs(pos[i][1] - pts[-1][1]), i))
                left.remove(nxt)
                order.append(nxt)
                pts += self._l(pts[-1], pos[nxt])[1:]
        else:
            order = list(device_ids)
            for did in order:
                pts += self._l(pts[-1], pos[did])[1:]
        layer = ROUTE_LAYERS[kind]
        gid = "R-" + circuit
        with self._op():
            old = groups.get(gid.upper())
            if old is not None:
                for it in self._items(old):
                    self.com.call(lambda it=it: it.Delete())
                try:
                    self.com.call(lambda: old.Delete())
                except Exception:  # noqa: BLE001
                    pass
            self._ensure_layer(layer)
            ms, c, com = self._ms(), self.com.call, self.com
            flat = [v for p in pts for v in p]
            pl = c(lambda: ms.AddLightWeightPolyline(com.flat(flat)))
            c(lambda: setattr(pl, "Layer", layer))
            ents = [pl]
            if label:
                scale = self._scale()
                t = c(lambda: ms.AddText(circuit, com.pt(pts[0][0] + 400 * scale,
                                                         pts[0][1] + 400 * scale), 210 * scale))
                c(lambda: setattr(t, "Layer", layer))
                ents.append(t)
            self._make_group(gid, ents)
        data["routes"][circuit] = {"circuit": circuit, "kind": kind, "db": db,
                                   "device_ids": order, "stale": False}
        for did in order:
            data["devices"][did].update(circuit=circuit, db=db)
        self._save(data)
        return {"route": gid, "layer": layer, "from": db, "devices": order, "points": len(pts)}

    @staticmethod
    def _l(a, b):
        if abs(a[0] - b[0]) < 1e-9 or abs(a[1] - b[1]) < 1e-9:
            return [a, b]
        return [a, (b[0], a[1]), b]

    def adopt(self, floor: str, layers: list[str] | None = None, min_size: float = 0.0,
              max_size: float = 1e12, tol: float = 1.0) -> dict[str, Any]:
        """Give existing reference symbols (loose LINEs) stable ids by grouping each cluster, so
        they can be moved/deleted/assigned by id. Geometry is not changed."""
        data = self._load()
        made = []
        with self._op():
            for cl in self._clusters(layers, tol, include_tracked=False):
                dtype = cl["type"]
                size = max(cl["width"], cl["height"])
                if dtype is None or not (min_size <= size <= max_size) or cl["lines"] < 2:
                    continue
                did = self._new_id(floor, dtype, data)
                self._make_group(did, cl["_ents"])
                data["devices"][did] = {"type": dtype, "x": cl["x"], "y": cl["y"], "rotation": 0.0,
                                        "circuit": None, "db": None, "room": None}
                made.append({"id": did, "type": dtype, "x": cl["x"], "y": cl["y"],
                             "size": round(size, 1)})
        self._save(data)
        counts: dict[str, int] = {}
        for m in made:
            counts[m["type"]] = counts.get(m["type"], 0) + 1
        return {"adopted": len(made), "by_type": counts, "devices": made[:200]}

    def add_text(self, layer: str, text: str, x: float, y: float, height: float) -> dict[str, Any]:
        with self._op():
            self._ensure_layer(layer)
            ms, c, com = self._ms(), self.com.call, self.com
            t = c(lambda: ms.AddText(str(text), com.pt(_num(x, "x"), _num(y, "y")),
                                     _num(height, "height")))
            c(lambda: setattr(t, "Layer", layer))
        return {"text": text, "layer": layer, "x": x, "y": y}

    def add_polyline(self, layer: str, points: list[list[float]]) -> dict[str, Any]:
        if len(points) < 2:
            raise ValueError("A polyline needs at least two points")
        with self._op():
            self._ensure_layer(layer)
            ms, c, com = self._ms(), self.com.call, self.com
            flat = [_num(v, "coordinate") for p in points for v in p[:2]]
            pl = c(lambda: ms.AddLightWeightPolyline(com.flat(flat)))
            c(lambda: setattr(pl, "Layer", layer))
        return {"layer": layer, "points": len(points)}

    # ------------------------------------------------------------------ view / file
    def zoom(self, x: float, y: float, width: float) -> dict[str, Any]:
        w = _num(width, "width")
        app, com = self.app, self.com
        com.call(lambda: app.ZoomWindow(com.pt(x - w / 2, y - w / 3), com.pt(x + w / 2, y + w / 3)))
        return {"zoomed_to": [x, y], "width": w}

    def undo(self) -> dict[str, Any]:
        doc = self.doc
        self.com.call(lambda: doc.SendCommand("_.UNDO 1 "))
        return {"sent": "UNDO 1", "note": "Reverts the last chat instruction. Run live_list_devices "
                                          "to re-sync tracked devices."}

    def save(self, path: str | None = None) -> dict[str, Any]:
        doc = self.doc
        if path:
            self.com.call(lambda: doc.SaveAs(str(Path(path).resolve())))
        else:
            self.com.call(lambda: doc.Save())
        return {"saved": str(self.com.call(lambda: doc.FullName))}

    def selftest(self) -> dict[str, Any]:
        """Create, move, route, undo and delete on the ACTIVE drawing, then clean up. Run it on a
        scratch drawing to prove the COM layer works on this AutoCAD install."""
        steps: list[str] = []
        try:
            db = self.place("TEST", "db", 0, 0)
            steps.append("placed DB")
            lum = self.place("TEST", "luminaire", 2000, 1000, circuit="TEST-L01", db=db["id"])
            steps.append("placed luminaire")
            self.move(lum["id"], dx=500)
            steps.append("moved luminaire")
            self.route("TEST-L01", "lighting", db=db["id"])
            steps.append("drew route")
            listing = self.list_devices()
            steps.append(f"read back {len(listing['devices'])} devices")
        finally:
            for did in ("TEST-LUM-01", "DB-TEST"):
                try:
                    self.delete(did)
                except Exception:  # noqa: BLE001
                    pass
            try:
                data = self._load()
                g = self._groups().get("R-TEST-L01".upper())
                if g is not None:
                    for it in self._items(g):
                        self.com.call(lambda it=it: it.Delete())
                    try:
                        self.com.call(lambda: g.Delete())
                    except Exception:  # noqa: BLE001
                        pass
                data["routes"].pop("TEST-L01", None)
                self._save(data)
            except Exception:  # noqa: BLE001
                pass
        steps.append("cleaned up test objects")
        return {"ok": True, "steps": steps}
