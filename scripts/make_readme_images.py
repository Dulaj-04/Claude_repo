"""Regenerate the README images in docs/images from the real MCP code.

    python scripts/make_readme_images.py

Everything is built from a synthetic demo floor (no real project data) by calling the same tool
functions an assistant would call. Needs matplotlib, openpyxl; the Excel picture also needs
LibreOffice (soffice) and pdftoppm, and is skipped when they are missing.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logging.disable(logging.CRITICAL)
ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT / "src"))

import ezdxf  # noqa: E402
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import openpyxl  # noqa: E402
from matplotlib.image import imread  # noqa: E402

from acad_electrical_mcp import dialux, server, workbook  # noqa: E402
from acad_electrical_mcp import plan as plans  # noqa: E402
from acad_electrical_mcp.model import Store  # noqa: E402

ROOMS = [
    {"id": "GF-1", "name": "LABORATORY", "bounds": [0, 0, 9000, 6000]},
    {"id": "GF-2", "name": "CLASSROOM", "bounds": [9000, 0, 18000, 6000]},
    {"id": "GF-3", "name": "CORRIDOR", "bounds": [0, 6000, 13000, 8400]},
    {"id": "GF-4", "name": "STORE", "bounds": [13000, 6000, 18000, 8400]},
]
BG, FG = "#1e1e1e", "#e6e6e6"


def make_architecture(path: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.units = 4
    for n, c in (("Wall", 7), ("Doors", 3), ("TEXT", 7), ("FURNITURE", 9)):
        doc.layers.add(n, color=c)
    m = doc.modelspace()
    for r in ROOMS:
        x0, y0, x1, y1 = r["bounds"]
        m.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": "Wall"})
        ny = {"GF-1": 3900, "GF-2": 4300, "GF-3": 6250, "GF-4": 6250}[r["id"]]
        m.add_text(r["name"], height=320, dxfattribs={"layer": "TEXT"}).set_placement((x0 + 350, ny))
    for (a, b) in [((2000, 6000), (3000, 6000)), ((10000, 6000), (11000, 6000)), ((13000, 7000), (13000, 7800))]:
        m.add_line(a, b, dxfattribs={"layer": "Doors", "color": 3})
    for x in (1500, 4500, 7000):  # lab benches
        m.add_lwpolyline([(x, 2300), (x + 1500, 2300), (x + 1500, 3000), (x, 3000)], close=True, dxfattribs={"layer": "FURNITURE"})
    for x in (10200, 12600, 15000):  # classroom desks
        for y in (1500, 3000):
            m.add_lwpolyline([(x, y), (x + 1800, y), (x + 1800, y + 500), (x, y + 500)], close=True, dxfattribs={"layer": "FURNITURE"})
    doc.saveas(str(path))


def to_dialux(x_mm: float, y_mm: float) -> tuple[float, float]:
    return (-y_mm / 1000 + 120.0, x_mm / 1000 + 40.0)  # metres, rotated 90 deg, own origin


def make_dialux_export(path: Path) -> list[tuple[str, float, float]]:
    doc = ezdxf.new("R2018")
    doc.units = 6
    for n in ("LUM_PANEL", "LUM_DOWN", "EXIT_SIGN"):
        b = doc.blocks.new(n)
        b.add_circle((0, 0), 0.15)
        b.add_attdef("LABEL", (0.2, 0), "x")
    pts: list[tuple[str, float, float]] = []
    for x0 in (1125, 10125):
        for i in range(4):
            for j in range(3):
                pts.append(("LUM_PANEL", x0 + i * 2250, 1000 + j * 2000))
    pts += [("LUM_DOWN", 1500 + i * 2500, 7200) for i in range(5)] + [("LUM_DOWN", 15500, 7200)]
    pts += [("EXIT_SIGN", 2500, 5600), ("EXIT_SIGN", 10500, 5600)]
    m = doc.modelspace()
    for k, (name, x, y) in enumerate(pts, 1):
        ref = m.add_blockref(name, to_dialux(x, y), dxfattribs={"layer": "DIALUX"})
        ref.add_auto_attribs({"LABEL": f"D{k:02d}"})
    doc.saveas(str(path))
    return pts


def build_demo(work: Path) -> dict:
    ws = work / "ws"
    server.CFG.workspace = ws
    server.CFG.require_plan = False
    server.STORE = Store(ws)
    server.PLANS = plans.PlanStore(ws)
    arch, dlx = work / "GF_architectural.dxf", work / "dialux_export.dxf"
    make_architecture(arch)
    pts = make_dialux_export(dlx)
    lst = work / "luminaires.csv"
    with open(lst, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Luminaire", "Quantity", "P [W]", "Φ [lm]"])
        w.writerows([["LUM_PANEL", 24, 36, 4200], ["LUM_DOWN", 6, 12, 1100], ["EXIT_SIGN", 2, 3, ""]])
    s = server
    s.prepare_floor_model("demo", "GF", str(arch), ROOMS)
    s.apply_changes("demo", "GF", s.plan_devices("demo", "GF", [{"type": "db", "x": 12400, "y": 7700}])["changeset_id"])
    t = dialux.fit_two_points(to_dialux(0, 0), to_dialux(6000, 0), (0, 0), (6000, 0))
    imp = s.dialux_import("demo", "GF", str(dlx), block_names=["LUM_PANEL", "LUM_DOWN", "EXIT_SIGN"], transform=t,
                          type_map={"EXIT_SIGN": "emergency"}, luminaire_list=str(lst), label_attribute="LABEL")
    s.apply_changes("demo", "GF", imp["changeset_id"])
    devs = s.list_devices("demo", "GF")["devices"]
    by_room = {}
    for d in devs:
        if d["type"] in ("luminaire", "emergency"):
            by_room.setdefault(d["room"], []).append(d["id"])
    for circ, rooms in (("GF-L01", ["GF-1"]), ("GF-L02", ["GF-2"]), ("GF-L03", ["GF-3", "GF-4"])):
        ids = [i for r in rooms for i in by_room.get(r, [])]
        s.apply_changes("demo", "GF", s.set_circuit_assignment("demo", "GF", circ, "lighting", "DB-GF", ids)["changeset_id"])
    extra = []
    for x in (600, 2700, 4800, 6900, 8400):
        extra.append({"type": "socket", "x": x, "y": 450, "room": "GF-1", "circuit": "GF-P01"})
    for x in (9600, 11700, 13800, 15900, 17400):
        extra.append({"type": "socket", "x": x, "y": 450, "room": "GF-2", "circuit": "GF-P02"})
    extra += [{"type": "data", "x": 1200, "y": 5300, "room": "GF-1", "circuit": "GF-P01"},
              {"type": "data", "x": 16800, "y": 5300, "room": "GF-2", "circuit": "GF-P02"},
              {"type": "ac", "x": 6500, "y": 5550, "room": "GF-1", "circuit": "GF-AC01"},
              {"type": "ac", "x": 14500, "y": 5550, "room": "GF-2", "circuit": "GF-AC02"},
              {"type": "switch", "x": 3400, "y": 5750, "room": "GF-1", "circuit": "GF-L01"},
              {"type": "switch", "x": 11300, "y": 5750, "room": "GF-2", "circuit": "GF-L02"}]
    s.apply_changes("demo", "GF", s.plan_devices("demo", "GF", extra)["changeset_id"])
    for c in ("GF-L01", "GF-L02", "GF-L03", "GF-P01", "GF-P02", "GF-AC01", "GF-AC02"):
        s.apply_changes("demo", "GF", s.plan_routes("demo", "GF", c)["changeset_id"])
    val = s.validate_drawing("demo", "GF")
    man = s.export_package("demo", "GF")
    sched = s.lighting_schedule("demo", "GF")
    return {"out": ws / "demo" / "output" / "GF", "validation": val, "manifest": man, "schedule": sched,
            "transform": t, "import": imp, "dialux_pts": [(n, *to_dialux(x, y)) for n, x, y in pts],
            "arch": arch, "devices": s.list_devices("demo", "GF")}


def fig_views(out: Path, dest: Path) -> None:
    names = [("Architectural_Base", "Architectural base"), ("Lighting_Design", "Lighting"),
             ("Power_Socket_AC_Design", "Power / sockets / AC"), ("Electrical_Complete", "Combined")]
    fig, axes = plt.subplots(2, 2, figsize=(14, 8.4), facecolor=BG)
    for ax, (suffix, title) in zip(axes.flat, names, strict=True):
        ax.imshow(imread(out / f"GF_{suffix}.png"))
        ax.set_title(title, color=FG, fontsize=13, pad=6)
        ax.axis("off")
    fig.subplots_adjust(left=0.01, right=0.99, top=0.95, bottom=0.01, wspace=0.02, hspace=0.12)
    fig.savefig(dest, dpi=100, facecolor=BG)
    plt.close(fig)


def draw_plan(ax, arch: Path, color="#9a9a9a") -> None:
    doc = ezdxf.readfile(str(arch))
    for e in doc.modelspace().query("LWPOLYLINE"):
        if e.dxf.layer == "Wall":
            pts = [(p[0], p[1]) for p in e.get_points()]
            pts.append(pts[0])
            ax.plot(*zip(*pts, strict=True), color=color, lw=1.2)


def fig_dialux(demo: dict, dest: Path) -> None:
    fig, (a, b) = plt.subplots(1, 2, figsize=(14, 5.4), facecolor=BG, gridspec_kw={"width_ratios": [1, 1.55]})
    cols = {"LUM_PANEL": "#f2c94c", "LUM_DOWN": "#56ccf2", "EXIT_SIGN": "#27ae60"}
    for n, x, y in demo["dialux_pts"]:
        a.scatter([x], [y], s=34, color=cols[n], zorder=3)
    a.set_title("1  DIALux export: metres, own origin, rotated", color=FG, fontsize=12)
    a.set_xlabel("x (m)", color=FG)
    a.set_ylabel("y (m)", color=FG)
    a.set_aspect("equal")
    draw_plan(b, demo["arch"])
    for d in demo["devices"]["devices"]:
        if d["type"] in ("luminaire", "emergency"):
            b.scatter([d["x"]], [d["y"]], s=34, color="#27ae60" if d["type"] == "emergency" else (
                "#f2c94c" if d.get("luminaire_type") == "LUM_PANEL" else "#56ccf2"), zorder=3)
    for r in ROOMS:
        x0, y0, x1, y1 = r["bounds"]
        b.text(x0 + 250, y1 - 500, r["name"], color="#bdbdbd", fontsize=10)
    t = demo["transform"]
    b.set_title(f"2  dialux_align (2 points) + dialux_import: {demo['import']['found_in_file']} symbols placed in rooms,\n"
                f"scale x{t['scale']:.0f}, rotation {t['rotation'] % 360:.0f}°, residual {t['residual']:.4f}",
                color=FG, fontsize=12)
    b.set_xlabel("x (mm)", color=FG)
    b.set_aspect("equal")
    for ax in (a, b):
        ax.set_facecolor(BG)
        ax.tick_params(colors=FG)
        for sp in ax.spines.values():
            sp.set_color("#555")
    handles = [plt.Line2D([], [], marker="o", ls="", color=c, label=n) for n, c in cols.items()]
    b.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=3, facecolor=BG,
             labelcolor=FG, edgecolor="#555", fontsize=9)
    fig.tight_layout()
    fig.savefig(dest, dpi=100, facecolor=BG)
    plt.close(fig)


def fig_excel(work: Path, dest: Path) -> bool:
    if not (shutil.which("soffice") and shutil.which("pdftoppm")):
        print("skip excel image (soffice/pdftoppm missing)")
        return False
    ws_dir = work / "xl"
    res = workbook.create(ws_dir, "demo", "load_schedule", True)
    rows = [
        ["GF", "DB-GF", "GF-LTG", "Lighting (from DIALux)", "lighting", 1, 1.08, None, 0.8, "1", "N", "lighting_schedule: 1080 W"],
        ["GF", "DB-GF", "GF-SKT", "Socket outlets", "sockets", 10, 0.3, 0.9, 0.5, "1", "N", "sample"],
        ["GF", "DB-GF", "GF-AC", "Air conditioning", "ac", 2, 3.5, 0.85, 0.8, "3", "N", "sample"],
        ["GF", "MSB", "PUMP-1", "Water pump 1", "pump", 1, 100, 0.85, 1.0, "3", "Y", "brief"],
        ["GF", "MSB", "PUMP-2", "Water pump 2", "pump", 1, 100, 0.85, 1.0, "3", "Y", "brief"],
        ["1F", "DB-1F", "1F-FIRE", "Fire alarm panel", "fire", 1, 0.5, 0.9, None, "1", "Y", "factor not given yet"],
    ]
    wb = openpyxl.load_workbook(res["path"])
    data = wb["Data"]
    for r, vals in enumerate(rows, start=2):
        for c, v in enumerate(vals, start=1):
            data.cell(row=r, column=c, value=v)
    data.delete_rows(len(rows) + 4, 400)
    del wb["Instructions"]
    for letter in ("J", "L"):  # hide phases and source columns so the picture stays readable
        data.column_dimensions[letter].hidden = True
    return render_sheet(work, wb, data, dest)


def render_sheet(work: Path, wb, data, dest: Path) -> bool:
    from PIL import Image, ImageChops
    data.page_setup.orientation = "landscape"
    data.sheet_properties.pageSetUpPr = openpyxl.worksheet.properties.PageSetupProperties(fitToPage=True)
    data.page_setup.fitToWidth, data.page_setup.fitToHeight = 1, 0
    tmp = work / "xl" / "shot.xlsx"
    for old in (*(work / "xl").glob("pg*.png"), work / "xl" / "shot.pdf"):
        old.unlink(missing_ok=True)
    wb.save(tmp)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(work / "xl"), str(tmp)],
                   check=True, capture_output=True, timeout=180)
    if not (work / "xl" / "shot.pdf").exists():
        raise SystemExit("LibreOffice produced no PDF (is libreoffice-calc installed?)")
    subprocess.run(["pdftoppm", "-png", "-r", "190", "-f", "1", "-l", "1", str(work / "xl" / "shot.pdf"), str(work / "xl" / "pg")], check=True)
    png = next((work / "xl").glob("pg*.png"))
    im = Image.open(png).convert("RGB")
    bbox = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
    im.crop((max(bbox[0] - 14, 0), max(bbox[1] - 14, 0), bbox[2] + 14, bbox[3] + 14)).save(dest)
    return True


def fig_placement(work: Path, dest: Path) -> bool:
    if not (shutil.which("soffice") and shutil.which("pdftoppm")):
        return False
    res = workbook.create(work / "xl", "demo", "placement_table", True)
    rows = [["GF", "LABORATORY", "socket", 500, 400, 0, "GF-P01", "DB-GF", "GF-R1", ""],
            ["GF", "LABORATORY", "socket", 2500, 400, 0, "GF-P01", "DB-GF", "GF-R2", ""],
            ["GF", "LABORATORY", "data", 3000, 4500, 0, "GF-P01", "DB-GF", "GF-D1", ""],
            ["GF", "CLASSROOM", "socket", 6500, 400, 0, "GF-P02", "DB-GF", "GF-R3", ""],
            ["GF", "CLASSROOM", "ac", 9000, 4500, 0, "GF-AC1", "DB-GF", "GF-AC1", "ceiling cassette"]]
    wb = openpyxl.load_workbook(res["path"])
    data = wb["Data"]
    for r, vals in enumerate(rows, start=2):
        for c, v in enumerate(vals, start=1):
            data.cell(row=r, column=c, value=v)
    data.delete_rows(len(rows) + 4, 400)
    del wb["Instructions"]
    return render_sheet(work, wb, data, dest)


def main() -> None:
    IMG.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="readme_imgs_"))
    demo = build_demo(work)
    out = demo["out"]
    shutil.copy(out / "GF_Electrical_Complete.png", IMG / "demo-combined.png")
    fig_views(out, IMG / "demo-views.png")
    fig_dialux(demo, IMG / "demo-dialux-import.png")
    fig_excel(work, IMG / "demo-load-schedule.png")
    fig_placement(work, IMG / "demo-placement-table.png")
    s = demo["schedule"]
    summary = {"validation_ok": demo["validation"]["ok"], "device_counts": demo["validation"]["device_counts"],
               "schedule_total": {k: s[k] for k in ("total_luminaires", "installed_watts", "installed_kw", "complete")},
               "rooms": [{k: r[k] for k in ("room", "name", "count", "watts", "area_m2", "w_per_m2")} for r in s["rooms"]],
               "import": {k: demo["import"][k] for k in ("found_in_file", "by_block", "outside_all_rooms", "operations")}}
    print(json.dumps(summary, indent=2))
    if not math.isfinite(s["installed_watts"]):
        raise SystemExit("bad schedule")


if __name__ == "__main__":
    main()
