import pytest

from acad_electrical_mcp import server
from acad_electrical_mcp.backends.base import BackendError
from acad_electrical_mcp.live import LiveSession, PlainCom
from acad_electrical_mcp.schematic import Schematic
from fake_acad import App


@pytest.fixture()
def sch(tmp_path):
    app = App()
    doc = app.ActiveDocument
    doc.add_block("HPB11", (100, 200), {"TAG1": "PB1", "DESC1": "START", "DESC2": "PUSHBUTTON",
                                       "INST": "PANEL", "LOC": "FRONT", "MFG": "ABB",
                                       "CAT": "CP1-10", "TERM01": "13", "TERM02": "14"})
    doc.add_block("VCR11", (300, 200), {"TAG1": "K1", "DESC1": "MOTOR", "X1TERM01": "A1"})
    doc.add_block("WD_WNUM", (150, 210), {"WIRENO": "101"})
    doc.add_block("TITLE", (0, 0), {"DWGNAME": "x"}, layer="0")
    for lay in ("WIRES", "WIRES", "WIRES_B", "WALL"):
        doc.ModelSpace.AddLine((0, 0, 0), (50, 0, 0)).Layer = lay
    live = LiveSession(PlainCom(app), tmp_path / "st")
    return Schematic(live), live, app


def test_read_components_wire_numbers_and_wires(sch):
    s, _, _ = sch
    r = s.read()
    assert r["components_total"] == 2 and r["looks_like_schematic"]
    pb = next(c for c in r["components"] if c["tag"] == "PB1")
    assert pb["description"] == "START PUSHBUTTON" and pb["manufacturer"] == "ABB"
    assert pb["catalog"] == "CP1-10" and pb["terminals"] == {"TERM01": "13", "TERM02": "14"}
    assert r["wire_numbers"][0]["wire_number"] == "101"
    assert r["wires"]["lines_per_wire_layer"] == {"WIRES": 2, "WIRES_B": 1}
    assert r["other_attribute_blocks"] == 1  # the title block is not a component


def test_lisp_roundtrip_chunking_and_errors(sch):
    s, _, app = sch
    big = "x" * 1000
    app.ActiveDocument.lisp_handler = lambda cmd: big
    assert s.lisp("anything") == big
    app.ActiveDocument.lisp_handler = lambda cmd: "short"
    assert s.lisp("anything") == "short"

    def boom(cmd):
        raise RuntimeError("bad expr")
    app.ActiveDocument.lisp_handler = boom
    with pytest.raises(BackendError, match="LISP ERROR"):
        s.lisp("(oops)")


def test_lisp_timeout_when_autocad_busy(sch, monkeypatch):
    s, _, app = sch
    app.ActiveDocument.SendCommand = lambda cmd: None  # AutoCAD never answers
    monkeypatch.setattr("acad_electrical_mcp.schematic.time.sleep", lambda *_: None)
    t = iter(range(0, 1000, 10))
    monkeypatch.setattr("acad_electrical_mcp.schematic.time.time", lambda: next(t))
    with pytest.raises(BackendError, match="did not answer"):
        s.lisp("(x)", timeout=15)


def test_probe(sch):
    s, _, app = sch
    app.ActiveDocument.lisp_handler = lambda cmd: "C:AEPROJECT,C:AECOMPONENT,C:AEWIRENO,"
    p = s.probe()
    assert p["symbols"] == ["C:AEPROJECT", "C:AECOMPONENT", "C:AEWIRENO"] and not p["truncated"]


def _handler(arx, command):
    def handler(cmd):
        if "(arx)" in cmd:
            return arx
        if "getcname" in cmd:
            return command
        return {"PRODUCT": "AutoCAD", "ACADVER": "25.1s"}["PRODUCT" if "PRODUCT" in cmd else "ACADVER"]
    return handler


def test_detect_sees_electrical_modules_even_when_no_lisp_symbol(sch):
    s, _, app = sch
    app.ActiveDocument.lisp_handler = _handler("acad.arx,acade.arx,", "yes")
    d = s.detect()
    assert d["lisp_bridge"] == "ok" and d["components"] == 2 and d["looks_like_schematic"]
    assert d["electrical_loaded"] == "yes" and d["registered_commands"] == {"AEPROJECT": "yes"}
    assert any("acade.arx" in e for e in d["evidence"])


def test_detect_not_loaded_only_when_module_and_command_both_missing(sch):
    s, _, app = sch
    app.ActiveDocument.lisp_handler = _handler("acad.arx,acetutil.arx,", "no")
    app.Caption = "AutoCAD 2026 - [Drawing1.dwg]"
    d = s.detect()
    assert d["electrical_loaded"] == "no" and d["evidence"] == []
    # window title alone is not enough to claim it is loaded
    app.Caption = "AutoCAD Electrical 2026 - [Drawing1.dwg]"
    d = s.detect()
    assert d["electrical_loaded"] == "unknown" and d["evidence"] == ["window title says AutoCAD Electrical"]


def test_commands_exist_validates_names(sch):
    s, _, app = sch
    app.ActiveDocument.lisp_handler = lambda cmd: "yes" if "AEPROJECT" in cmd else "no"
    assert s.commands_exist(["AEPROJECT", "NOPE"]) == {"AEPROJECT": "yes", "NOPE": "no"}
    with pytest.raises(ValueError):
        s.commands_exist(['X") (command "erase"'])


def test_mode_switch_blocks_plan_tools_and_back(sch, monkeypatch):
    _, live, _ = sch
    monkeypatch.setattr(server, "_LIVE", live)
    assert server.live_place("4F", "socket", 10, 10)["created"]  # default = plan mode
    server.live_set_mode("schematic")
    with pytest.raises(ValueError, match="Schematic mode is on"):
        server.live_place("4F", "socket", 20, 20)
    with pytest.raises(ValueError, match="Schematic mode is on"):
        server.live_route("C1", "lighting")
    assert server.sch_read(limit=5)["components_total"] == 2  # sch tools work
    server.live_set_mode("plan")
    assert server.live_place("4F", "socket", 20, 20)["created"]


def test_run_lisp_needs_explicit_enable(sch, monkeypatch):
    _, live, app = sch
    monkeypatch.setattr(server, "_LIVE", live)
    monkeypatch.setattr(server.CFG, "allow_commands", False)
    with pytest.raises(ValueError, match="disabled"):
        server.sch_run_lisp("(+ 1 2)")
    monkeypatch.setattr(server.CFG, "allow_commands", True)
    app.ActiveDocument.lisp_handler = lambda cmd: "3"
    assert server.sch_run_lisp("(+ 1 2)") == {"result": "3"}
