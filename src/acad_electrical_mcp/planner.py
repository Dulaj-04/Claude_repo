"""Changeset planner: proposals -> preview -> guarded apply. Pure functions over the model."""

from __future__ import annotations

import copy
import math
from typing import Any

from .ids import DEVICE_TYPES, ROUTE_LAYERS, next_device_id, route_id
from .model import sha256_obj

KIND_OF = {t: v[2] for t, v in DEVICE_TYPES.items() if v[2]}
KIND_OF["data"] = "power"
# optional per-device data carried through from imports (e.g. DIALux); never invented
EXTRA_FIELDS = ("watts", "lumens", "luminaire_type", "source", "dialux_label")


class Conflict(RuntimeError):
    pass


def _changeset(model: dict[str, Any], ops: list[dict], notes: list[str], label: str) -> dict:
    return {
        "id": "cs-" + sha256_obj([model["revision"], ops])[:10],
        "project": model["project"],
        "floor": model["floor"],
        "base_revision": model["revision"],
        "label": label,
        "ops": ops,
        "assumptions": notes,
        "unchanged": 0,
    }


def _num(v: Any, what: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number, got {v!r}") from None
    if not math.isfinite(f):
        raise ValueError(f"{what} must be finite")
    return f


def plan_devices(
    model: dict[str, Any], devices: list[dict[str, Any]], remove_ids: list[str] | None = None,
    tolerance: float = 0.01,
) -> dict[str, Any]:
    ops: list[dict] = []
    notes: list[str] = []
    unchanged = 0
    existing = {d["id"]: d for d in model["devices"]}
    taken = set(existing)
    circuits = {c["id"]: c for c in model["circuits"]}
    new_circuits: dict[str, dict] = {}
    db_ids = [d["id"] for d in model["devices"] if d["type"] == "db"]
    pending_positions: list[tuple[str, float, float]] = []

    for spec in devices:
        dtype = spec.get("type")
        if dtype not in DEVICE_TYPES:
            raise ValueError(f"Unknown device type {dtype!r}; expected {sorted(DEVICE_TYPES)}")
        x, y = _num(spec.get("x"), "x"), _num(spec.get("y"), "y")
        rot = _num(spec.get("rotation", 0.0), "rotation")
        did = spec.get("id")
        if dtype == "db":
            did = f"DB-{model['floor']}"
        cand = {
            "type": dtype, "x": x, "y": y, "rotation": rot,
            "room": spec.get("room"), "circuit": spec.get("circuit"),
            "db": spec.get("db"), "note": spec.get("note"),
        }
        extras = {k: spec[k] for k in EXTRA_FIELDS if spec.get(k) is not None}
        cand.update(extras)
        if did and did in existing:
            cur = existing[did]
            if cur["type"] != dtype:
                raise ValueError(f"{did} is a {cur['type']}; its type cannot be changed")
            changes = {k: v for k, v in cand.items() if k != "type" and (
                (k in ("x", "y", "rotation") and abs(cur.get(k, 0) - v) > 1e-9)
                or (k not in ("x", "y", "rotation") and v is not None and cur.get(k) != v))}
            if changes:
                ops.append({"op": "update_device", "id": did, "changes": changes})
            else:
                unchanged += 1
            continue
        if not did:  # idempotence: same type at (almost) the same place is the same device
            dup = next((d for d in list(model["devices"]) if d["type"] == dtype
                        and abs(d["x"] - x) <= tolerance and abs(d["y"] - y) <= tolerance), None)
            dup_p = next((p for p in pending_positions if p[0] == dtype
                          and abs(p[1] - x) <= tolerance and abs(p[2] - y) <= tolerance), None)
            if dup:
                changes = {k: v for k, v in extras.items() if dup.get(k) != v}
                if changes:
                    ops.append({"op": "update_device", "id": dup["id"], "changes": changes})
                else:
                    unchanged += 1
                continue
            if dup_p:
                unchanged += 1
                continue
            did = next_device_id(model["floor"], dtype, taken)
        taken.add(did)
        pending_positions.append((dtype, x, y))
        if cand["circuit"] and cand["circuit"] not in circuits and cand["circuit"] not in new_circuits:
            db = cand["db"] or (db_ids[0] if db_ids else None)
            new_circuits[cand["circuit"]] = {"id": cand["circuit"], "kind": KIND_OF.get(dtype), "db": db}
            notes.append(f"Circuit {cand['circuit']} did not exist; creating a {KIND_OF.get(dtype)} "
                         f"circuit on {db or 'no DB yet'}.")
        if dtype != "db" and cand["circuit"] and not cand["db"]:
            cand["db"] = (circuits.get(cand["circuit"]) or new_circuits[cand["circuit"]]).get("db")
        ops.append({"op": "add_device", "device": {"id": did, **cand}})
    for c in new_circuits.values():
        ops.insert(0, {"op": "add_circuit", "circuit": c})
    for rid in remove_ids or []:
        if rid not in existing:
            raise ValueError(f"Cannot remove unknown device {rid}")
        ops.append({"op": "remove_device", "id": rid})
    cs = _changeset(model, ops, notes, "plan_devices")
    cs["unchanged"] = unchanged
    return cs


def propose_lighting_grid(
    model: dict[str, Any], room_id: str, spacing_x: float, spacing_y: float,
    circuit: str | None = None, margin: float = 0.0,
) -> dict[str, Any]:
    room = next((r for r in model["rooms"] if r["id"] == room_id), None)
    if room is None:
        raise ValueError(f"Unknown room {room_id!r}")
    if spacing_x <= 0 or spacing_y <= 0:
        raise ValueError("spacing_x and spacing_y must be positive")
    x0, y0, x1, y1 = room["bounds"]
    x0, y0, x1, y1 = x0 + margin, y0 + margin, x1 - margin, y1 - margin
    nx, ny = max(1, int((x1 - x0) // spacing_x)), max(1, int((y1 - y0) // spacing_y))
    sx, sy = (x1 - x0) / nx, (y1 - y0) / ny
    specs = [
        {"type": "luminaire", "x": round(x0 + sx * (i + .5), 6), "y": round(y0 + sy * (j + .5), 6),
         "room": room_id, "circuit": circuit}
        for j in range(ny) for i in range(nx)
    ]
    cs = plan_devices(model, specs)
    cs["label"] = "propose_lighting_grid"
    cs["assumptions"].append(
        f"DRAFT grid of {nx}x{ny} luminaires from spacing {spacing_x}x{spacing_y}; this is a "
        "placement proposal only, not a lighting calculation."
    )
    return cs


def set_circuit(
    model: dict[str, Any], circuit_id: str, kind: str, db: str | None,
    device_ids: list[str] | None,
) -> dict[str, Any]:
    if kind not in ROUTE_LAYERS:
        raise ValueError(f"kind must be one of {sorted(ROUTE_LAYERS)}")
    devs = {d["id"]: d for d in model["devices"]}
    if db and (db not in devs or devs[db]["type"] != "db"):
        raise ValueError(f"DB {db!r} is not a distribution board on this floor")
    ops: list[dict] = []
    cur = next((c for c in model["circuits"] if c["id"] == circuit_id), None)
    new = {"id": circuit_id, "kind": kind, "db": db}
    if cur is None:
        ops.append({"op": "add_circuit", "circuit": new})
    elif cur != new:
        ops.append({"op": "update_circuit", "circuit": new})
    for did in device_ids or []:
        if did not in devs:
            raise ValueError(f"Unknown device {did}")
        if KIND_OF.get(devs[did]["type"]) != kind:
            raise ValueError(f"{did} ({devs[did]['type']}) cannot be on a {kind} circuit")
        ch = {k: v for k, v in (("circuit", circuit_id), ("db", db)) if devs[did].get(k) != v}
        if ch:
            ops.append({"op": "update_device", "id": did, "changes": ch})
    return _changeset(model, ops, [], "set_circuit_assignment")


def _l_path(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]]:
    if abs(a[0] - b[0]) < 1e-9 or abs(a[1] - b[1]) < 1e-9:
        return [a, b]
    return [a, (b[0], a[1]), b]


def plan_route(
    model: dict[str, Any], circuit_id: str, device_ids: list[str] | None = None
) -> dict[str, Any]:
    circuits = {c["id"]: c for c in model["circuits"]}
    c = circuits.get(circuit_id)
    if c is None:
        raise ValueError(f"Unknown circuit {circuit_id!r}; create it with set_circuit_assignment")
    devs = {d["id"]: d for d in model["devices"]}
    if not c.get("db") or c["db"] not in devs:
        raise ValueError(f"Circuit {circuit_id} has no valid DB; assign one first")
    ids = device_ids or [d["id"] for d in model["devices"] if d.get("circuit") == circuit_id]
    if not ids:
        raise ValueError(f"No devices on circuit {circuit_id}")
    for did in ids:
        if did not in devs:
            raise ValueError(f"Unknown device {did}")
        if devs[did].get("circuit") != circuit_id:
            raise ValueError(f"{did} is not on circuit {circuit_id}")
    cur = (devs[c["db"]]["x"], devs[c["db"]]["y"])
    order: list[str] = []
    if device_ids:
        order = list(device_ids)  # caller-defined order
    else:  # nearest-neighbour chain from the DB, deterministic tie-break on id
        left = sorted(ids)
        pos = cur
        while left:
            nxt = min(left, key=lambda i: (abs(devs[i]["x"] - pos[0]) + abs(devs[i]["y"] - pos[1]), i))
            order.append(nxt)
            left.remove(nxt)
            pos = (devs[nxt]["x"], devs[nxt]["y"])
    pts: list[tuple[float, float]] = [cur]
    for did in order:
        seg = _l_path(pts[-1], (devs[did]["x"], devs[did]["y"]))
        pts += seg[1:]
    route = {"id": route_id(circuit_id), "kind": c["kind"], "circuit": circuit_id, "db": c["db"],
             "points": [list(p) for p in pts], "device_ids": order}
    old = next((r for r in model["routes"] if r["id"] == route["id"]), None)
    ops: list[dict] = []
    if old is None:
        ops.append({"op": "add_route", "route": route})
    elif old != route:
        ops.append({"op": "update_route", "route": route})
    cs = _changeset(model, ops, [], "plan_routes")
    cs["unchanged"] = 0 if ops else 1
    cs["assumptions"].append(
        "Route geometry is orthogonal and is a DRAFT drawing aid; it is not a cable-routing or "
        "cable-sizing result."
    )
    return cs


def apply_ops(model: dict[str, Any], cs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Return a new model with the changeset applied (revision bumped)."""
    if cs["base_revision"] != model["revision"]:
        raise Conflict(
            f"Stale changeset: planned against revision {cs['base_revision']} but the model is at "
            f"revision {model['revision']}. Re-plan the change."
        )
    m = copy.deepcopy(model)
    stats = {"added": 0, "updated": 0, "removed": 0}
    for op in cs["ops"]:
        kind = op["op"]
        if kind == "add_device":
            if any(d["id"] == op["device"]["id"] for d in m["devices"]):
                raise Conflict(f"Device {op['device']['id']} already exists")
            m["devices"].append(op["device"])
            stats["added"] += 1
        elif kind == "update_device":
            d = next((d for d in m["devices"] if d["id"] == op["id"]), None)
            if d is None:
                raise Conflict(f"Device {op['id']} no longer exists")
            d.update(op["changes"])
            stats["updated"] += 1
        elif kind == "remove_device":
            m["devices"] = [d for d in m["devices"] if d["id"] != op["id"]]
            m["routes"] = [r for r in m["routes"] if op["id"] not in r["device_ids"]]
            stats["removed"] += 1
        elif kind in ("add_circuit", "update_circuit"):
            m["circuits"] = [c for c in m["circuits"] if c["id"] != op["circuit"]["id"]]
            m["circuits"].append(op["circuit"])
            stats["added" if kind == "add_circuit" else "updated"] += 1
        elif kind in ("add_route", "update_route"):
            m["routes"] = [r for r in m["routes"] if r["id"] != op["route"]["id"]]
            m["routes"].append(op["route"])
            stats["added" if kind == "add_route" else "updated"] += 1
        else:
            raise ValueError(f"Unknown op {kind!r}")
    # stale routes: moving a device invalidates the stored path
    moved = {op["id"] for op in cs["ops"] if op["op"] == "update_device"
             and {"x", "y"} & set(op["changes"])}
    if moved:
        m["routes"] = [r for r in m["routes"] if not moved & set(r["device_ids"])
                       or any(op["op"] in ("add_route", "update_route")
                              and op["route"]["id"] == r["id"] for op in cs["ops"])]
    m["devices"].sort(key=lambda d: d["id"])
    m["circuits"].sort(key=lambda c: c["id"])
    m["routes"].sort(key=lambda r: r["id"])
    m["revision"] += 1
    return m, stats


def describe(cs: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    conflicts = []
    if cs["base_revision"] != model["revision"]:
        conflicts.append(f"Changeset is stale (base revision {cs['base_revision']}, model "
                         f"{model['revision']}).")
    rooms = {r["id"] for r in model["rooms"]}
    for op in cs["ops"]:
        if op["op"] == "add_device" and op["device"].get("room") not in (None, *rooms):
            conflicts.append(f"{op['device']['id']}: unknown room {op['device']['room']}")
    summary = []
    for op in cs["ops"]:
        k = op["op"]
        if k == "add_device":
            d = op["device"]
            summary.append(f"+ {d['id']} {d['type']} @ ({d['x']:.3f}, {d['y']:.3f})")
        elif k == "update_device":
            summary.append(f"~ {op['id']} {op['changes']}")
        elif k == "remove_device":
            summary.append(f"- {op['id']} (and routes using it)")
        elif k in ("add_circuit", "update_circuit"):
            summary.append(f"{'+' if k == 'add_circuit' else '~'} circuit {op['circuit']['id']}")
        else:
            summary.append(f"{'+' if k == 'add_route' else '~'} route {op['route']['id']}")
    return {
        "changeset_id": cs["id"], "label": cs["label"], "base_revision": cs["base_revision"],
        "operations": len(cs["ops"]), "unchanged_inputs": cs.get("unchanged", 0),
        "changes": summary, "assumptions": cs["assumptions"], "conflicts": conflicts,
        "can_apply": not conflicts,
    }
