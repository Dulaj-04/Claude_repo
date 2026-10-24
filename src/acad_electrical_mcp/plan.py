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
        _step("s1", "Prepare the floor model (rooms + architectural reference)", "drawing", ["register_reference", "prepare_floor_model"], ["arch_dxf", "room_list"], [], "Registers the reference file and rooms; differences from approved names are reported, not overwritten."),
        _step("s2", "Inspect the DIALux export", "review", ["dialux_inspect"], ["dialux_export"], [], "Lists block names, counts, layers and units. Decide which blocks are luminaires / emergency signs."),
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
           template: str | None = None) -> dict[str, Any]:
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
            "checklist": render_markdown(plan), "updated": plan["updated"]}
