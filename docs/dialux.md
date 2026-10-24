# Importing a DIALux lighting layout

Brings the luminaires from a DIALux **DWG/DXF export** into the floor model (or straight into the open AutoCAD drawing), attaches wattages you supply, and totals the lighting load.

What it does **not** do: it does not read DIALux project files (`.dlx`/`.evo`), recalculate lux, or invent wattages. Lux results and wattages come from DIALux; you give the MCP the luminaire list.

## What to export from DIALux
1. **Drawing export (DWG or DXF)** with the luminaires placed (DIALux: *Export → CAD / DWG*). Luminaires should be drawn as **blocks** (inserts). If `dialux_inspect` reports "No block inserts found", export again with luminaires as blocks.
2. **Luminaire list** (Excel or CSV) with type, quantity and power (and luminous flux if you want it). Optional but needed for wattage.

DWG files can be read when the free [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter) is installed; otherwise save/export as DXF.

## Workflow
1. **Look inside the file**: *"Run `dialux_inspect` on `C:\...\floor4.dxf`."* It lists block names, counts, layers and attributes. Pick which blocks are luminaires (and which, if any, are emergency/exit signs).
2. **Align it to your architecture.** DIALux commonly exports in **metres** with its own origin. Pick two points you can identify in both drawings (two column or wall corners) and run `dialux_align` with their coordinates in each. It returns a transform with a `residual`; a residual near zero means the two points agree. If you already know the shift/rotation/scale, pass `dx, dy, rotation, scale` directly.
3. **Read the luminaire list** (optional check): `import_luminaire_list` shows the detected columns and any types with missing wattage.
4. **Import**: `dialux_import` with `block_names`, the `transform`, the `luminaire_list` path (or `watts_by_type`), and optionally a `circuit` and `type_map` (`{"EXIT_SIGN": "emergency"}`). It returns a **changeset** to review: counts per block, luminaires outside every room, missing wattages, and a scale warning if the imported extent does not match the rooms. Nothing is drawn until `apply_changes`.
5. **Schedule**: `lighting_schedule` totals luminaires and installed watts per room and floor, and W/m² where the room area and wattage are both known.

Re-importing is safe: luminaires at the same position update in place, never duplicate. `replace_previous=true` also removes earlier DIALux luminaires that are no longer in the file.

## Straight into AutoCAD (live mode)
*"Run `live_import_luminaires` for floor 4F with these block names and this transform."* It defaults to a **dry run** (counts and first positions). Repeat with `dry_run=false` to draw them in the open drawing as **one undo step**; `live_undo` reverts the whole import. Wattage and luminaire type are stored with each device and shown by `live_list_devices`.

## Checks that protect you
- **Scale/units check**: compares the imported span with the rooms' span; a ratio far from 1 is flagged even when points happen to fall inside some room (metres vs millimetres).
- **Outside-rooms count**: luminaires that fall in no room.
- **Missing wattage** is listed by type; the lighting total is marked incomplete rather than guessed.
- Everything is **DRAFT**: the schedule is *installed connected load* with no diversity or demand factor and no lux calculation.

## Limits (be aware)
- Built and tested against synthetic DIALux-style exports (block inserts with attributes, nested blocks, metres, rotated/shifted frames). **It has not been run on a real DIALux export yet.** Send a real export and the importer can be adjusted to its exact structure.
- Luminaires drawn as loose lines (not blocks) cannot be imported.
- Rooms are rectangles from `prepare_floor_model`; irregular rooms are approximated by their bounds.
