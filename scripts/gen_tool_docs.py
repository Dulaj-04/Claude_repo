"""Generate docs/tools.md from the tools the server really registers.

    python scripts/gen_tool_docs.py          # rewrite docs/tools.md
    python scripts/gen_tool_docs.py --check  # exit 1 if docs/tools.md is out of date (used in CI)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "tools.md"

GROUPS: list[tuple[str, str, list[str]]] = [
    ("Plan-first workflow",
     "Define a goal for large requests, track steps and the inputs needed from you. Guide: "
     "docs/planning.md.",
     ["plan_templates", "plan_define", "plan_show", "plan_next", "plan_approve", "plan_update_step",
      "plan_provide_input", "plan_add_step", "plan_add_input", "plan_note"]),
    ("Excel inputs and calculations",
     "Excel sheets for you to fill in, a validating reader, and the load summary.",
     ["workbook_create", "workbook_read", "load_summary"]),
    ("Inspect", "Read drawings and models. Nothing is changed.",
     ["register_reference", "inspect_drawing", "list_devices", "list_floors", "get_preview_image"]),
    ("Prepare", "Create the floor model and set the drawing template.",
     ["prepare_floor_model", "prepare_template"]),
    ("Plan", "Propose devices, circuits and routes. Each call returns a changeset; nothing is drawn "
     "until `apply_changes`.",
     ["plan_devices", "propose_lighting_grid", "set_circuit_assignment", "plan_routes"]),
    ("Apply, undo, sync", "Guarded writes with undo and read-back.",
     ["preview_changes", "apply_changes", "undo_last", "sync_from_drawing"]),
    ("DIALux import and lighting schedule",
     "Bring a DIALux lighting layout into the floor model and total the lighting load. "
     "Guide: docs/dialux.md.",
     ["dialux_inspect", "dialux_align", "import_luminaire_list", "dialux_import",
      "lighting_schedule"]),
    ("Output", "Generate the drawing views and the export package.",
     ["generate_views", "export_package", "validate_drawing"]),
    ("Live editing (plan symbols)",
     "Windows + AutoCAD / AutoCAD Electrical running. These edit the **open drawing directly**.",
     ["live_connect", "live_selftest", "live_scan", "live_adopt", "live_list_devices", "live_texts",
      "live_place", "live_import_luminaires", "live_move", "live_delete", "live_assign", "live_route", "live_add_text",
      "live_add_polyline", "live_zoom", "live_undo", "live_save"]),
    ("Mode switch", "Choose between plan-symbol tools and AutoCAD Electrical schematic tools.",
     ["live_set_mode"]),
    ("Schematic mode (AutoCAD Electrical)",
     "Read and probe only for now. Enabled with `live_set_mode('schematic')`.",
     ["sch_detect", "sch_check_commands", "sch_modules", "sch_read", "sch_probe", "sch_run_lisp"]),
    ("AutoCAD utilities (Windows)", "Direct AutoCAD COM helpers.",
     ["acad_status", "acad_open", "acad_run_command"]),
]


def _type(schema: dict) -> str:
    if "enum" in schema:
        return " \\| ".join(f"`{v}`" for v in schema["enum"])
    if "anyOf" in schema:
        parts = [_type(s) for s in schema["anyOf"] if s.get("type") != "null"]
        return " or ".join(parts) or "any"
    t = schema.get("type", "any")
    if t == "array":
        return f"list of {_type(schema.get('items', {}))}"
    return t


def render() -> str:
    from acad_electrical_mcp import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    listed = [n for _, _, names in GROUPS for n in names]
    missing = sorted(set(tools) - set(listed))
    unknown = sorted(set(listed) - set(tools))
    if missing or unknown:
        raise SystemExit(f"scripts/gen_tool_docs.py GROUPS out of date. Missing: {missing} "
                         f"Unknown: {unknown}")
    out = ["# Tool reference", "",
           f"All {len(tools)} MCP tools, generated from the running server "
           "(`python scripts/gen_tool_docs.py`). Do not edit by hand.", "",
           "Floor tools take `project` and `floor` (letters, digits, `_`, `-`). Coordinates are in "
           "the drawing's units.", "", "| Group | Tools |", "| --- | --- |"]
    for title, _, names in GROUPS:
        out.append(f"| {title} | {', '.join(f'`{n}`' for n in names)} |")
    out.append("")
    for title, blurb, names in GROUPS:
        out += [f"## {title}", "", blurb, ""]
        for n in names:
            t = tools[n]
            props = t.inputSchema.get("properties", {})
            req = set(t.inputSchema.get("required", []))
            out += [f"### `{n}`", "", " ".join((t.description or "").split()), ""]
            if props:
                out += ["| Parameter | Type | Required | Default |", "| --- | --- | --- | --- |"]
                for p, s in props.items():
                    dflt = "" if p in req else (f"`{s['default']}`" if "default" in s else "none")
                    out.append(f"| `{p}` | {_type(s)} | {'yes' if p in req else 'no'} | {dflt} |")
                out.append("")
            else:
                out += ["_No parameters._", ""]
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/tools.md is out of date. Run: python scripts/gen_tool_docs.py")
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
