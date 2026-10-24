"""Structured project plans: a goal, ordered steps, and the inputs needed from the user.

A plan is stored per project (``<workspace>/<project>/plan.json``) so every session starts from the
same record. Steps declare which inputs they need (files, Excel sheets, values, decisions); a step is
only *ready* when its dependencies are done and its inputs are provided, so the plan can always say
what is next and exactly what is waiting on the user. Nothing here calculates engineering values.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import safe_name

STEP_STATUS = ("pending", "in_progress", "done", "blocked", "skipped")
INPUT_STATUS = ("missing", "provided", "not_applicable")
STEP_KINDS = ("input", "calc", "drawing", "review", "output")
_DONE = ("done", "skipped")


class PlanError(ValueError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _inp(id: str, title: str, why: str, kind: str, how: str, workbook: str | None = None) -> dict:
    return {"id": id, "title": title, "why": why, "kind": kind, "how": how, "workbook": workbook,
            "status": "missing", "reference": None, "note": None}


def _step(id: str, title: str, kind: str, tools: list[str], needs: list[str], deps: list[str],
          guidance: str, built: bool = True) -> dict:
    return {"id": id, "title": title, "kind": kind, "tools": tools, "needs": needs,
            "depends_on": deps, "guidance": guidance, "built": built, "status": "pending",
            "note": None, "result": None}


# --------------------------------------------------------------------------- templates
def _t_lighting_from_dialux() -> tuple[list, list]:
    inputs = [
        _inp("arch_dxf", "Architectural drawing of the floor (DXF/DWG)", "Base for rooms and for aligning the DIALux layout.", "file", "Give the file path. DWG needs the ODA File Converter, otherwise save as DXF."),
        _inp("room_list", "Room list with approved ids and names", "Rooms receive the luminaires and define areas.", "table", "Room id, name and bounds [minx,miny,maxx,maxy] in drawing units (read candidate names with register_reference), or confirm those read from the drawing."),
        _inp("dialux_export", "DIALux DWG/DXF export with luminaires as blocks", "Source of luminaire positions.", "file", "DIALux: Export to CAD/DWG with luminaires placed. Give the file path."),
        _inp("luminaire_list", "Luminaire list with wattage (and lumens)", "Gives the installed load; wattage is never guessed.", "workbook", "Excel/CSV from DIALux, or fill the luminaire_list template (workbook_create).", "luminaire_list"),
        _inp("alignment_points", "Two points identifiable in both drawings", "Aligns the DIALux frame (usually metres) to the architecture.", "value", "Coordinates of two corners in the DIALux file and the same two in the architectural drawing."),
        _inp("lighting_requirements", "Target illuminance per room (record)", "Documents the design basis; lux is calculated in DIALux, not here.", "workbook", "Fill the lighting_requirements template.", "lighting_requirements"),
        _inp("circuit_plan", "Circuit grouping and DB location", "Needed to assign circuits and draw routes.", "decision", "Which luminaires share a circuit, circuit ids (e.g. 4F-L01), and where the DB is."),
    ]
    steps = [
        _step("s0a", "Inventory the files you were given", "review", ["intake_scan"], [], [], "Lists every file, its type, floor, units and size; flags missing floors, duplicates and scale mismatches between floors. Nothing is changed."),
        _step("s0b", "Protect the base drawings and make working copies", "drawing", ["base_register", "floor_from_base"], ["arch_dxf"], ["s0a"], "Base drawings become read-only for the MCP; every floor is drawn in its own copy."),
        _step("s0c", "Slim down a heavy DIALux export (on a copy)", "drawing", ["dxf_sanitize", "live_inventory", "live_sanitize"], ["dialux_export"], ["s0b"], "Heavy DIALux exports (hundreds of thousands of mesh objects) can stall AutoCAD. Strips the calculation/mesh layers and scales metres to millimetres, always on a copy."),
        _step("s1", "Prepare the floor model (rooms + architectural reference)", "drawing", ["register_reference", "prepare_floor_model"], ["arch_dxf", "room_list"], [], "Registers the reference file and rooms; differences from approved names are reported, not overwritten."),
        _step("s2", "Inspect the DIALux export", "review", ["dialux_inspect", "live_inventory"], ["dialux_export"], ["s0c"], "Lists block names, counts, layers and units. Decide which blocks are luminaires / emergency signs."),
        _step("s3", "Align the DIALux layout to the architecture", "calc", ["dialux_align"], ["alignment_points"], ["s2"], "Check the residual is near zero."),
        _step("s4", "Read and check the luminaire list", "input", ["workbook_read", "import_luminaire_list"], ["luminaire_list"], [], "Confirms wattage per luminaire type; lists types with missing wattage."),
        _step("s5", "Import luminaires as a changeset and review it", "drawing", ["dialux_import", "preview_changes"], [], ["s1", "s3", "s4"], "Review counts per block, luminaires outside rooms, scale warning, missing wattage. Apply only after the user agrees."),
        _step("s6", "Apply the import (offline model or live drawing)", "drawing", ["apply_changes", "live_import_luminaires"], [], ["s5"], "Offline: apply_changes. Live: dry run first, then draw as one undo step."),
        _step("s7", "Lighting schedule", "output", ["lighting_schedule"], [], ["s6"], "Installed watts per room/floor. Not a lux calculation; no demand factor."),
        _step("s8", "Assign circuits and draw routes", "drawing", ["set_circuit_assignment", "plan_routes", "live_route"], ["circuit_plan"], ["s6"], "Circuits tie luminaires to the DB in the model."),
        _step("s9", "Validate and export the drawing package", "output", ["validate_drawing", "export_package"], ["lighting_requirements"], ["s7", "s8"], "Draft package with a manifest and validation record."),
    ]
    return inputs, steps


def _t_load_and_max_demand() -> tuple[list, list]:
    inputs = [
        _inp("load_schedule", "Load schedule workbook", "All connected loads per floor/board: lighting, sockets, AC, pumps, lifts, fire, services.", "workbook", "workbook_create kind=load_schedule, fill it in Excel, save, then workbook_read.", "load_schedule"),
        _inp("demand_factors", "Demand/diversity factors and their source", "Maximum demand = connected load x factors. They must come from your approved method.", "value", "Fill the Demand factor column per row (0-1) and name the source (regulation table, tutorial, lecturer). Leave blank if unknown: it will be listed as missing, not assumed."),
        _inp("essential_loads", "Which loads are essential (run on the generator)", "Needed for generator sizing.", "decision", "Mark the Essential column Y/N per row."),
        _inp("pump_data", "Pump data (2 x 100 kW): starting method, efficiency, power factor", "Motor starting current drives generator and cable sizing.", "value", "Starting method (DOL/star-delta/soft/VFD), efficiency, PF, duty/standby arrangement."),
    ]
    steps = [
        _step("l1", "Create the load schedule workbook", "input", ["workbook_create"], [], [], "Produces load_schedule.xlsx with instructions, dropdowns and formulas."),
        _step("l2", "Fill in loads (you, in Excel)", "input", [], ["load_schedule"], ["l1"], "Add every load. Take lighting from lighting_schedule and socket/AC counts from the drawing.", False),
        _step("l3", "Check the workbook", "review", ["workbook_read"], ["load_schedule"], ["l2"], "Lists missing required fields and invalid values."),
        _step("l4", "Connected load and maximum demand", "calc", ["load_summary"], ["load_schedule", "demand_factors"], ["l3"], "Sums connected kW and demand kW by floor, board and category using YOUR factors."),
        _step("l5", "Essential load total for generator selection", "calc", ["load_summary"], ["essential_loads", "pump_data"], ["l4"], "Essential connected/demand load. Generator and transformer selection itself is not automated.", False),
        _step("l6", "Review results with the user and record decisions", "review", ["plan_update_step", "plan_note"], [], ["l4"], "Record assumptions and sources before using the numbers downstream."),
    ]
    return inputs, steps


def _t_cable_and_protection() -> tuple[list, list]:
    inputs = [
        _inp("cable_inputs", "Cable and protection inputs workbook", "Per circuit: design current, length, installation method, standard-table values.", "workbook", "workbook_create kind=cable_protection_inputs; fill in Excel.", "cable_protection_inputs"),
        _inp("standard_tables", "Values from your design standard", "Tabulated current capacity, correction factors, mV/A/m: copied from the standard you use.", "value", "Enter them in the sheet with the table/clause named in the Source column. They are never filled in for you."),
        _inp("max_demand", "Maximum demand result", "Main cables and breakers follow from it.", "value", "Result of the load_and_max_demand plan."),
    ]
    steps = [
        _step("c1", "Create the cable/protection workbook", "input", ["workbook_create"], [], [], "Sheet with design-current, capacity, correction factor, voltage-drop and device columns."),
        _step("c2", "Fill in circuits and table values (you, in Excel)", "input", [], ["cable_inputs", "standard_tables"], ["c1"], "Excel formulas in the sheet show Ib <= In <= Iz and voltage drop for each row.", False),
        _step("c3", "Check the workbook", "review", ["workbook_read"], ["cable_inputs"], ["c2"], "Structure and number checks only."),
        _step("c4", "Cable sizing and protection coordination checks", "calc", [], ["max_demand"], ["c3"], "Not automated yet: use the workbook formulas and have the results reviewed. A calculation tool can be added once the method and tables are agreed.", False),
    ]
    return inputs, steps


def _t_drawing_from_reference() -> tuple[list, list]:
    inputs = [
        _inp("arch_dxf", "Architectural drawing of the floor (DXF/DWG)", "Base drawing, never modified.", "file", "Give the file path."),
        _inp("room_list", "Room list with approved ids and names", "Rooms drive placement and checks.", "table", "Room id, name, bounds; or confirm those read from the drawing."),
        _inp("placement_spec", "What to place and where", "Devices need positions, rooms and circuits.", "decision", "Counts/positions per room, or a reference drawing to extract them from (live_adopt/live_scan)."),
    ]
    steps = [
        _step("d1", "Register the reference and prepare the floor model", "drawing", ["register_reference", "prepare_floor_model"], ["arch_dxf", "room_list"], [], "Check units (headers can be wrong) and room names."),
        _step("d2", "Place devices (changesets, previewed)", "drawing", ["plan_devices", "propose_lighting_grid", "preview_changes", "apply_changes"], ["placement_spec"], ["d1"], "Or work live in the open drawing with the live_* tools."),
        _step("d3", "Circuits and routes", "drawing", ["set_circuit_assignment", "plan_routes"], [], ["d2"], "Routes are tied to circuits and DBs in the model."),
        _step("d4", "Validate and export", "output", ["validate_drawing", "export_package"], [], ["d3"], "Draft package with manifest."),
    ]
    return inputs, steps


def _t_sld_and_boq() -> tuple[list, list]:
    inputs = [
        _inp("design_results", "Approved results: demand, transformer/generator, cables, protection", "The SLD shows ratings and sizes; the BOQ prices them.", "value", "Results from the earlier plans, reviewed."),
        _inp("unit_costs", "Unit costs for the BOQ", "Total cost = quantity x unit cost.", "workbook", "Supply prices; no prices are assumed."),
    ]
    steps = [
        _step("b1", "Quantities from the drawing model", "output", ["list_devices", "lighting_schedule"], [], [], "Device counts per floor are available from the model."),
        _step("b2", "Single line diagram", "drawing", [], ["design_results"], [], "Not built yet.", False),
        _step("b3", "BOQ and cost estimate", "output", [], ["design_results", "unit_costs"], ["b1"], "Not built yet.", False),
    ]
    return inputs, steps


def _t_full_project() -> tuple[list, list]:
    names = [
        ("f1", "1. Understand the building (architectural CAD)", "drawing_from_reference"),
        ("f2", "2. Load identification", "load_and_max_demand"),
        ("f3", "3. Lighting design (DIALux)", "lighting_from_dialux"),
        ("f4", "4. Socket outlet design", "drawing_from_reference"),
        ("f5", "5. Maximum demand", "load_and_max_demand"),
        ("f6", "6-7. Transformer and generator selection", "load_and_max_demand"),
        ("f8", "8. Three-phase distribution design", "cable_and_protection"),
        ("f9", "9-10. Cable sizing and protection", "cable_and_protection"),
        ("f11", "11. Single line diagram", "sld_and_boq"),
        ("f12", "12. BOQ and cost estimate", "sld_and_boq"),
        ("f13", "13. Final report", None),
    ]
    steps = []
    for sid, title, sub in names:
        built = sid not in ("f6", "f8", "f13")
        guide = (f"Run as its own plan: plan_define template='{sub}'." if sub else
                 "Assemble from the outputs of the other steps (not automated).")
        steps.append(_step(sid, title, "review", [], [], [], guide, built))
    return [], steps


def _t_floor_set_workflow() -> tuple[list, list]:
    inputs = [
        _inp("requirement", "Requirement document and work plan", "Defines scope, supply, special loads and the deliverables.", "file", "Give the file paths (PDF/Markdown)."),
        _inp("reference_material", "Reference reports, tutorials and sample tables", "Method, table structure, naming and style to follow.", "file", "Give the folder or file paths; say which one is master when two disagree."),
        _inp("base_drawings", "Base architectural drawings per floor, with their role", "Never edited: every floor is drawn in its own copy. Combined-room floors may use a different base.", "file", "Floor to file, and which floors share a base (for example ground and top floor combined)."),
        _inp("room_plan", "Room plan (image or table) with approved ids and names", "Rooms drive placement, areas and checks.", "table", "Room id, name and which floor; flag rooms that span two floors."),
        _inp("dialux_exports", "DIALux export per floor", "Source of luminaire positions and wattage.", "file", "Final exports only; say if a floor was re-exported."),
        _inp("load_workbooks", "A/C and socket workbooks (counts per room)", "Decide what is placed in each room; no template copied between rooms.", "workbook", "placement_table sheet per floor, or your own workbooks mapped to it.", "placement_table"),
        _inp("label_style", "Label and legend style", "Short names with the floor in them, a key box, plain style.", "decision", "Approve the style on the first floor; it is then applied everywhere."),
        _inp("demand_method", "Demand and diversity method", "Maximum demand and sizing follow your method, not a default.", "value", "Simultaneity table / factors and their source."),
    ]
    steps = [
        _step("t0", "S0 Intake: inventory every input", "review", ["intake_scan", "base_register"], [], [], "Hashes, units, extents, floors present and missing, duplicates, scale mismatches between floors. Gate: anything missing or unclear?"),
        _step("t1", "S1 Plan and questions (hard gate)", "review", ["plan_define", "plan_approve"], ["requirement", "reference_material"], ["t0"], "Short questions with a default each; answer 'defaults' or by letter. Nothing is drawn before this is approved."),
        _step("t2", "S2 Base preparation per floor", "drawing", ["floor_from_base", "dxf_sanitize", "live_sanitize", "prepare_floor_model"], ["base_drawings", "room_plan"], ["t1"], "Copy the base, strip heavy layers, fix units and scale, set rooms. Gate: room names, combined rooms, unit conflicts."),
        _step("t3", "S3 Lighting import from DIALux", "drawing", ["dialux_inspect", "dialux_align", "dialux_import", "live_import_luminaires"], ["dialux_exports"], ["t2"], "Counts and watts must equal the DIALux luminaire list. Gate: circuit grouping and DB position."),
        _step("t4", "S4 Loads from the workbooks (A/C, sockets), floor by floor", "drawing", ["place_from_table", "live_place_from_table", "plan_approve_floor", "plan_confirm_floor"], ["load_workbooks"], ["t2"], "Draw the first floor, show it, wait for confirmation, then the next. Never one floor ahead."),
        _step("t5", "S5 Routing and circuit lengths", "calc", ["plan_routes", "live_route", "export_circuit_lengths", "live_circuit_lengths"], [], ["t4"], "Every circuit gets a route and a length; lengths go into a workbook range for cable sizing."),
        _step("t6", "S6 Labels, legend and key", "drawing", ["live_add_text"], ["label_style"], ["t4"], "Legend and key box block are not automated yet.", False),
        _step("t7", "S7 Verify against the workbooks", "review", ["verify_placement", "live_verify_placement", "validate_drawing"], [], ["t5"], "Counts per room and type match the workbooks; deviations are listed with a reason."),
        _step("t8", "S8 Calculation handoff", "calc", ["export_circuit_lengths", "load_summary"], ["demand_method"], ["t7"], "Maximum demand from your factors is automated; cable sizing, transformer/generator and short circuit are not.", False),
        _step("t9", "S9 Single line diagrams", "drawing", [], [], ["t8"], "Not built yet.", False),
        _step("t10", "S10 Export and plot", "output", ["export_package", "get_preview_image"], [], ["t7"], "DXF/PNG/PDF with a manifest. Batch plotting of open drawings is not automated yet."),
        _step("t11", "S11 Quantities for the BOQ and report", "output", ["export_quantities"], [], ["t7"], "Counts per type and floor as a named range; BOQ prices are not automated."),
    ]
    return inputs, steps


TEMPLATES: dict[str, dict[str, Any]] = {
    "lighting_from_dialux": {"title": "Lighting layout from DIALux", "build": _t_lighting_from_dialux,
        "summary": "Import a DIALux layout for a floor, total the lighting load, assign circuits, export."},
    "load_and_max_demand": {"title": "Load schedule and maximum demand", "build": _t_load_and_max_demand,
        "summary": "Collect loads in Excel, check them, compute connected load and maximum demand with your factors."},
    "cable_and_protection": {"title": "Cable sizing and protection inputs", "build": _t_cable_and_protection,
        "summary": "Structured Excel inputs for cable and protection checks (calculation tool not built yet)."},
    "drawing_from_reference": {"title": "Floor drawing from a reference", "build": _t_drawing_from_reference,
        "summary": "Prepare a floor model, place devices, circuits and routes, validate and export."},
    "sld_and_boq": {"title": "Single line diagram and BOQ", "build": _t_sld_and_boq,
        "summary": "Quantities from the model; SLD and BOQ generation are not built yet."},
    "floor_set_workflow": {"title": "Multi-floor project workflow (intake to quantities)", "build": _t_floor_set_workflow,
        "summary": "Stages S0-S11 with approval gates: intake, plan, base preparation, lighting, loads floor by floor, routes and lengths, verify, export."},
    "full_project": {"title": "Electrical installation project (whole work plan)", "build": _t_full_project,
        "summary": "The 13-part work plan as a roadmap; each part runs as its own plan."},
}


def list_templates() -> list[dict[str, Any]]:
    out = []
    for key, t in TEMPLATES.items():
        inputs, steps = t["build"]()
        out.append({"template": key, "title": t["title"], "summary": t["summary"],
                    "steps": len(steps), "inputs_needed": len(inputs),
                    "not_built": [s["title"] for s in steps if not s["built"]]})
    return out


# --------------------------------------------------------------------------- store
class PlanStore:
    def __init__(self, workspace: Path):
        self.root = workspace

    def path(self, project: str) -> Path:
        return self.root / safe_name(project, "project") / "plan.json"

    def exists(self, project: str) -> bool:
        return self.path(project).exists()

    def load(self, project: str) -> dict[str, Any]:
        p = self.path(project)
        if not p.exists():
            raise PlanError(f"No plan for project {project!r}. Define one with plan_define "
                            "(see plan_templates).")
        return json.loads(p.read_text())

    def save(self, plan: dict[str, Any]) -> None:
        plan["updated"] = _now()
        p = self.path(plan["project"])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(plan, indent=2))
        tmp.replace(p)

    def set_active(self, project: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".active_plan").write_text(project)

    def active(self) -> str | None:
        f = self.root / ".active_plan"
        return f.read_text().strip() if f.exists() else None


def _log(plan: dict, text: str) -> None:
    plan["log"].append({"at": _now(), "event": text})
    plan["log"] = plan["log"][-200:]


def define(project: str, title: str, goal: str, success_criteria: list[str],
           scope_in: list[str] | None = None, scope_out: list[str] | None = None,
           template: str | None = None, floors: list[str] | None = None) -> dict[str, Any]:
    safe_name(project, "project")
    if not title.strip() or not goal.strip():
        raise PlanError("A plan needs a title and a goal statement.")
    if not success_criteria:
        raise PlanError("Give at least one success criterion: how will we know it is done?")
    inputs, steps = [], []
    if template:
        if template not in TEMPLATES:
            raise PlanError(f"Unknown template {template!r}; options: {sorted(TEMPLATES)}")
        inputs, steps = TEMPLATES[template]["build"]()
    plan = {"project": project, "title": title, "template": template, "created": _now(),
            "updated": _now(), "status": "draft",
            "goal": {"statement": goal, "success_criteria": success_criteria,
                     "scope_in": scope_in or [], "scope_out": scope_out or [],
                     "approved": False, "approval_note": None},
            "floors": {safe_name(f, "floor"): {"approved": False, "confirmed": False, "note": None}
                       for f in floors or []},
            "inputs": inputs, "steps": steps, "assumptions": [], "log": []}
    _log(plan, f"Plan defined from template {template or 'none'}; waiting for the goal to be approved.")
    return plan


def _input(plan: dict, iid: str) -> dict:
    for i in plan["inputs"]:
        if i["id"] == iid:
            return i
    raise PlanError(f"Unknown input {iid!r}. Inputs: {[i['id'] for i in plan['inputs']]}")


def _step_obj(plan: dict, sid: str) -> dict:
    for s in plan["steps"]:
        if s["id"] == sid:
            return s
    raise PlanError(f"Unknown step {sid!r}. Steps: {[s['id'] for s in plan['steps']]}")


def _missing_inputs(plan: dict, step: dict) -> list[dict]:
    return [i for i in (_input(plan, n) for n in step["needs"]) if i["status"] == "missing"]


def step_state(plan: dict, step: dict) -> dict[str, Any]:
    if step["status"] in _DONE:
        return {"state": step["status"]}
    waiting = [s["id"] for s in (_step_obj(plan, d) for d in step["depends_on"])
               if s["status"] not in _DONE]
    missing = _missing_inputs(plan, step)
    if waiting or missing:
        return {"state": "blocked", "waiting_on_steps": waiting,
                "missing_inputs": [i["id"] for i in missing]}
    return {"state": "ready" if step["status"] == "pending" else step["status"]}


def update_step(plan: dict, step_id: str, status: str, note: str | None = None,
                result: str | None = None, force: bool = False) -> dict:
    if status not in STEP_STATUS:
        raise PlanError(f"status must be one of {STEP_STATUS}")
    step = _step_obj(plan, step_id)
    if not plan["goal"]["approved"] and status in ("in_progress", "done") and not force:
        raise PlanError("The goal is not approved yet. Show the plan to the user and call "
                        "plan_approve once they agree.")
    if status in ("in_progress", "done") and not force:
        st = step_state(plan, step)
        if st["state"] == "blocked":
            raise PlanError(f"Step {step_id} is blocked: {st}. Provide the missing inputs / finish "
                            "the earlier steps, or pass force=true with a note explaining why.")
        if status == "done" and not (result or step.get("result")):
            raise PlanError("Record a result when finishing a step: what was produced or decided "
                            "(file paths, counts, key numbers).")
    step["status"] = status
    if note:
        step["note"] = note
    if result:
        step["result"] = result
    _log(plan, f"Step {step_id} -> {status}" + (f": {note}" if note else ""))
    if all(s["status"] in _DONE for s in plan["steps"]) and plan["steps"]:
        plan["status"] = "complete"
        _log(plan, "All steps finished.")
    elif plan["status"] == "complete":
        plan["status"] = "approved"
    return step


def provide_input(plan: dict, input_id: str, reference: str | None, note: str | None,
                  not_applicable: bool = False) -> dict:
    inp = _input(plan, input_id)
    if not_applicable:
        inp["status"], inp["note"] = "not_applicable", note or "not applicable"
    else:
        if not reference:
            raise PlanError("Say what was provided: a file path, a value, or a short description "
                            "of the decision.")
        inp["status"], inp["reference"], inp["note"] = "provided", reference, note
    _log(plan, f"Input {input_id} -> {inp['status']}" + (f" ({reference})" if reference else ""))
    return inp


def add_step(plan: dict, title: str, kind: str, tools: list[str] | None, needs: list[str] | None,
             depends_on: list[str] | None, guidance: str | None) -> dict:
    if kind not in STEP_KINDS:
        raise PlanError(f"kind must be one of {STEP_KINDS}")
    for n in needs or []:
        _input(plan, n)
    for d in depends_on or []:
        _step_obj(plan, d)
    n = 1
    while any(s["id"] == f"x{n}" for s in plan["steps"]):
        n += 1
    s = _step(f"x{n}", title, kind, tools or [], needs or [], depends_on or [], guidance or "")
    plan["steps"].append(s)
    _log(plan, f"Step {s['id']} added: {title}")
    return s


def add_input(plan: dict, input_id: str, title: str, why: str, kind: str, how: str,
              workbook: str | None) -> dict:
    if any(i["id"] == input_id for i in plan["inputs"]):
        raise PlanError(f"Input {input_id!r} already exists.")
    i = _inp(input_id, title, why, kind, how, workbook)
    plan["inputs"].append(i)
    _log(plan, f"Input {input_id} added: {title}")
    return i


def note(plan: dict, text: str, kind: str, source: str | None) -> dict:
    n = {"at": _now(), "kind": kind, "text": text, "source": source}
    plan["assumptions"].append(n)
    _log(plan, f"{kind.capitalize()} recorded: {text[:80]}")
    return n


def next_actions(plan: dict) -> dict[str, Any]:
    ready, blocked, ask = [], [], {}
    for s in plan["steps"]:
        st = step_state(plan, s)
        if st["state"] in ("ready", "in_progress"):
            ready.append({"id": s["id"], "title": s["title"], "tools": s["tools"],
                          "built": s["built"], "guidance": s["guidance"]})
        elif st["state"] == "blocked":
            blocked.append({"id": s["id"], "title": s["title"], **{k: v for k, v in st.items()
                                                                  if k != "state"}})
            for iid in st.get("missing_inputs", []):
                i = _input(plan, iid)
                ask[iid] = {"id": iid, "title": i["title"], "why": i["why"], "how": i["how"],
                            "kind": i["kind"], "workbook_template": i["workbook"],
                            "needed_by": [x["id"] for x in plan["steps"] if iid in x["needs"]]}
    return {"ready_now": ready, "blocked": blocked, "needed_from_user": list(ask.values())}


def render_markdown(plan: dict) -> str:
    g = plan["goal"]
    done = sum(s["status"] in _DONE for s in plan["steps"])
    lines = [f"# {plan['title']}  ({plan['status']})", "", f"**Goal:** {g['statement']}", "",
             "**Done when:**"] + [f"- {c}" for c in g["success_criteria"]]
    if g["scope_in"]:
        lines += ["", "**In scope:** " + "; ".join(g["scope_in"])]
    if g["scope_out"]:
        lines += ["**Out of scope:** " + "; ".join(g["scope_out"])]
    lines += ["", f"**Progress:** {done}/{len(plan['steps'])} steps", ""]
    for s in plan["steps"]:
        st = step_state(plan, s)
        box = "x" if s["status"] == "done" else ("-" if s["status"] == "skipped" else " ")
        extra = ""
        if st["state"] == "blocked":
            bits = [f"after {', '.join(st['waiting_on_steps'])}" if st["waiting_on_steps"] else "",
                    f"needs {', '.join(st['missing_inputs'])}" if st["missing_inputs"] else ""]
            extra = " (waiting: " + "; ".join(b for b in bits if b) + ")"
        elif st["state"] == "in_progress":
            extra = " (in progress)"
        if not s["built"]:
            extra += " [not automated yet]"
        lines.append(f"- [{box}] {s['id']}. {s['title']}{extra}")
    fl = plan.get("floors") or {}
    if fl:
        lines += ["", "**Floors:** " + "; ".join(
            f"{f} {'confirmed' if v['confirmed'] else ('OPEN, awaiting your check' if v['approved'] else 'not started')}"
            for f, v in fl.items())]
    miss = [i for i in plan["inputs"] if i["status"] == "missing"]
    if miss:
        lines += ["", "**Needed from you:**"] + [f"- {i['title']} ({i['id']}): {i['how']}" for i in miss]
    return "\n".join(lines)


def summary(plan: dict) -> dict[str, Any]:
    return {"project": plan["project"], "title": plan["title"], "status": plan["status"],
            "approved": plan["goal"]["approved"], "goal": plan["goal"],
            "progress": {"done": sum(s["status"] in _DONE for s in plan["steps"]),
                         "total": len(plan["steps"])},
            "steps": [{**{k: s[k] for k in ("id", "title", "kind", "status", "tools", "built",
                                            "note", "result")}, **step_state(plan, s)}
                      for s in plan["steps"]],
            "inputs": [{k: i[k] for k in ("id", "title", "kind", "status", "reference", "workbook")}
                       for i in plan["inputs"]],
            "assumptions": plan["assumptions"], "next": next_actions(plan),
            "floors": plan.get("floors", {}), "still_open": still_open(plan),
            "checklist": render_markdown(plan), "updated": plan["updated"]}


# --------------------------------------------------------------------------- floor scopes and gates
def approve_floor(plan: dict, floor: str, note: str | None = None, force: bool = False) -> dict:
    """Allow drawing on one floor. Only one floor may be open at a time: the previous floor must
    be confirmed by the user first (force=true overrides, e.g. to work in parallel)."""
    if not plan["goal"]["approved"]:
        raise PlanError("The goal is not approved yet. Approve the goal first (plan_approve).")
    floors = plan.setdefault("floors", {})
    if floor not in floors:
        floors[floor] = {"approved": False, "confirmed": False, "note": None}
    open_ = [f for f, v in floors.items() if v["approved"] and not v["confirmed"] and f != floor]
    if open_ and not force:
        raise PlanError(f"Floor {open_[0]} is still open: ask the user to check it, then call "
                        "plan_confirm_floor. One floor at a time (force=true to work in parallel).")
    floors[floor].update(approved=True, confirmed=False, note=note)
    _log(plan, f"Floor {floor} approved for drawing" + (f": {note}" if note else ""))
    return floors[floor]


def confirm_floor(plan: dict, floor: str, note: str | None = None) -> dict:
    f = plan.setdefault("floors", {}).get(floor)
    if not f or not f["approved"]:
        raise PlanError(f"Floor {floor!r} was never approved for drawing.")
    f.update(confirmed=True, note=note or f.get("note"))
    _log(plan, f"Floor {floor} confirmed by the user" + (f": {note}" if note else ""))
    return f


def active_floor(plan: dict) -> str | None:
    open_ = [f for f, v in plan.get("floors", {}).items() if v["approved"] and not v["confirmed"]]
    return open_[0] if len(open_) == 1 else None


def gate_check(plan: dict | None, floor: str | None = None) -> str | None:
    """None when drawing is allowed, otherwise the reason it is refused."""
    if plan is None or not plan["goal"]["approved"]:
        return ("no approved plan. Define the goal with plan_define, show it to the user and call "
                "plan_approve after they agree.")
    floors = plan.get("floors") or {}
    if not floors:
        return None
    if floor is not None:
        f = floors.get(floor)
        if not f or not f["approved"]:
            return (f"floor {floor} is not approved for drawing. Ask the user, then call "
                    "plan_approve_floor.")
        if f["confirmed"]:
            return (f"floor {floor} was already confirmed by the user. Re-open it with "
                    "plan_approve_floor if it must change.")
        return None
    open_ = [f for f, v in floors.items() if v["approved"] and not v["confirmed"]]
    if len(open_) == 1:
        return None
    if not open_:
        return ("no floor is approved for drawing. Ask the user which floor to start with, then "
                "call plan_approve_floor.")
    return f"several floors are open ({', '.join(open_)}): say which one you are drawing."


def add_open_item(plan: dict, text: str) -> dict:
    return note(plan, text, "open", None)


def resolve_open_item(plan: dict, index: int, how: str | None = None) -> dict:
    items = plan["assumptions"]
    if not 0 <= index < len(items) or items[index]["kind"] != "open":
        raise PlanError("No open item with that number. See still_open in plan_show.")
    items[index]["resolved"] = how or "done"
    _log(plan, f"Open item {index} resolved: {items[index]['text'][:60]}")
    return items[index]


def still_open(plan: dict) -> dict[str, Any]:
    return {
        "open_items": [{"number": i, "text": n["text"]} for i, n in enumerate(plan["assumptions"])
                       if n["kind"] == "open" and not n.get("resolved")],
        "unfinished_steps": [s["id"] for s in plan["steps"] if s["status"] not in _DONE],
        "missing_inputs": [i["id"] for i in plan["inputs"] if i["status"] == "missing"],
        "floors_awaiting_confirmation": [f for f, v in plan.get("floors", {}).items()
                                         if v["approved"] and not v["confirmed"]],
    }


# --------------------------------------------------------------------------- change impact
IMPACT_EDGES: dict[str, list[str]] = {
    "architecture": ["rooms", "drawings"], "rooms": ["drawings", "lighting_schedule"],
    "dialux_export": ["luminaires"], "luminaires": ["drawings", "loads", "circuits"],
    "sockets": ["drawings", "loads", "circuits"], "ac_units": ["drawings", "loads", "circuits"],
    "circuits": ["routes", "drawings", "phase_balance"], "routes": ["cable_lengths", "drawings"],
    "cable_lengths": ["cable_sizes"], "load_schedule": ["loads"], "loads": ["demand", "phase_balance"],
    "demand_factors": ["demand"], "demand": ["transformer", "generator", "cable_sizes"],
    "phase_balance": ["cable_sizes"], "transformer": ["short_circuit", "sld", "boq"],
    "generator": ["sld", "boq"], "cable_sizes": ["protection", "short_circuit", "sld", "boq"],
    "protection": ["sld"], "short_circuit": ["sld", "report"], "sld": ["report"],
    "drawings": ["quantities"], "quantities": ["boq"], "boq": ["report"],
    "lighting_schedule": ["loads"],
}
IMPACT_LABELS = {
    "rooms": "room list and areas", "drawings": "CAD drawings", "luminaires": "luminaire list",
    "loads": "load schedule", "circuits": "circuit list", "routes": "routes", "cable_lengths": "circuit lengths",
    "cable_sizes": "cable sizes", "demand": "maximum demand", "phase_balance": "phase balance",
    "transformer": "transformer selection", "generator": "generator selection", "protection": "protection devices",
    "short_circuit": "short-circuit calculation", "sld": "single line diagrams", "quantities": "quantities",
    "boq": "BOQ", "report": "report", "lighting_schedule": "lighting schedule",
}


def impact_of(change: str) -> dict[str, Any]:
    key = change.strip().lower().replace(" ", "_").replace("-", "_")
    if key not in IMPACT_EDGES and key not in IMPACT_LABELS:
        raise PlanError(f"Unknown change {change!r}. Known: {sorted(set(IMPACT_EDGES) | set(IMPACT_LABELS))}")
    seen, queue = [], [key]
    while queue:
        for nxt in IMPACT_EDGES.get(queue.pop(0), []):
            if nxt not in seen and nxt != key:
                seen.append(nxt)
                queue.append(nxt)
    labels = [IMPACT_LABELS.get(x, x) for x in seen]
    return {"change": key, "affected": labels,
            "cad_drawings_affected": "drawings" in seen,
            "note": ("Tell the user this list before changing anything, and say which of these "
                     "outputs must be regenerated.")}
