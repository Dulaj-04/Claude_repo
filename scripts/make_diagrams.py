"""Write the README diagrams (SVG) to docs/images.   python scripts/make_diagrams.py"""

from __future__ import annotations

from html import escape
from pathlib import Path

IMG = Path(__file__).resolve().parents[1] / "docs" / "images"
FONT = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"
MONO = "'SFMono-Regular', Consolas, 'DejaVu Sans Mono', monospace"
BG, CARD, EDGE = "#0f172a", "#1e293b", "#334155"
TXT, MUTED = "#e2e8f0", "#94a3b8"
BLUE, AMBER, GREEN, VIOLET, GREY, RED = "#38bdf8", "#fbbf24", "#34d399", "#a78bfa", "#64748b", "#f87171"


def svg(w: int, h: int, body: str, title: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-label="{escape(title)}"><title>{escape(title)}</title>'
            f'<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{MUTED}"/></marker></defs>'
            f'<rect width="{w}" height="{h}" rx="16" fill="{BG}"/>{body}</svg>')


def t(x, y, s, size=16, fill=TXT, weight="400", anchor="start", mono=False) -> str:
    fam = MONO if mono else FONT
    return (f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" fill="{fill}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{escape(s)}</text>')


def box(x, y, w, h, fill=CARD, stroke=EDGE, r=12, sw=1.5) -> str:
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'


def arrow(x1, y1, x2, y2, color=MUTED) -> str:
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="2" '
            f'marker-end="url(#a)"/>')


def pill(x, y, label, color, w=None) -> str:
    w = w or 14 + 8.2 * len(label)
    return (f'<rect x="{x}" y="{y}" width="{w}" height="26" rx="13" fill="{color}" fill-opacity="0.16" '
            f'stroke="{color}" stroke-width="1.2"/>' + t(x + w / 2, y + 18, label, 13, color, "600", "middle"))


def banner() -> str:
    b = t(60, 98, "autocad-electrical-mcp", 48, TXT, "700")
    b += t(62, 134, "Plan, draw and check electrical installations", 22, MUTED)
    b += t(62, 162, "in AutoCAD Electrical, by chatting.", 22, MUTED)
    x = 62
    for label, c in (("Live in AutoCAD", BLUE), ("Offline DXF / PDF", GREEN), ("DIALux import", AMBER),
                     ("Excel inputs", VIOLET), ("Plan-first", RED)):
        b += pill(x, 188, label, c)
        x += 14 + 8.2 * len(label) + 12
    b += t(62, 250, "Claude Desktop, Claude Code and any MCP client (ChatGPT via HTTPS)", 15, MUTED)
    # mini floor plan motif
    ox, oy = 800, 40
    b += box(ox, oy, 380, 220, "#111c33", EDGE, 10)
    b += f'<g stroke="#cbd5e1" stroke-width="2" fill="none"><rect x="{ox+20}" y="{oy+20}" width="340" height="180"/><line x1="{ox+190}" y1="{oy+20}" x2="{ox+190}" y2="{oy+200}"/><line x1="{ox+20}" y1="{oy+150}" x2="{ox+360}" y2="{oy+150}"/></g>'
    for i in range(3):
        for j in range(2):
            cx, cy = ox + 70 + i * 55, oy + 55 + j * 45
            b += (f'<g stroke="#fbbf24" stroke-width="1.6" fill="none"><circle cx="{cx}" cy="{cy}" r="9"/>'
                  f'<line x1="{cx-6}" y1="{cy-6}" x2="{cx+6}" y2="{cy+6}"/><line x1="{cx-6}" y1="{cy+6}" x2="{cx+6}" y2="{cy-6}"/></g>')
    for i in range(3):
        for j in range(2):
            cx, cy = ox + 245 + i * 38, oy + 60 + j * 45
            b += (f'<g stroke="#fbbf24" stroke-width="1.6" fill="none"><circle cx="{cx}" cy="{cy}" r="9"/>'
                  f'<line x1="{cx-6}" y1="{cy-6}" x2="{cx+6}" y2="{cy+6}"/><line x1="{cx-6}" y1="{cy+6}" x2="{cx+6}" y2="{cy-6}"/></g>')
    b += f'<polyline points="{ox+70},{oy+100} {ox+70},{oy+135} {ox+270},{oy+135}" stroke="#f87171" stroke-width="2" fill="none"/>'
    b += f'<rect x="{ox+270}" y="{oy+125}" width="22" height="20" fill="none" stroke="#34d399" stroke-width="2"/><line x1="{ox+270}" y1="{oy+125}" x2="{ox+292}" y2="{oy+145}" stroke="#34d399" stroke-width="2"/>'
    for i in range(5):
        b += f'<circle cx="{ox+45+i*60}" cy="{oy+185}" r="6" fill="none" stroke="#e879f9" stroke-width="2"/>'
    return svg(1200, 290, b, "autocad-electrical-mcp")


def architecture() -> str:
    b = t(40, 46, "How it fits together", 26, TXT, "700")
    b += box(40, 90, 230, 300)
    b += t(155, 124, "You + an AI client", 18, TXT, "700", "middle")
    for i, s in enumerate(("Claude Desktop", "Claude Code", "ChatGPT (via HTTPS)", "any MCP client")):
        b += box(60, 150 + i * 56, 190, 42, BG, EDGE, 8) + t(155, 177 + i * 56, s, 15, MUTED, anchor="middle")
    b += t(155, 378, "chat in plain language", 13, AMBER, anchor="middle")
    b += arrow(272, 240, 338, 240) + arrow(338, 262, 272, 262)
    b += box(340, 70, 420, 340, "#13203a", BLUE, 14, 2)
    b += t(550, 104, "acad-electrical-mcp (63 tools)", 18, BLUE, "700", "middle")
    mods = (("Plan-first engine", "goal, steps, inputs needed", RED), ("Drawing model", "changesets, preview, undo", GREEN),
            ("Live editor", "COM, one undo step per call", BLUE), ("DIALux importer", "inspect, align, import", AMBER),
            ("Excel sheets", "create, check, load summary", VIOLET), ("Validation", "ids, circuits, routes, files", GREY))
    for i, (n, d, c) in enumerate(mods):
        x, y = 360 + (i % 2) * 200, 124 + (i // 2) * 92
        b += box(x, y, 190, 80, CARD, c, 10) + t(x + 95, y + 34, n, 14, TXT, "600", "middle") + t(x + 95, y + 58, d, 12, MUTED, anchor="middle")
    outs = (("AutoCAD / AutoCAD Electrical", "open drawing edited live", BLUE), ("Drawing files", "DXF, PNG, A3 PDF, manifest", GREEN),
            ("Excel workbooks", "load schedule, cable inputs", VIOLET), ("DIALux exports", "DWG/DXF layout, luminaire list", AMBER))
    for i, (n, d, c) in enumerate(outs):
        y = 90 + i * 78
        b += box(840, y, 260, 64, CARD, c, 10) + t(970, y + 28, n, 15, TXT, "600", "middle") + t(970, y + 49, d, 12, MUTED, anchor="middle")
        b += arrow(762, 240, 838, y + 32) if i not in (3,) else arrow(838, y + 32, 762, 240)
    b += t(550, 440, "Reference drawings are never modified. Everything produced is a DRAFT drawing aid.", 14, MUTED, anchor="middle")
    return svg(1140, 470, b, "Architecture of autocad-electrical-mcp")


def plan_flow() -> str:
    steps = (("1", "You ask", "a large request", AMBER, "you"), ("2", "Goal drafted", "plan_define", BLUE, "ai"),
             ("3", "You approve", "plan_approve", AMBER, "you"), ("4", "Inputs requested", "plan_next", BLUE, "ai"),
             ("5", "Steps run", "plan_update_step", BLUE, "ai"), ("6", "Checked output", "validate / export", GREEN, "ai"))
    b = t(40, 46, "Plan-first workflow", 26, TXT, "700")
    b += t(40, 74, "For anything bigger than one action, the assistant defines the goal first and asks you for exactly what it needs.", 15, MUTED)
    w, gap, x0, y = 160, 18, 40, 112
    for i, (n, title, sub, c, who) in enumerate(steps):
        x = x0 + i * (w + gap)
        b += box(x, y, w, 120, CARD, c, 12, 2) + f'<circle cx="{x+26}" cy="{y+28}" r="14" fill="{c}"/>' + t(x + 26, y + 34, n, 15, BG, "700", "middle")
        b += t(x + w / 2, y + 74, title, 16, TXT, "600", "middle") + t(x + w / 2, y + 98, sub, 12, MUTED, anchor="middle", mono=True)
        if i < len(steps) - 1:
            b += arrow(x + w + 2, y + 60, x + w + gap - 2, y + 60)
    b += pill(40, 256, "you", AMBER, 50) + t(98, 275, "decide and supply", 13, MUTED)
    b += pill(260, 256, "assistant", BLUE, 86) + t(354, 275, "plans, checks, keeps the plan updated", 13, MUTED)
    b += box(40, 304, 1060, 112, CARD, EDGE, 12)
    b += t(60, 336, "Inputs it can ask you for", 15, TXT, "700")
    for i, (lab, c) in enumerate((("files: drawings, DIALux export", GREEN), ("Excel sheets it creates for you", VIOLET),
                                  ("values with their source", AMBER), ("decisions: circuits, DB location", BLUE))):
        b += pill(60 + i * 258, 352, lab, c, 242)
    b += t(60, 404, "A step cannot finish while its inputs are missing or earlier steps are unfinished, and finishing records a result.", 13, MUTED)
    return svg(1140, 440, b, "Plan-first workflow")


def live_flow() -> str:
    b = t(40, 46, "Live mode: edit the open drawing while you chat", 26, TXT, "700")
    cards = (("You say", "“Add a socket on the north wall of the Classroom on circuit GF-P02”", AMBER, "chat"),
             ("Tool call", "live_place  →  live_assign  →  live_route", BLUE, "MCP"),
             ("AutoCAD", "symbol drawn on SOCKETS in your style, grouped as ACADE_<id>, one undo mark", GREEN, "COM"),
             ("You see it", "live_zoom shows the change. “Undo that” → live_undo reverts exactly that instruction", VIOLET, "screen"))
    w, gap, y = 245, 22, 96
    for i, (h, d, c, tag) in enumerate(cards):
        x = 40 + i * (w + gap)
        b += box(x, y, w, 190, CARD, c, 12, 2) + pill(x + 14, y + 14, tag, c, 62) + t(x + 14, y + 66, h, 18, TXT, "700")
        words, line, lines = d.split(), "", []
        for wd in words:
            if len(line) + len(wd) > 27:
                lines.append(line)
                line = wd
            else:
                line = (line + " " + wd).strip()
        lines.append(line)
        for k, ln in enumerate(lines[:5]):
            b += t(x + 14, y + 92 + k * 22, ln, 14, MUTED)
        if i < 3:
            b += arrow(x + w + 2, y + 95, x + w + gap - 2, y + 95)
    b += t(40, 330, "Hand edits are respected: move a symbol in AutoCAD and the next call uses its real position.", 14, MUTED)
    b += t(40, 354, "Illustrated flow. Windows + AutoCAD or AutoCAD Electrical running with a drawing open.", 13, GREY)
    return svg(1140, 380, b, "Live mode flow")


def roadmap() -> str:
    rows = (("1", "Understand the building (CAD)", "Built", "reads rooms, layers, units; never edits the reference"),
            ("2", "Load identification", "Partly", "Excel load schedule + checks; you supply the loads"),
            ("3", "Lighting design", "Built", "DIALux import, alignment, lighting schedule (lux stays in DIALux)"),
            ("4", "Socket outlet design", "Partly", "placement, circuits, routes, drawings; no socket load schedule yet"),
            ("5", "Maximum demand", "Partly", "connected + demand kW from YOUR factors; incomplete is flagged"),
            ("6", "Transformer selection", "Not yet", ""),
            ("7", "Generator selection", "Not yet", "essential-load total only"),
            ("8", "Three-phase distribution design", "Partly", "boards and circuits in the drawing; no phase balancing"),
            ("9", "Cable sizing", "Partly", "Excel inputs + visible formulas; no calculation tool"),
            ("10", "Protection design", "Partly", "device column and Ib ≤ In ≤ Iz check in the sheet"),
            ("11", "Single line diagram", "Not yet", ""),
            ("12", "BOQ and cost estimate", "Not yet", "device counts per floor are available"),
            ("13", "Final report", "Not yet", ""))
    col = {"Built": GREEN, "Partly": AMBER, "Not yet": GREY}
    b = t(40, 46, "Your work plan: what the MCP covers today", 26, TXT, "700")
    b += t(40, 74, "Honest status of each part of the electrical installation project plan.", 15, MUTED)
    for i, (n, title, st, note) in enumerate(rows):
        y = 96 + i * 44
        b += box(40, y, 1060, 38, CARD, EDGE, 8, 1) + t(60, y + 25, n, 15, MUTED, "700") + t(100, y + 25, title, 16, TXT, "600")
        b += pill(430, y + 6, st, col[st], 78) + t(530, y + 25, note, 13, MUTED)
    b += t(40, 96 + 13 * 44 + 30, "Everything is a DRAFT aid: engineering values come from you and your approved sources.", 14, MUTED)
    return svg(1140, 96 + 13 * 44 + 56, b, "Work plan coverage")


def main() -> None:
    IMG.mkdir(parents=True, exist_ok=True)
    for name, fn in (("banner", banner), ("architecture", architecture), ("workflow-plan-first", plan_flow),
                     ("workflow-live", live_flow), ("roadmap", roadmap)):
        (IMG / f"{name}.svg").write_text(fn(), encoding="utf-8")
        print("wrote", IMG / f"{name}.svg")


if __name__ == "__main__":
    main()
