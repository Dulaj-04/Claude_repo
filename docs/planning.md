# Plan-first workflow

For large requests the assistant works from a **plan**: a goal, success criteria, ordered steps, and a list of the inputs it needs from you. This is configured in the MCP server itself, so it applies in every client (Claude Desktop, Claude Code, ChatGPT connector).

## What the server does (and what it cannot)
- **Instructions (always on):** the server tells the assistant to check for a plan, define a goal for large requests, wait for your approval, work step by step, keep the plan updated, and end each reply with progress and what it needs from you.
- **Plan tools:** the plan is stored on disk per project (`<workspace>/<project>/plan.json`), so any session picks up where the last one stopped. A step can only be started or finished when its inputs are provided and earlier steps are done; finishing needs a recorded result.
- **Prompts:** `plan_a_request`, `lighting_from_dialux`, `load_schedule_and_demand`, `project_roadmap` (look for them in your client's prompt/"+" menu).
- **Strict mode (optional):** start the server with `ACAD_MCP_REQUIRE_PLAN=1` and the tools that change drawings (`apply_changes`, and all `live_*` tools that draw or edit) refuse until a plan goal is defined **and approved**. Reading, inspecting and dry runs stay allowed.
- **What it cannot do:** a server cannot force a model to follow instructions. The instructions and plan tools make the behaviour the default and visible; strict mode makes the drawing-changing tools enforce it.

## The loop
1. You ask for something large. The assistant calls `plan_show` (existing plan?) then `plan_define`: goal, success criteria, scope in/out, from a template if one fits (`plan_templates`).
2. It shows you the goal and the inputs it will need, and asks if the goal is right. **You approve** (`plan_approve`; it may only call this after you said yes).
3. `plan_next` lists what is ready and **exactly what it needs from you**, each with why it is needed and how to supply it (file path, value, decision, or an Excel sheet).
4. As you supply things it records them (`plan_provide_input`); as steps finish it records the result (`plan_update_step`); assumptions and decisions with their source go in `plan_note`.
5. Every reply ends with the progress and the next ask.

## Plan templates
| Template | For |
| --- | --- |
| `lighting_from_dialux` | DIALux layout for a floor: inspect, align, luminaire list, import, schedule, circuits, validate, export |
| `load_and_max_demand` | Load schedule in Excel, check, connected load and maximum demand with your factors |
| `cable_and_protection` | Cable and protection inputs in Excel (the calculation tool is **not built yet**) |
| `drawing_from_reference` | Floor model, devices, circuits, routes, validate, export |
| `sld_and_boq` | Quantities from the model (SLD and BOQ generation are **not built yet**) |
| `full_project` | Your 13-part work plan as a roadmap; each part runs as its own plan |

Steps that are not automated yet are marked `[not automated yet]` in the checklist so the plan never pretends otherwise.

## Excel input sheets
`workbook_create` writes a sheet to `<workspace>/<project>/inputs/` with an Instructions tab and a Data tab (required columns marked `*`, dropdowns, grey formula columns). Fill in the **Data** tab and save.

| Sheet | Purpose |
| --- | --- |
| `load_schedule` | Every load per floor/board: qty, unit kW, power factor, demand factor, phases, essential Y/N, source |
| `lighting_requirements` | Target lux per room and where the requirement comes from |
| `luminaire_list` | Wattage per luminaire type for the DIALux import |
| `cable_protection_inputs` | Per circuit: design current, length, method, table values from your standard, protective device |

`workbook_read` checks a filled sheet and reports **row and column** for every problem (missing required value, text where a number is expected, factor outside 0-1, unknown category, duplicate ids). It reads the input columns and does the arithmetic itself, because formulas written by software have no stored result until Excel saves the file.

`load_summary` then totals connected kW and demand kW by floor, board and category (plus essential loads). **Only factors you entered are used**: a row without a demand factor is listed and left out of the demand total, and the result says it is incomplete. Power factor and table values are likewise never filled in for you.

## Example
> I need the load schedule and maximum demand for the building. Plan it first.

The assistant defines the goal ("connected load and maximum demand per floor and board, from approved factors"), picks `load_and_max_demand`, shows you the inputs (load schedule, demand factors and their source, essential loads, pump data), creates `load_schedule.xlsx`, tells you what to fill in, checks it, runs `load_summary`, and records your decisions and sources in the plan.
