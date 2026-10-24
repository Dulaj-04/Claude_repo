"""AutoCAD Electrical SCHEMATIC support (read + probe), on top of the live COM session.

AutoCAD Electrical stores components as block references carrying attributes (TAG1, DESC1,
MFG, CAT, ...), wire numbers as attribute blocks and wires as LINEs on wire layers. This module
reads them and can evaluate AutoLISP in the running AutoCAD to discover which Electrical
commands/functions an installation exposes. Insert/wire tools are deliberately NOT here yet:
they will be built against what ``probe`` reports for the user's version.

Results come back from AutoLISP through the USERS1 system variable (<= ~450 characters per
round trip, chunked), because SendCommand itself returns nothing.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .backends.base import BackendError
from .live import LiveSession

CHUNK = 440
MAX_RESULT = 30000
# attribute names AutoCAD Electrical uses on components (reading is tolerant of others)
COMPONENT_ATTRS = ("TAG1", "TAG2", "TAGSTRIP", "DESC1", "DESC2", "DESC3", "INST", "LOC", "MFG",
                   "CAT", "ASSYCODE", "RATING1", "RATING2", "RATING3", "WDBLKNAM")
WIRE_NUMBER_ATTRS = ("WIRENO", "WIRENUM", "WIRE_NO")


class Schematic:
    def __init__(self, live: LiveSession) -> None:
        self.live = live

    # ------------------------------------------------------------------ AutoLISP bridge
    def lisp(self, expr: str, timeout: float = 20.0) -> str:
        """Evaluate an AutoLISP expression in the active drawing and return its printed value."""
        live, c = self.live, self.live.com.call
        doc = live.doc

        def run(cmd: str) -> str:
            c(lambda: doc.SetVariable("USERS1", ""))
            c(lambda: doc.SendCommand(cmd + "\n"))
            end = time.time() + timeout
            while time.time() < end:
                val = str(c(lambda: doc.GetVariable("USERS1")))
                if val:
                    return val
                time.sleep(0.15)
            raise BackendError(
                "AutoCAD did not answer the AutoLISP request in time. It may be busy or inside "
                "a command/dialog: press Esc in AutoCAD and try again.")

        first = run(
            '(progn (setq *acm-r* (vl-catch-all-apply (quote (lambda () (vl-princ-to-string '
            + expr + '))))) (if (vl-catch-all-error-p *acm-r*) (setq *acm-r* (strcat "LISP ERROR: "'
            ' (vl-catch-all-error-message *acm-r*)))) '
            f'(setvar "USERS1" (strcat "OK:" (substr *acm-r* 1 {CHUNK}))) (princ))')
        out = first[3:] if first.startswith("OK:") else first
        pos = CHUNK + 1
        while len(out) == pos - 1 and len(out) < MAX_RESULT:
            nxt = run(f'(progn (setvar "USERS1" (strcat "OK:" (substr *acm-r* {pos} {CHUNK}))) '
                      '(princ))')
            nxt = nxt[3:] if nxt.startswith("OK:") else nxt
            if not nxt:
                break
            out += nxt
            pos += CHUNK
            if len(nxt) < CHUNK:
                break
        if out.startswith("LISP ERROR"):
            raise BackendError(out)
        return out

    # ------------------------------------------------------------------ reading
    def _block_refs(self) -> list[dict[str, Any]]:
        c = self.live.com.call
        out = []
        for e in c(lambda: self.live._ms()):
            if "BlockReference" not in str(c(lambda e=e: e.ObjectName)):
                continue
            attrs: dict[str, str] = {}
            if c(lambda e=e: e.HasAttributes):
                for a in c(lambda e=e: e.GetAttributes()):
                    attrs[str(c(lambda a=a: a.TagString)).upper()] = str(c(lambda a=a: a.TextString))
            p = c(lambda e=e: e.InsertionPoint)
            try:
                name = str(c(lambda e=e: e.EffectiveName))
            except Exception:  # noqa: BLE001
                name = str(c(lambda e=e: e.Name))
            out.append({"block": name, "layer": str(c(lambda e=e: e.Layer)),
                        "x": round(p[0], 2), "y": round(p[1], 2), "attributes": attrs,
                        "handle": str(c(lambda e=e: e.Handle))})
        return out

    def read(self, include_wires: bool = True, limit: int = 500) -> dict[str, Any]:
        """Components (tags/descriptions/catalog data), wire numbers and wire geometry."""
        comps, wnos, others = [], [], 0
        for b in self._block_refs():
            a = b["attributes"]
            if any(k in a for k in WIRE_NUMBER_ATTRS):
                wnos.append({"wire_number": next(a[k] for k in WIRE_NUMBER_ATTRS if k in a),
                             "x": b["x"], "y": b["y"], "layer": b["layer"], "block": b["block"]})
            elif "TAG1" in a or "TAG2" in a or "TAGSTRIP" in a:
                comps.append({
                    "tag": a.get("TAG1") or a.get("TAG2") or a.get("TAGSTRIP"),
                    "block": b["block"], "layer": b["layer"], "x": b["x"], "y": b["y"],
                    "description": " ".join(a.get(k, "") for k in ("DESC1", "DESC2", "DESC3")).strip(),
                    "installation": a.get("INST", ""), "location": a.get("LOC", ""),
                    "manufacturer": a.get("MFG", ""), "catalog": a.get("CAT", ""),
                    "terminals": {k: v for k, v in a.items()
                                  if k.startswith(("TERM", "X1TERM", "X2TERM")) and v},
                    "other_attributes": {k: v for k, v in a.items()
                                         if k not in COMPONENT_ATTRS and not k.startswith(
                                             ("TERM", "X1TERM", "X2TERM")) and v},
                    "handle": b["handle"]})
            elif a:
                others += 1
        res: dict[str, Any] = {
            "components": comps[:limit], "components_total": len(comps),
            "wire_numbers": wnos[:limit], "wire_numbers_total": len(wnos),
            "other_attribute_blocks": others,
            "looks_like_schematic": bool(comps),
        }
        if include_wires:
            res["wires"] = self._wires()
        return res

    def _wires(self) -> dict[str, Any]:
        c = self.live.com.call
        per_layer: dict[str, int] = {}
        for e in c(lambda: self.live._ms()):
            if "Line" not in str(c(lambda e=e: e.ObjectName)):
                continue
            lay = str(c(lambda e=e: e.Layer))
            if "WIRE" in lay.upper() or lay.upper().startswith("WD"):
                per_layer[lay] = per_layer.get(lay, 0) + 1
        return {"lines_per_wire_layer": per_layer, "total": sum(per_layer.values()),
                "note": "Layers containing WIRE/WD. Line endpoints are not exposed here; the "
                        "wire network itself lives in AutoCAD Electrical's data."}

    # ------------------------------------------------------------------ probing
    def arx_modules(self) -> list[str]:
        """Compiled modules (ARX) loaded in this session. AutoCAD Electrical registers its
        commands through its own modules, which AutoLISP symbol checks cannot see."""
        raw = self.lisp('(apply (quote strcat) (mapcar (quote (lambda (s) (strcat s ","))) (arx)))')
        return [m for m in raw.split(",") if m and m.lower() != "nil"]

    def commands_exist(self, names: list[str]) -> dict[str, str]:
        """Ask AutoCAD whether each command name is registered (works for ARX/.NET commands)."""
        out: dict[str, str] = {}
        for n in names:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", n or ""):
                raise ValueError(f"Invalid command name {n!r}: letters, digits, '_', '-', '.' only")
            out[n] = "yes" if self.lisp(f'(if (getcname "{n}") "yes" "no")') == "yes" else "no"
        return out

    def detect(self) -> dict[str, Any]:
        app = self.live.app
        c = self.live.com.call
        info: dict[str, Any] = {"caption": str(c(lambda: app.Caption)),
                                "version": str(c(lambda: app.Version))}
        try:
            info["product"] = self.lisp('(getvar "PRODUCT")')
            info["acadver"] = self.lisp('(getvar "ACADVER")')
            info["lisp_bridge"] = "ok"
        except BackendError as exc:
            info["lisp_bridge"] = f"failed: {exc}"
        evidence: list[str] = []
        negative = 0
        if info["lisp_bridge"] == "ok":
            try:
                mods = self.arx_modules()
                info["arx_modules"] = mods
                hits = [m for m in mods if re.search(r"acade|ace_|wd_|electrical", m, re.I)]
                if hits:
                    evidence.append(f"Electrical module(s) loaded: {', '.join(hits)}")
                else:
                    negative += 1
            except BackendError as exc:
                info["arx_modules"] = f"failed: {exc}"
            try:
                cmds = self.commands_exist(["AEPROJECT"])
                info["registered_commands"] = cmds
                if cmds["AEPROJECT"] == "yes":
                    evidence.append("AEPROJECT is a registered command")
                else:
                    negative += 1
            except BackendError as exc:
                info["registered_commands"] = f"failed: {exc}"
        if re.search(r"electrical", info["caption"], re.I):
            evidence.append("window title says AutoCAD Electrical")
        info["electrical_loaded"] = ("yes" if any("module" in e or "command" in e for e in evidence)
                                     else ("unknown" if evidence else ("no" if negative else "unknown")))
        info["evidence"] = evidence
        info["note"] = ("PRODUCT reports plain 'AutoCAD' in vertical products, so it is not used "
                        "as evidence. 'unknown' with only the window title means the module list "
                        "and command check found nothing: use sch_check_commands with names you "
                        "know from the Electrical ribbon or help.")
        summary = self.read(include_wires=True, limit=0)
        info.update(components=summary["components_total"],
                    wire_numbers=summary["wire_numbers_total"],
                    wire_lines=summary["wires"]["total"],
                    looks_like_schematic=summary["looks_like_schematic"])
        return info

    def probe(self, prefix: str = "c:ae", limit: int = 400) -> dict[str, Any]:
        """List AutoLISP symbols present whose name starts with ``prefix`` (case-insensitive).
        Defaults to AutoCAD Electrical commands (c:ae*). Try c:wd, c:ace, wd_, ace_ as well."""
        pat = prefix.replace('"', "").replace("\\", "") + "*"
        raw = self.lisp(
            '(apply (quote strcat) (mapcar (quote (lambda (s) (strcat s ","))) (acad_strlsort '
            f'(vl-remove-if-not (quote (lambda (s) (wcmatch (strcase s) (strcase "{pat}")))) '
            '(atoms-family 1)))))')
        names = [n for n in raw.split(",") if n]
        return {"prefix": prefix, "count": len(names), "symbols": names[:limit],
                "truncated": len(names) > limit,
                "note": "These are the functions/commands this AutoCAD Electrical install exposes. "
                        "Share this list so insert/wire tools can be built against real names."}
