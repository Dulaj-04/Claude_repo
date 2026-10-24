"""MCP server exposing AutoCAD Electrical floor-drawing tools."""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__, dialux, export, inspection, planner, template, validate, workbook
from . import plan as plans
from .backends import autocad_backend
from .backends.base import BackendError
from .config import Config, safe_name
from .live import LiveSession, RealCom
from .model import Store, sha256_file
from .schematic import Schematic
from .template import VIEWS

logging.getLogger("ezdxf").setLevel(logging.ERROR)  # quiet stderr (stdio transport)

CFG = Config.from_env()
STORE = Store(CFG.workspace)
PLANS = plans.PlanStore(CFG.workspace)

INSTRUCTIONS = """\
Tools to plan, create, revise, validate and export electrical floor drawings and the inputs behind
them (AutoCAD Electrical workflow). Everything is a DRAFT drawing/calculation aid, not engineering
approval. Never invent ratings, factors or table values: ask the user, or record the gap.

WORK PLAN-FIRST (important)
1. At the start of a conversation about a project, call plan_show(project). If a plan exists,
   continue it; tell the user where it stands and what is next.
2. If the request is LARGE (more than one tool call, several drawings/floors, calculations, or it
   needs data from the user), do NOT start executing. First call plan_define with a clear goal,
   success criteria and scope (use plan_templates to pick a template), show the plan to the user and
   ask whether the goal is right. Call plan_approve only after they agree (user_confirmed=true).
3. Work step by step: plan_next tells you what is ready and EXACTLY what is needed from the user
   (files, Excel sheets, values, decisions). Ask for those inputs plainly, with the how-to from the
   plan. Excel input sheets: workbook_create gives a template, the user fills it in, workbook_read
   checks it, load_summary computes from it.
4. Keep the plan current: plan_provide_input when the user supplies something, plan_update_step with
   a result when a step finishes, plan_note for assumptions and decisions. End each reply with the
   short progress line and what you need next from the user.
5. Small, single-action requests (one placement, one inspection) need no plan.

Offline drawing flow: prepare_floor_model -> plan_devices / propose_lighting_grid /
set_circuit_assignment / plan_routes (each returns a changeset) -> preview_changes -> apply_changes ->
validate_drawing -> export_package. Reference drawings are never modified.
DIALux: dialux_inspect -> dialux_align -> import_luminaire_list -> dialux_import (changeset) ->
apply_changes (or live_import_luminaires) -> lighting_schedule.
Always pass explicit project and floor ids (letters, digits, '_' and '-' only).

LIVE MODE (Windows, drawing open in AutoCAD / AutoCAD Electrical): when the user wants to work
on the open drawing and see changes on screen, use the live_* tools instead: live_connect first,
then live_scan / live_adopt (give existing symbols ids), live_texts (room names and positions),
live_place / live_move / live_delete / live_assign / live_route, live_zoom to show the change,
live_undo to revert one instruction. Each call edits the open drawing immediately; keep steps
small and tell the user what changed. These draw plan-level geometry, not Electrical schematic
components.
SCHEMATIC MODE: only when the user asks to work with AutoCAD Electrical schematics, call
live_set_mode('schematic'), then sch_detect / sch_read / sch_probe. Otherwise stay in plan mode.
Check unit_scale_vs_mm from live_connect: reference drawings are in millimetres
even when the header says metres.
"""

mcp = FastMCP("acad-electrical", instructions=INSTRUCTIONS)


def _model(project: str, floor: str, create: bool = False) -> dict[str, Any]:
    return STORE.load(project, floor, create=create)


def _regen_and_record(model: dict[str, Any], overwrite: bool = False) -> list[dict[str, Any]]:
    out_dir = STORE.output_dir(model["project"], model["floor"])
    res = export.generate_dxf_views(model, out_dir, CFG.backend, None, overwrite)
    STORE.save(model)
    return res


# --------------------------------------------------------------------------- inspect
@mcp.tool()
def register_reference(path: str) -> dict[str, Any]:
    """Hash and inventory a reference DXF (or DWG with ODA converter): layers, entity counts,
    blocks, units, extents, layouts and text entities (candidate room labels). Read-only."""
    return inspection.inspect_drawing(path)


@mcp.tool()
def inspect_drawing(project: str, floor: str, view: Literal[
        "architectural", "lighting", "power", "combined"] = "combined") -> dict[str, Any]:
    """Inventory a generated output drawing of a floor (layers, entity counts, device ids)."""
    model = _model(project, floor)
    path = STORE.output_dir(project, floor) / export.view_filename(floor, view, "dxf")
    if not path.exists():
        raise ValueError(f"{path.name} has not been generated yet; run apply_changes or "
                         "generate_views.")
    info = inspection.inspect_drawing(str(path))
    import ezdxf
    info["devices_in_drawing"] = sorted(validate.read_devices(ezdxf.readfile(str(path))))
    info["model_revision"] = model["revision"]
    return info


@mcp.tool()
def list_devices(project: str, floor: str) -> dict[str, Any]:
    """List the logical devices, circuits, routes and rooms of a floor model."""
    m = _model(project, floor)
    return {k: m[k] for k in ("revision", "status", "units", "source", "rooms", "devices",
                              "circuits", "routes")}


# --------------------------------------------------------------------------- prepare
@mcp.tool()
def prepare_floor_model(
    project: str, floor: str, reference_path: str | None = None,
    rooms: list[dict[str, Any]] | None = None, replace_rooms: bool = False,
) -> dict[str, Any]:
    """Create/update the floor model: register the architectural reference (hash + units check)
    and rooms. Each room is {"id","name","bounds":[minx,miny,maxx,maxy]} in drawing units.
    Existing rooms are kept unless replace_rooms=true; differing names are reported, not
    overwritten silently. The reference file itself is never modified."""
    safe_name(project, "project")
    safe_name(floor, "floor")
    model = _model(project, floor, create=True)
    report: dict[str, Any] = {"warnings": [], "room_differences": []}
    if reference_path:
        info = inspection.inspect_drawing(reference_path, max_texts=0)
        model["source"] = {"path": info["path"], "sha256": info["sha256"]}
        model["units"] = info["units"]
        report["warnings"] += info["warnings"]
        report["reference"] = {k: info[k] for k in ("sha256", "units", "extents", "layouts")}
    for r in rooms or []:
        if not {"id", "name", "bounds"} <= set(r) or len(r["bounds"]) != 4:
            raise ValueError("Each room needs id, name and bounds [minx,miny,maxx,maxy]")
        b = [float(v) for v in r["bounds"]]
        if b[0] >= b[2] or b[1] >= b[3]:
            raise ValueError(f"Room {r['id']}: bounds must be [minx,miny,maxx,maxy] with min<max")
        cur = next((x for x in model["rooms"] if x["id"] == r["id"]), None)
        if cur and not replace_rooms:
            if cur["name"] != r["name"] or cur["bounds"] != b:
                report["room_differences"].append(
                    {"id": r["id"], "existing": cur, "supplied": {**r, "bounds": b}})
            continue
        model["rooms"] = [x for x in model["rooms"] if x["id"] != r["id"]]
        model["rooms"].append({"id": r["id"], "name": r["name"], "bounds": b})
    model["rooms"].sort(key=lambda r: r["id"])
    STORE.save(model)
    report.update(project=project, floor=floor, revision=model["revision"],
                  rooms=len(model["rooms"]), model_path=str(STORE.floor_dir(project, floor)))
    return report


@mcp.tool()
def prepare_template(project: str, floor: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """Customise the drawing template. Keys: layers {name:{color,lineweight,description}},
    symbol_size, text_height (drawing units), status, title_block {project,drawn_by,notes[]}.
    Regenerate views afterwards with generate_views."""
    model = _model(project, floor)
    model["template"] = template.merge_template(model["template"], overrides)
    STORE.save(model)
    return {"template": model["template"]}


# --------------------------------------------------------------------------- planning
def _store_cs(cs: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    STORE.save_changeset(cs)
    return planner.describe(cs, model)


@mcp.tool()
def plan_devices(
    project: str, floor: str, devices: list[dict[str, Any]],
    remove_ids: list[str] | None = None, tolerance: float = 0.01,
) -> dict[str, Any]:
    """Propose luminaires, switches, sockets, data, ac, emergency and db devices.
    Each device: {"type","x","y"} plus optional id, rotation, room, circuit, db, note.
    Same-type devices at the same position (within tolerance) are treated as already present,
    so repeating a plan creates no duplicates. Returns a changeset to preview/apply."""
    m = _model(project, floor)
    return _store_cs(planner.plan_devices(m, devices, remove_ids, tolerance), m)


@mcp.tool()
def propose_lighting_grid(
    project: str, floor: str, room_id: str, spacing_x: float, spacing_y: float,
    circuit: str | None = None, margin: float = 0.0,
) -> dict[str, Any]:
    """Propose a regular luminaire grid in a room (placement draft only, no lux calculation)."""
    m = _model(project, floor)
    return _store_cs(planner.propose_lighting_grid(m, room_id, spacing_x, spacing_y,
                                                   circuit, margin), m)


@mcp.tool()
def set_circuit_assignment(
    project: str, floor: str, circuit_id: str, kind: Literal["lighting", "power", "ac"],
    db: str | None = None, device_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create/update a circuit, bind it to a distribution board and assign devices to it."""
    m = _model(project, floor)
    return _store_cs(planner.set_circuit(m, circuit_id, kind, db, device_ids), m)


@mcp.tool()
def plan_routes(
    project: str, floor: str, circuit_id: str, device_ids: list[str] | None = None
) -> dict[str, Any]:
    """Propose an orthogonal route from the circuit's DB through its devices (nearest-first, or
    in the order given). The route is associated with the circuit/DB in the model; geometric
    crossings imply no connectivity. Draft drawing aid, not cable routing/sizing."""
    m = _model(project, floor)
    return _store_cs(planner.plan_route(m, circuit_id, device_ids), m)


@mcp.tool()
def preview_changes(project: str, floor: str, changeset_id: str) -> dict[str, Any]:
    """Show what a changeset would do (adds/updates/removals, assumptions, conflicts)."""
    m = _model(project, floor)
    return planner.describe(STORE.load_changeset(project, floor, changeset_id), m)


@mcp.tool()
def apply_changes(
    project: str, floor: str, changeset_id: str, overwrite_manual_edits: bool = False
) -> dict[str, Any]:
    """Apply a changeset: guarded by model revision, reference hash and manual-edit detection;
    snapshots the previous revision (undo_last), regenerates all four DXF views and reads them
    back for validation."""
    _gate("apply_changes")
    m = _model(project, floor)
    cs = STORE.load_changeset(project, floor, changeset_id)
    out_dir = STORE.output_dir(project, floor)
    try:
        new, stats = planner.apply_ops(m, cs)
    except planner.Conflict as exc:
        raise ValueError(str(exc)) from exc
    problems = validate.validate_model(new)
    errs = [i for i in problems if i["severity"] == "error"]
    if errs:
        raise ValueError("Changeset rejected, resulting model is invalid: "
                         + "; ".join(i["message"] for i in errs))
    STORE.snapshot(m)
    try:
        files = export.generate_dxf_views(new, out_dir, CFG.backend, None, overwrite_manual_edits)
    except BackendError as exc:
        raise ValueError(str(exc)) from exc
    STORE.save(new)
    STORE.delete_changeset(project, floor, changeset_id)
    report = validate.validate_floor(new, out_dir)
    return {"revision": new["revision"], **stats, "files": [f["dxf"] for f in files],
            "validation": {k: report[k] for k in ("ok", "errors", "warnings", "issues")}}


@mcp.tool()
def undo_last(project: str, floor: str, overwrite_manual_edits: bool = False) -> dict[str, Any]:
    """Restore the model to the revision before the last apply_changes and regenerate views."""
    m = _model(project, floor)
    snap = STORE.last_snapshot(project, floor)
    if snap is None:
        raise ValueError("Nothing to undo.")
    prev = json.loads(snap.read_text())
    prev["outputs"] = m["outputs"]
    prev["revision"] = m["revision"] + 1  # revisions only move forward
    try:
        _regen_and_record(prev, overwrite_manual_edits)
    except BackendError as exc:
        raise ValueError(str(exc)) from exc
    snap.unlink()
    return {"restored_from": snap.name, "revision": prev["revision"],
            "devices": len(prev["devices"])}


# --------------------------------------------------------------------------- views/export
@mcp.tool()
def generate_views(
    project: str, floor: str,
    views: list[Literal["architectural", "lighting", "power", "combined"]] | None = None,
    overwrite_manual_edits: bool = False,
) -> dict[str, Any]:
    """(Re)generate DXF views from the canonical model, e.g. after prepare_template."""
    m = _model(project, floor)
    out_dir = STORE.output_dir(project, floor)
    try:
        res = export.generate_dxf_views(m, out_dir, CFG.backend, views, overwrite_manual_edits)
    except BackendError as exc:
        raise ValueError(str(exc)) from exc
    STORE.save(m)
    return {"revision": m["revision"], "files": res}


@mcp.tool()
def export_package(
    project: str, floor: str,
    views: list[Literal["architectural", "lighting", "power", "combined"]] | None = None,
    overwrite_manual_edits: bool = False,
) -> dict[str, Any]:
    """Write the drawing package: DXF per view, DWG (needs live AutoCAD or ODA File Converter;
    otherwise reported as skipped), PNG previews, A3 PDF sheets and manifest.json with hashes
    and the validation result."""
    m = _model(project, floor)
    out_dir = STORE.output_dir(project, floor)
    try:
        manifest = export.export_package(m, out_dir, CFG.backend, views, overwrite_manual_edits)
    except BackendError as exc:
        raise ValueError(str(exc)) from exc
    STORE.save(m)
    return manifest


@mcp.tool()
def get_preview_image(
    project: str, floor: str,
    view: Literal["architectural", "lighting", "power", "combined"] = "combined",
) -> Image:
    """Render a PNG of a generated view so the drawing can be looked at."""
    _model(project, floor)
    out_dir = STORE.output_dir(project, floor)
    dxf = out_dir / export.view_filename(floor, view, "dxf")
    if not dxf.exists():
        raise ValueError(f"{dxf.name} has not been generated yet.")
    png = out_dir / export.view_filename(floor, view, "png")
    export.render_image(dxf, png, dark=True, size_in=(12, 8))
    return Image(path=str(png))


@mcp.tool()
def validate_drawing(project: str, floor: str) -> dict[str, Any]:
    """Validate model consistency and the generated files: devices/circuits/routes, labels,
    layers, architecture preserved vs reference, reference hash, manual edits, reopen check."""
    m = _model(project, floor)
    return validate.validate_floor(m, STORE.output_dir(project, floor))


@mcp.tool()
def sync_from_drawing(
    project: str, floor: str, view: Literal["lighting", "power", "combined"] = "combined",
    remove_missing: bool = False,
) -> dict[str, Any]:
    """Adopt manual edits: read device positions from an edited output DXF back into the model.
    Devices only in the drawing are reported (not adopted). Routes touching moved devices are
    dropped and must be re-planned. Use remove_missing=true to delete devices erased in CAD."""
    import ezdxf
    m = _model(project, floor)
    path = STORE.output_dir(project, floor) / export.view_filename(floor, view, "dxf")
    if not path.exists():
        raise ValueError(f"{path.name} does not exist.")
    found = validate.read_devices(ezdxf.readfile(str(path)))
    layers = set(VIEWS[view][1])
    from .ids import DEVICE_TYPES
    moved, missing = [], []
    for d in m["devices"]:
        if DEVICE_TYPES[d["type"]][1] not in layers:
            continue
        hit = found.get(d["id"])
        if not hit:
            missing.append(d["id"])
            continue
        h = hit[0]
        if abs(h["x"] - d["x"]) > 1e-6 or abs(h["y"] - d["y"]) > 1e-6:
            d["x"], d["y"] = h["x"], h["y"]
            moved.append(d["id"])
    unknown = sorted(set(found) - {d["id"] for d in m["devices"]})
    if remove_missing and missing:
        m["devices"] = [d for d in m["devices"] if d["id"] not in missing]
    drop = set(moved) | (set(missing) if remove_missing else set())
    dropped_routes = [r["id"] for r in m["routes"] if drop & set(r["device_ids"])]
    m["routes"] = [r for r in m["routes"] if r["id"] not in dropped_routes]
    if moved or (remove_missing and missing):
        STORE.snapshot(_model(project, floor))
        m["revision"] += 1
    m["outputs"][path.name] = sha256_file(path)  # edits accepted
    STORE.save(m)
    return {"revision": m["revision"], "moved": moved, "missing_in_drawing": missing,
            "removed": missing if remove_missing else [], "unknown_in_drawing": unknown,
            "routes_dropped_replan_needed": dropped_routes,
            "note": "Run generate_views to refresh the other views."}


@mcp.tool()
def list_floors(project: str) -> dict[str, Any]:
    """List floors that have a model in a project."""
    base = STORE.root / safe_name(project, "project")
    floors = sorted(p.parent.name for p in base.glob("*/model.json")) if base.exists() else []
    return {"project": project, "floors": floors, "output_root": str(base / "output")}


# --------------------------------------------------------------------------- live AutoCAD
@mcp.tool()
def acad_status() -> dict[str, Any]:
    """(Windows) Report the running AutoCAD / AutoCAD Electrical session via COM."""
    try:
        return autocad_backend.status()
    except BackendError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def acad_open(path: str) -> dict[str, Any]:
    """(Windows) Open a drawing read-only in the running AutoCAD session."""
    try:
        return autocad_backend.open_document(path)
    except BackendError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def acad_run_command(command: str) -> dict[str, Any]:
    """(Windows) Send a command line to the active AutoCAD document. Disabled unless the server
    was started with ACAD_MCP_ALLOW_COMMANDS=1 because commands can modify any open drawing."""
    if not CFG.allow_commands:
        raise ValueError("acad_run_command is disabled. Start the server with "
                         "ACAD_MCP_ALLOW_COMMANDS=1 to enable it.")
    try:
        return autocad_backend.run_command(command)
    except BackendError as exc:
        raise ValueError(str(exc)) from exc




# --------------------------------------------------------------------------- DIALux import
def _transform(dx, dy, rotation, scale, transform):
    return transform if transform else dialux.make_transform(dx, dy, rotation, scale)


@mcp.tool()
def dialux_inspect(path: str, include_nested: bool = False) -> dict[str, Any]:
    """Look inside a DIALux DWG/DXF export (read-only): block names with counts, layers,
    attributes, positions and units. Run this first, then choose which block names are luminaires
    for dialux_import. DWG needs the ODA File Converter on this machine; otherwise save/export it as
    DXF. Set include_nested=true if the luminaires sit inside one big block."""
    return _guard(lambda: dialux.inspect(path, include_nested))


@mcp.tool()
def dialux_align(source_a: list[float], source_b: list[float], target_a: list[float],
                 target_b: list[float]) -> dict[str, Any]:
    """Work out the transform (shift, rotation, scale) that maps the DIALux export onto the
    architectural drawing, from two points you can identify in both (e.g. two column corners):
    source_a/b in the DIALux file, target_a/b in the architectural drawing. Pass the result as
    `transform` to dialux_import. 'residual' is the leftover error at the second point (drawing
    units); a large value means the points do not match."""
    return _guard(lambda: dialux.fit_two_points(source_a, source_b, target_a, target_b))


@mcp.tool()
def import_luminaire_list(path: str, sheet: str | None = None,
                          columns: dict[str, str] | None = None) -> dict[str, Any]:
    """Read a luminaire list (CSV, or XLSX with openpyxl) exported from DIALux or Excel: type/name,
    quantity, wattage and luminous flux, with the columns detected automatically (override with
    columns={'type': 'Header', 'watts': 'Header', ...}). Read-only. Wattage is never invented: types
    without one are listed in missing_watts."""
    return _guard(lambda: dialux.read_luminaire_list(path, sheet, columns))


@mcp.tool()
def dialux_import(
    project: str, floor: str, path: str, block_names: list[str] | None = None,
    layers: list[str] | None = None, include_nested: bool = False,
    dx: float = 0.0, dy: float = 0.0, rotation: float = 0.0, scale: float = 1.0,
    transform: dict[str, float] | None = None, type_map: dict[str, str] | None = None,
    luminaire_list: str | None = None, watts_by_type: dict[str, float] | None = None,
    circuit: str | None = None, label_attribute: str | None = None,
    replace_previous: bool = False, tolerance: float = 0.01,
) -> dict[str, Any]:
    """Import luminaires from a DIALux DWG/DXF export into the floor model as a CHANGESET (nothing
    is drawn until apply_changes). block_names/layers choose what to import (see dialux_inspect);
    dx/dy/rotation/scale or `transform` (from dialux_align) position it on the architectural
    drawing; type_map maps block names to device types (default luminaire; e.g. {'EXIT_SIGN':
    'emergency'}); luminaire_list (CSV/XLSX path) and/or watts_by_type supply wattage and lumens;
    rooms are assigned from the model's room bounds. Re-importing updates in place, no duplicates;
    replace_previous=true also removes earlier DIALux devices that are no longer in the file."""
    m = _model(project, floor)

    def run():
        t = _transform(dx, dy, rotation, scale, transform)
        found = dialux.extract(path, block_names, layers, include_nested, t)
        if not found:
            raise ValueError("No matching blocks found. Check block_names/layers with dialux_inspect.")
        specs = dialux.specs_from_list(dialux.read_luminaire_list(luminaire_list)) \
            if luminaire_list else {}
        tag = f"dialux:{Path(path).name}:{dialux.sha256_file(path)[:8]}"
        devs, info = dialux.build_device_specs(m, found, type_map, specs, watts_by_type, circuit,
                                               tag, label_attribute)
        remove = []
        if replace_previous:
            for d in m["devices"]:
                if str(d.get("source", "")).startswith("dialux:") and not any(
                        n["type"] == d["type"] and abs(n["x"] - d["x"]) <= tolerance
                        and abs(n["y"] - d["y"]) <= tolerance for n in devs):
                    remove.append(d["id"])
        cs = planner.plan_devices(m, devs, remove, tolerance)
        cs["label"] = "dialux_import"
        chk = dialux.extent_check(m, devs)
        info["extent_check"] = chk
        if chk and chk["suspicious"]:
            cs["assumptions"].append(
                f"SCALE/UNITS LOOK WRONG: the imported luminaires span {chk['imported_span']} but "
                f"the rooms span {chk['rooms_span']} (ratio {chk['ratio']}). Check the units "
                "(DIALux exports metres) and use dialux_align or scale/dx/dy.")
        if info["outside_all_rooms"]:
            cs["assumptions"].append(
                f"{info['outside_all_rooms']} imported luminaire(s) fall outside every room in the "
                "model: check the alignment (dialux_align) or add the missing rooms.")
        if info["types_without_watts"]:
            cs["assumptions"].append(
                f"No wattage for {info['types_without_watts']}: supply luminaire_list or "
                "watts_by_type; the lighting schedule will show them as missing.")
        cs["assumptions"].append(
            f"Imported from {Path(path).name}; positions as exported by DIALux, transformed with "
            f"{t}. Values are as supplied, not verified.")
        STORE.save_changeset(cs)
        return {**planner.describe(cs, m), "found_in_file": len(found),
                "by_block": dict(Counter(f["block"] for f in found)), **info,
                "transform": t, "removed_previous": remove}

    return _guard(run)


@mcp.tool()
def lighting_schedule(project: str, floor: str, watts_by_type: dict[str, float] | None = None,
                      unit_to_m: float | None = None) -> dict[str, Any]:
    """Lighting schedule for a floor from the model: luminaire counts by type per room, installed
    wattage per room and floor (and W/m2 when room area is known). Wattage comes from the device
    (e.g. imported from DIALux) or watts_by_type; luminaires without one are reported as missing,
    never guessed. unit_to_m = metres per drawing unit (0.001 for a millimetre drawing). This is
    installed load only: no demand factor, no lux calculation."""
    m = _model(project, floor)
    return _guard(lambda: dialux.lighting_schedule(m, watts_by_type, unit_to_m))


# --------------------------------------------------------------------------- plan-first workflow
def _pl(project: str) -> dict[str, Any]:
    try:
        return PLANS.load(project)
    except plans.PlanError as exc:
        raise ValueError(str(exc)) from exc


def _plan_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except plans.PlanError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def plan_templates() -> dict[str, Any]:
    """List the plan templates (lighting_from_dialux, load_and_max_demand, cable_and_protection,
    drawing_from_reference, sld_and_boq, full_project) with their steps, the inputs they need and
    which steps are not automated yet, and the Excel input workbooks available."""
    return {"templates": plans.list_templates(), "workbooks": workbook.list_workbooks()}


@mcp.tool()
def plan_define(project: str, title: str, goal: str, success_criteria: list[str],
                scope_in: list[str] | None = None, scope_out: list[str] | None = None,
                template: str | None = None, replace: bool = False) -> dict[str, Any]:
    """Start a plan for a large request BEFORE doing any work: a title, a one-paragraph goal, the
    success criteria ('done when...'), what is in and out of scope, and optionally a template that
    pre-fills the steps and the inputs needed from the user. The plan starts as a draft: show it to
    the user and call plan_approve once they agree. Use replace=true to overwrite an existing plan."""
    safe_name(project, "project")
    if PLANS.exists(project) and not replace:
        raise ValueError(f"A plan already exists for {project!r}. Call plan_show, or pass "
                         "replace=true to start over.")
    plan = _plan_call(plans.define, project, title, goal, success_criteria, scope_in, scope_out,
                      template)
    PLANS.save(plan)
    PLANS.set_active(project)
    out = plans.summary(plan)
    out["tell_the_user"] = ("Show the goal, success criteria, steps and the inputs needed from the "
                            "user, and ask: is this the right goal? Then call plan_approve.")
    return out


@mcp.tool()
def plan_show(project: str) -> dict[str, Any]:
    """Current plan for a project: goal, approval, step statuses, inputs received/missing, what is
    ready now and exactly what is needed from the user, plus a markdown checklist to show them.
    Call this at the start of a session and after every step."""
    plan = _pl(project)
    PLANS.set_active(project)
    return plans.summary(plan)


@mcp.tool()
def plan_next(project: str) -> dict[str, Any]:
    """What to do next: steps that are ready, steps that are blocked (and by what), and the list
    of inputs the user must supply with how to supply them (including the Excel template to use).
    Ask the user for these in plain words."""
    plan = _pl(project)
    return {**plans.next_actions(plan), "checklist": plans.render_markdown(plan)}


@mcp.tool()
def plan_approve(project: str, user_confirmed: bool, note: str | None = None) -> dict[str, Any]:
    """Record that the USER agreed to the goal and scope. Pass user_confirmed=true only after the
    user has actually said yes in this conversation; never on your own initiative."""
    if not user_confirmed:
        raise ValueError("Ask the user whether the goal and scope are right first; call this with "
                         "user_confirmed=true only after they say yes.")
    plan = _pl(project)
    plan["goal"]["approved"], plan["goal"]["approval_note"] = True, note
    plan["status"] = "approved" if plan["status"] == "draft" else plan["status"]
    plans._log(plan, "Goal approved by the user" + (f": {note}" if note else ""))
    PLANS.save(plan)
    PLANS.set_active(project)
    return plans.summary(plan)


@mcp.tool()
def plan_update_step(project: str, step_id: str, status: Literal[
        "pending", "in_progress", "done", "blocked", "skipped"], note: str | None = None,
        result: str | None = None, force: bool = False) -> dict[str, Any]:
    """Update a step. Finishing a step ('done') needs a result (files written, counts, key numbers,
    decisions) and is refused while its inputs are missing or earlier steps are unfinished, unless
    force=true with a note saying why. Call this as each step starts and finishes."""
    plan = _pl(project)
    _plan_call(plans.update_step, plan, step_id, status, note, result, force)
    PLANS.save(plan)
    return plans.summary(plan)


@mcp.tool()
def plan_provide_input(project: str, input_id: str, reference: str | None = None,
                       note: str | None = None, not_applicable: bool = False) -> dict[str, Any]:
    """Record that the user supplied an input: reference = file path, value, or a short description
    of the decision; or not_applicable=true if it does not apply. Unblocks the steps that need it."""
    plan = _pl(project)
    _plan_call(plans.provide_input, plan, input_id, reference, note, not_applicable)
    PLANS.save(plan)
    return {**plans.next_actions(plan), "checklist": plans.render_markdown(plan)}


@mcp.tool()
def plan_add_step(project: str, title: str, kind: Literal[
        "input", "calc", "drawing", "review", "output"], tools: list[str] | None = None,
        needs_inputs: list[str] | None = None, depends_on: list[str] | None = None,
        guidance: str | None = None) -> dict[str, Any]:
    """Add a step to the plan (e.g. when the scope grows). Say which inputs it needs and which
    steps must finish first."""
    plan = _pl(project)
    s = _plan_call(plans.add_step, plan, title, kind, tools, needs_inputs, depends_on, guidance)
    PLANS.save(plan)
    return {"added": s, "checklist": plans.render_markdown(plan)}


@mcp.tool()
def plan_add_input(project: str, input_id: str, title: str, why: str, how: str,
                   kind: Literal["file", "value", "table", "decision", "workbook"] = "value",
                   workbook: str | None = None) -> dict[str, Any]:
    """Add something the plan needs from the user: why it is needed and exactly how to provide it.
    workbook = name of an Excel template (see plan_templates) if it should be supplied that way."""
    plan = _pl(project)
    i = _plan_call(plans.add_input, plan, input_id, title, why, kind, how, workbook)
    PLANS.save(plan)
    return {"added": i, "checklist": plans.render_markdown(plan)}


@mcp.tool()
def plan_note(project: str, text: str, kind: Literal["assumption", "decision", "note"] = "assumption",
              source: str | None = None) -> dict[str, Any]:
    """Record an assumption, a decision or a note with its source (who/what it came from), so it
    is visible in the plan and in the report. Use it for every number or choice the user gave you."""
    plan = _pl(project)
    n = plans.note(plan, text, kind, source)
    PLANS.save(plan)
    return {"recorded": n, "count": len(plan["assumptions"])}


# --------------------------------------------------------------------------- Excel inputs
@mcp.tool()
def workbook_create(project: str, kind: Literal[
        "load_schedule", "lighting_requirements", "luminaire_list", "cable_protection_inputs"],
        overwrite: bool = False) -> dict[str, Any]:
    """Create an Excel input sheet for the user to fill in (instructions sheet, 'Data' sheet with
    required columns marked *, dropdowns and visible formulas). Saved under
    <workspace>/<project>/inputs/. Tell the user the path and what to fill in; then call
    workbook_read once they have saved it. An existing file is never overwritten unless asked."""
    return _guard(lambda: workbook.create(CFG.workspace, project, kind, overwrite))


@mcp.tool()
def workbook_read(project: str, kind: Literal[
        "load_schedule", "lighting_requirements", "luminaire_list", "cable_protection_inputs"],
        path: str | None = None, plan_input_id: str | None = None) -> dict[str, Any]:
    """Read and validate a filled-in sheet (default: the project's inputs/<kind>.xlsx): missing
    required fields, bad numbers, factors outside 0-1, unknown categories, duplicate ids. Returns
    the rows and a list of problems with row numbers to give the user. If plan_input_id is given
    and the sheet has no errors, that plan input is marked provided."""
    p = path or str(workbook.inputs_dir(CFG.workspace, project) / f"{kind}.xlsx")
    res = _guard(lambda: workbook.read(p, kind))
    if plan_input_id and res["ok"] and PLANS.exists(project):
        plan = PLANS.load(project)
        _plan_call(plans.provide_input, plan, plan_input_id, p, f"{res['row_count']} rows, no errors",
                   False)
        PLANS.save(plan)
        res["plan_input_marked_provided"] = plan_input_id
    return res


@mcp.tool()
def load_summary(project: str, path: str | None = None, plan_input_id: str | None = None
                 ) -> dict[str, Any]:
    """Connected load and maximum demand from the filled-in load schedule, by floor, board and
    category, plus the essential loads. Demand uses ONLY the demand factors the user entered: rows
    without one are listed and left out of the demand total (never assumed to be 1), and the result
    is marked incomplete. Refuses a sheet that has errors."""
    p = path or str(workbook.inputs_dir(CFG.workspace, project) / "load_schedule.xlsx")
    res = _guard(lambda: workbook.read(p, "load_schedule"))
    if not res["ok"]:
        raise ValueError(f"The load schedule has {res['errors']} error(s); fix them first "
                         f"(workbook_read shows rows and columns): {res['issues'][:5]}")
    out = workbook.load_summary(res["rows"])
    out["source_file"] = res["path"]
    out["rows_used"] = res["row_count"]
    return out


# --------------------------------------------------------------------------- prompts
@mcp.prompt()
def plan_a_request(project: str, request: str) -> str:
    """Turn a request into a structured plan before doing any work."""
    return (f"Project: {project}\nRequest: {request}\n\nWork plan-first with the acad-electrical "
            "tools. 1) plan_show to see whether a plan already exists. 2) If this request is large, "
            "pick a template with plan_templates, call plan_define with a clear goal, success "
            "criteria and scope, show it to me and ask if the goal is right. 3) Only after I agree, "
            "plan_approve. 4) Then use plan_next to tell me exactly what you need from me (files, "
            "Excel sheets, values, decisions) and work step by step, keeping the plan updated.")


@mcp.prompt()
def lighting_from_dialux(project: str, floor: str) -> str:
    """Plan and run a DIALux lighting import for one floor."""
    return (f"Project {project}, floor {floor}: import our DIALux lighting layout. Use "
            "plan_define with template 'lighting_from_dialux', show me the goal and the inputs you "
            "need (architectural drawing, DIALux DWG/DXF export, luminaire list, alignment points), "
            "wait for my approval, then follow plan_next step by step.")


@mcp.prompt()
def load_schedule_and_demand(project: str) -> str:
    """Plan a load schedule and maximum demand calculation using Excel input sheets."""
    return (f"Project {project}: build the load schedule and maximum demand. Use plan_define with "
            "template 'load_and_max_demand', create the load_schedule workbook, tell me exactly how "
            "to fill it in, check it with workbook_read, then run load_summary. Do not assume demand "
            "factors: ask me for them and record their source with plan_note.")


@mcp.prompt()
def project_roadmap(project: str) -> str:
    """Set up the whole electrical installation project as a roadmap of separate plans."""
    return (f"Project {project}: set up the whole electrical installation work plan. Use plan_define "
            "with template 'full_project' as the roadmap, tell me which parts are automated and "
            "which are not yet, and propose which part to plan first.")

# --------------------------------------------------------------------------- live editing
_LIVE: LiveSession | None = None


def _live() -> LiveSession:
    """Session bound to the drawing currently open in AutoCAD (created on first use)."""
    global _LIVE
    if _LIVE is None:
        _LIVE = LiveSession(RealCom(), CFG.workspace / "live")
    return _LIVE


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except BackendError as exc:
        raise ValueError(str(exc)) from exc
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface COM errors readably to the assistant
        raise ValueError(f"AutoCAD call failed: {type(exc).__name__}: {exc}") from exc


def _gate(what: str) -> None:
    """Strict mode (ACAD_MCP_REQUIRE_PLAN=1): drawing-changing tools need an approved plan goal."""
    if not CFG.require_plan:
        return
    project = PLANS.active()
    ok = False
    if project and PLANS.exists(project):
        ok = PLANS.load(project)["goal"]["approved"]
    if not ok:
        raise ValueError(
            f"{what} is blocked: this server requires an approved plan before it changes drawings "
            "(ACAD_MCP_REQUIRE_PLAN=1). Define the goal with plan_define, show it to the user and "
            "call plan_approve after they agree.")


def _plan(write: bool = True) -> LiveSession:
    if write:
        _gate("Changing the open drawing")
    live = _live()
    if live.mode != "plan":
        raise ValueError("Schematic mode is on, so plan-level drawing tools are disabled. Use the "
                         "sch_* tools, or call live_set_mode('plan') to go back.")
    return live


@mcp.tool()
def live_set_mode(mode: Literal["plan", "schematic"]) -> dict[str, Any]:
    """Choose what the live tools work with. 'plan' (default): floor-plan symbols and routes
    (live_place, live_route...). 'schematic': AutoCAD Electrical schematics (sch_* tools); the
    plan-level drawing tools are then blocked so the two are never mixed by accident. Only switch
    to 'schematic' when the user asks to work with schematics."""
    live = _live()
    live.mode = mode
    return {"mode": mode, "use": "sch_detect, sch_read, sch_probe" if mode == "schematic"
            else "live_place, live_move, live_route, ..."}


@mcp.tool()
def sch_detect() -> dict[str, Any]:
    """SCHEMATIC: check the open drawing / AutoCAD Electrical: product and version, whether the
    Electrical commands are loaded, whether the AutoLISP bridge works, and how many components,
    wire numbers and wire lines the drawing has. Run this first in schematic mode."""
    return _guard(lambda: Schematic(_live()).detect())


@mcp.tool()
def sch_read(include_wires: bool = True, limit: int = 500) -> dict[str, Any]:
    """SCHEMATIC: read the open schematic: components (tag, description, installation, location,
    manufacturer, catalog, terminals), wire numbers and wire-layer line counts. Read-only."""
    return _guard(lambda: Schematic(_live()).read(include_wires, limit))


@mcp.tool()
def sch_probe(prefix: str = "c:ae", limit: int = 400) -> dict[str, Any]:
    """SCHEMATIC: list the AutoLISP functions/commands this AutoCAD Electrical install exposes whose
    name starts with prefix (try c:ae, c:wd, c:ace, wd_, ace_). Read-only; used to learn the real
    command names before insert/wire tools are built."""
    return _guard(lambda: Schematic(_live()).probe(prefix, limit))


@mcp.tool()
def sch_check_commands(names: list[str]) -> dict[str, Any]:
    """SCHEMATIC: ask AutoCAD whether each command name is registered (e.g. AEPROJECT). Unlike
    sch_probe this also sees commands provided by compiled Electrical modules. Read-only."""
    return {"registered": _guard(lambda: Schematic(_live()).commands_exist(names))}


@mcp.tool()
def sch_modules() -> dict[str, Any]:
    """SCHEMATIC: list the compiled modules (ARX) loaded in the open AutoCAD session; shows
    whether AutoCAD Electrical's own modules are active. Read-only."""
    mods = _guard(lambda: Schematic(_live()).arx_modules())
    return {"count": len(mods), "modules": mods}


@mcp.tool()
def sch_run_lisp(expression: str) -> dict[str, Any]:
    """SCHEMATIC (advanced): evaluate an AutoLISP expression in the open drawing and return its
    printed result. Can change the drawing, so it is disabled unless the server was started with
    ACAD_MCP_ALLOW_COMMANDS=1."""
    if not CFG.allow_commands:
        raise ValueError("sch_run_lisp is disabled. Start the server with ACAD_MCP_ALLOW_COMMANDS=1.")
    return {"result": _guard(lambda: Schematic(_live()).lisp(expression))}


@mcp.tool()
def live_connect() -> dict[str, Any]:
    """LIVE (Windows): attach to the drawing open in AutoCAD / AutoCAD Electrical and report its
    name, entity counts per layer and unit scale. Start here. All live_* tools edit the open
    drawing directly, so the user sees every change on screen."""
    return _guard(lambda: _live().connect())


@mcp.tool()
def live_selftest() -> dict[str, Any]:
    """LIVE: prove the connection works. Creates a DB, luminaire and route on the ACTIVE drawing,
    moves and reads them back, then removes them. Run on a scratch drawing the first time."""
    return _guard(lambda: _live().selftest())


@mcp.tool()
def live_scan(layers: list[str] | None = None) -> dict[str, Any]:
    """LIVE: find symbols in the open drawing (loose LINEs that touch are clustered per device
    layer, as in the reference drawings). Returns type, centre and size of each, not yet tracked."""
    cl = _guard(lambda: _live().scan(layers))
    by: dict[str, int] = {}
    for c in cl:
        by[c["layer"]] = by.get(c["layer"], 0) + 1
    return {"count": len(cl), "by_layer": by, "symbols": cl[:300]}


@mcp.tool()
def live_adopt(floor: str, layers: list[str] | None = None, min_size: float = 0.0,
               max_size: float = 1e12) -> dict[str, Any]:
    """LIVE: give the existing symbols in the open drawing stable ids (e.g. 4F-LUM-01, DB-4F) by
    grouping them, so they can be moved, deleted or assigned by id. Geometry is not changed.
    Use min_size/max_size (drawing units) to skip tiny pieces such as socket pins."""
    safe_name(floor, "floor")
    return _guard(lambda: _plan().adopt(floor, layers, min_size, max_size))


@mcp.tool()
def live_list_devices() -> dict[str, Any]:
    """LIVE: tracked devices/routes with positions read fresh from the drawing (flags devices
    the user moved by hand, and ones deleted by hand)."""
    return _guard(lambda: _live().list_devices())


@mcp.tool()
def live_texts(layer: str | None = None, contains: str | None = None) -> dict[str, Any]:
    """LIVE: read TEXT in the open drawing with positions (e.g. layer 'TEXT' for room names, to
    work out where a room is before placing devices)."""
    return {"texts": _guard(lambda: _live().texts(layer, contains))}


@mcp.tool()
def live_place(
    floor: str, type: Literal["luminaire", "switch", "socket", "data", "ac", "emergency", "db"],
    x: float, y: float, rotation: float = 0.0, circuit: str | None = None,
    db: str | None = None, room: str | None = None,
) -> dict[str, Any]:
    """LIVE: draw a device symbol (reference style, correct layer) at x,y in the open drawing now.
    Same type at the same spot is not duplicated. One undo step."""
    safe_name(floor, "floor")
    return _guard(lambda: _plan().place(floor, type, x, y, rotation, circuit, db, room))


@mcp.tool()
def live_import_luminaires(
    floor: str, path: str, block_names: list[str] | None = None, layers: list[str] | None = None,
    include_nested: bool = False, dx: float = 0.0, dy: float = 0.0, rotation: float = 0.0,
    scale: float = 1.0, transform: dict[str, float] | None = None,
    type_map: dict[str, str] | None = None, luminaire_list: str | None = None,
    watts_by_type: dict[str, float] | None = None, circuit: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """LIVE: draw luminaires from a DIALux DWG/DXF export into the OPEN AutoCAD drawing, as one
    undo step. Same parameters as dialux_import. dry_run=true (default) only reports what would be
    drawn (counts per block, first positions); call again with dry_run=false to draw. Already placed
    luminaires at the same spot are skipped."""
    safe_name(floor, "floor")

    def run():
        live = _plan(write=not dry_run)
        t = _transform(dx, dy, rotation, scale, transform)
        found = dialux.extract(path, block_names, layers, include_nested, t)
        if not found:
            raise ValueError("No matching blocks found. Check block_names/layers with dialux_inspect.")
        specs = dialux.specs_from_list(dialux.read_luminaire_list(luminaire_list)) \
            if luminaire_list else {}
        tag = f"dialux:{Path(path).name}:{dialux.sha256_file(path)[:8]}"
        devs, info = dialux.build_device_specs({"rooms": []}, found, type_map, specs,
                                               watts_by_type, circuit, tag)
        report = {"found_in_file": len(found), "by_block": dict(Counter(f["block"] for f in found)),
                  "types_without_watts": info["types_without_watts"], "transform": t,
                  "first_positions": [[d["x"], d["y"]] for d in devs[:10]]}
        if dry_run:
            return {"dry_run": True, **report, "next": "call again with dry_run=false to draw"}
        items = [{"type": d["type"], "x": d["x"], "y": d["y"], "rotation": d["rotation"],
                  "circuit": d["circuit"],
                  "extra": {k: d[k] for k in ("luminaire_type", "watts", "lumens", "source")
                            if d.get(k) is not None}} for d in devs]
        return {"dry_run": False, **report, **live.place_many(floor, items)}

    return _guard(run)


@mcp.tool()
def live_move(device_id: str, x: float | None = None, y: float | None = None,
              dx: float | None = None, dy: float | None = None) -> dict[str, Any]:
    """LIVE: move a tracked device to x,y or by dx,dy. Routes through it are flagged stale."""
    return _guard(lambda: _plan().move(device_id, x, y, dx, dy))


@mcp.tool()
def live_delete(device_id: str) -> dict[str, Any]:
    """LIVE: delete a tracked device from the open drawing (one undo step)."""
    return _guard(lambda: _plan().delete(device_id))


@mcp.tool()
def live_assign(device_id: str, circuit: str, db: str | None = None) -> dict[str, Any]:
    """LIVE: record which circuit / distribution board a device belongs to."""
    return _guard(lambda: _plan().assign(device_id, circuit, db))


@mcp.tool()
def live_route(circuit: str, kind: Literal["lighting", "power", "ac"],
               device_ids: list[str] | None = None, db: str | None = None,
               label: bool = True) -> dict[str, Any]:
    """LIVE: draw (or redraw, replacing the old one) an orthogonal route from the DB through the
    circuit's devices on the lighting/power/AC wiring layer, labelled with the circuit id."""
    return _guard(lambda: _plan().route(circuit, kind, device_ids, db, label))


@mcp.tool()
def live_add_text(layer: str, text: str, x: float, y: float, height: float) -> dict[str, Any]:
    """LIVE: add a TEXT note/label to the open drawing."""
    return _guard(lambda: _plan().add_text(layer, text, x, y, height))


@mcp.tool()
def live_add_polyline(layer: str, points: list[list[float]]) -> dict[str, Any]:
    """LIVE: draw a polyline [[x,y],...] on a layer in the open drawing."""
    return _guard(lambda: _plan().add_polyline(layer, points))


@mcp.tool()
def live_zoom(x: float, y: float, width: float) -> dict[str, Any]:
    """LIVE: pan/zoom AutoCAD's view to x,y so the user can see the change."""
    return _guard(lambda: _live().zoom(x, y, width))


@mcp.tool()
def live_undo() -> dict[str, Any]:
    """LIVE: undo the last chat instruction in AutoCAD (each live_* call is one undo step)."""
    return _guard(lambda: _live().undo())


@mcp.tool()
def live_save(path: str | None = None) -> dict[str, Any]:
    """LIVE: save the open drawing (or Save As to path)."""
    return _guard(lambda: _live().save(path))


# --------------------------------------------------------------------------- CLI
def _selftest() -> int:
    import asyncio
    import sys
    import tempfile

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run() -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "acad_electrical_mcp.server"],
                env={**os.environ, "ACAD_MCP_WORKSPACE": tmp},
            )
            async with stdio_client(params) as (r, w), ClientSession(r, w) as sess:
                await sess.initialize()
                return sorted(t.name for t in (await sess.list_tools()).tools)

    try:
        names = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - report any startup failure
        print(f"SELFTEST FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"SELFTEST OK: server started and exposes {len(names)} tools:")
    print("  " + ", ".join(names))
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="acad-electrical-mcp", description=__doc__)
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--allowed-host", action="append", default=[],
                    help="extra Host header allowed over HTTP (e.g. your ngrok domain)")
    ap.add_argument("--workspace", help="project/output folder (default ./acad_mcp_workspace)")
    ap.add_argument("--backend", choices=["dxf", "autocad"], help="drawing backend")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--make-sample", metavar="FILE.dxf",
                    help="write a small sample architectural floor DXF and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="start the server over stdio, list its tools and exit (0 = OK)")
    args = ap.parse_args(argv)
    if args.selftest:
        raise SystemExit(_selftest())
    if args.make_sample:
        from .sample import make_sample_floor
        print(make_sample_floor(args.make_sample))
        return
    if args.workspace:
        CFG.workspace = Path(args.workspace).resolve()
        STORE.root = CFG.workspace
    if args.backend:
        CFG.backend = args.backend
    CFG.workspace.mkdir(parents=True, exist_ok=True)
    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if args.allowed_host or args.host not in ("127.0.0.1", "localhost", "::1"):
            hosts = args.allowed_host + [f"{args.host}:*", "127.0.0.1:*", "localhost:*"]
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                allowed_origins=[f"https://{h}" for h in args.allowed_host]
                + ["https://chatgpt.com", "https://claude.ai"],
            )
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
