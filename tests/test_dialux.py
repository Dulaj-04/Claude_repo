import math

import ezdxf
import pytest

from acad_electrical_mcp import dialux, server
from acad_electrical_mcp.live import LiveSession, PlainCom
from fake_acad import App

# Luminaire positions in the architectural drawing (mm), inside rooms 4-A / 4-B of the sample floor
TRUE = [(1500, 1250), (4500, 1250), (1500, 3750), (4500, 3750), (8000, 2500)]


def to_dialux_frame(x, y):
    """DIALux exports in metres, origin elsewhere, rotated 90 degrees: the inverse of the real
    alignment the tests then recover."""
    xm, ym = x / 1000.0, y / 1000.0
    return (-ym + 120.0, xm + 40.0)  # rotate +90, shift


def make_export(path, with_nested=False):
    doc = ezdxf.new("R2018")
    doc.units = 6
    for name in ("LUM_A", "LUM_B", "EXIT_SIGN"):
        blk = doc.blocks.new(name)
        blk.add_circle((0, 0), 0.15)
        blk.add_attdef("LABEL", (0.2, 0), "x")
    msp = doc.modelspace()
    for i, (x, y) in enumerate(TRUE):
        name = "LUM_A" if i < 3 else "LUM_B"
        ref = msp.add_blockref(name, to_dialux_frame(x, y), dxfattribs={"layer": "DIALUX_LUM"})
        ref.add_auto_attribs({"LABEL": f"L{i + 1}"})
    msp.add_blockref("EXIT_SIGN", to_dialux_frame(900, 600), dxfattribs={"layer": "DIALUX_EXIT"})
    msp.add_blockref("*U1", (0, 0))  # anonymous block names are ignored
    if with_nested:
        outer = doc.blocks.new("PROJECT")
        outer.add_blockref("LUM_A", (1, 1))
        msp.add_blockref("PROJECT", (10, 10))
    doc.saveas(path)
    return path


@pytest.fixture()
def export(tmp_path):
    return make_export(str(tmp_path / "dialux.dxf"))


def test_inspect_lists_blocks_layers_attributes_and_hints(export):
    r = dialux.inspect(export)
    names = {b["block"]: b for b in r["blocks"]}
    assert names["LUM_A"]["count"] == 3 and names["LUM_B"]["count"] == 2
    assert names["LUM_A"]["looks_like_luminaire"] and names["LUM_A"]["sample_attributes"] == {"LABEL": "L1"}
    assert "*U1" not in names and r["block_total"] == 6
    assert any("DIALUX_LUM" in b["layers"] for b in r["blocks"])


def test_inspect_warns_when_no_blocks(tmp_path):
    doc = ezdxf.new("R2018")
    doc.modelspace().add_line((0, 0), (1, 1))
    doc.saveas(tmp_path / "lines.dxf")
    r = dialux.inspect(str(tmp_path / "lines.dxf"))
    assert r["blocks"] == [] and any("No block inserts" in w for w in r["warnings"])


def test_fit_two_points_recovers_the_alignment():
    a_src, b_src = to_dialux_frame(0, 0), to_dialux_frame(6000, 0)
    t = dialux.fit_two_points(a_src, b_src, (0, 0), (6000, 0))
    assert math.isclose(t["scale"], 1000, rel_tol=1e-9) and math.isclose(t["rotation"] % 360, 270, abs_tol=1e-6)
    assert t["residual"] < 1e-6
    x, y = dialux.apply_transform(t, *to_dialux_frame(4500, 3750))
    assert (round(x, 3), round(y, 3)) == (4500.0, 3750.0)
    with pytest.raises(ValueError):
        dialux.fit_two_points((0, 0), (0, 0), (1, 1), (2, 2))
    with pytest.raises(ValueError):
        dialux.make_transform(scale=0)
    with pytest.raises(ValueError):
        dialux.make_transform(dx=float("nan"))


def test_extract_needs_a_selection_and_filters(export):
    with pytest.raises(ValueError, match="Say what to import"):
        dialux.extract(export)
    assert len(dialux.extract(export, block_names=["lum_a"])) == 3  # case-insensitive
    assert len(dialux.extract(export, layers=["DIALUX_EXIT"])) == 1


def test_nested_blocks_need_the_flag(tmp_path):
    p = make_export(str(tmp_path / "n.dxf"), with_nested=True)
    assert len(dialux.extract(p, block_names=["LUM_A"])) == 3
    assert len(dialux.extract(p, block_names=["LUM_A"], include_nested=True)) == 4


def test_luminaire_list_csv_dialux_style_headers(tmp_path):
    f = tmp_path / "list.csv"
    f.write_text("Project;Demo\nLuminaire;Quantity;P [W];Φ [lm]\nLUM_A;3;18,5;2100\nLUM_B;2;;\n",
                 encoding="utf-8")
    r = dialux.read_luminaire_list(str(f))
    assert r["rows"][0] == {"type": "LUM_A", "qty": 3.0, "watts": 18.5, "lumens": 2100.0}
    assert r["missing_watts"] == ["LUM_B"]
    assert dialux.specs_from_list(r)["lum_a"]["watts"] == 18.5


def test_luminaire_list_xlsx_and_column_override(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item", "Wattage", "Count"])
    ws.append(["LUM_A", 20, 3])
    f = tmp_path / "list.xlsx"
    wb.save(f)
    with pytest.raises(ValueError, match="Cannot find"):
        dialux.read_luminaire_list(str(f))
    r = dialux.read_luminaire_list(str(f), columns={"type": "Item", "qty": "Count"})
    assert r["rows"] == [{"type": "LUM_A", "qty": 3.0, "watts": 20.0, "lumens": None}]


def _align(export):
    return dialux.fit_two_points(to_dialux_frame(0, 0), to_dialux_frame(6000, 0), (0, 0), (6000, 0))


def test_import_apply_schedule_and_reimport(env, export):
    s = env["server"]
    t = _align(export)
    out = s.dialux_import("demo", "4F", export, block_names=["LUM_A", "LUM_B", "EXIT_SIGN"],
                          transform=t, type_map={"EXIT_SIGN": "emergency"},
                          watts_by_type={"LUM_A": 18, "EXIT_SIGN": 3}, circuit="4F-L01",
                          label_attribute="LABEL")
    assert out["found_in_file"] == 6 and out["by_block"] == {"LUM_A": 3, "LUM_B": 2, "EXIT_SIGN": 1}
    assert out["outside_all_rooms"] == 0
    assert out["types_without_watts"] == {"LUM_B": 2}
    assert any("No wattage" in a for a in out["assumptions"])
    s.apply_changes("demo", "4F", out["changeset_id"])

    devs = {d["id"]: d for d in s.list_devices("demo", "4F")["devices"]}
    lum = [d for d in devs.values() if d["type"] == "luminaire"]
    assert len(lum) == 5 and {d["room"] for d in lum} == {"4-A", "4-B"}
    first = next(d for d in lum if abs(d["x"] - 1500) < 1 and abs(d["y"] - 1250) < 1)
    assert first["watts"] == 18 and first["luminaire_type"] == "LUM_A"
    assert first["dialux_label"] == "L1" and first["source"].startswith("dialux:dialux.dxf:")
    assert any(d["type"] == "emergency" for d in devs.values())

    # schedule: LUM_B has no wattage, so the total is flagged incomplete, not guessed
    sch = s.lighting_schedule("demo", "4F")
    assert sch["total_luminaires"] == 6 and sch["installed_watts"] == 3 * 18 + 3
    assert sch["types_without_watts"] == {"LUM_B": 2} and sch["complete"] is False
    r_a = next(r for r in sch["rooms"] if r["room"] == "4-A")
    assert r_a["area_m2"] == 30.0 and r_a["w_per_m2"] is None  # incomplete room: no W/m2
    sch2 = s.lighting_schedule("demo", "4F", watts_by_type={"LUM_B": 25})
    assert sch2["complete"] and sch2["installed_watts"] == 3 * 18 + 3 + 2 * 25

    # re-import: nothing duplicated, nothing to do
    again = s.dialux_import("demo", "4F", export, block_names=["LUM_A", "LUM_B", "EXIT_SIGN"],
                            transform=t, type_map={"EXIT_SIGN": "emergency"},
                            watts_by_type={"LUM_A": 18, "EXIT_SIGN": 3}, circuit="4F-L01",
                            label_attribute="LABEL")
    assert again["operations"] == 0
    # new wattage for an existing luminaire updates it in place
    upd = s.dialux_import("demo", "4F", export, block_names=["LUM_B"], transform=t,
                          watts_by_type={"LUM_B": 25})
    assert upd["operations"] == 2 and all(c.startswith("~") for c in upd["changes"])


def test_replace_previous_removes_luminaires_no_longer_in_the_export(env, tmp_path, export):
    s = env["server"]
    t = _align(export)
    first = s.dialux_import("demo", "4F", export, block_names=["LUM_A", "LUM_B"], transform=t)
    s.apply_changes("demo", "4F", first["changeset_id"])
    doc = ezdxf.readfile(export)
    victim = next(e for e in doc.modelspace().query("INSERT") if e.dxf.name == "LUM_B")
    doc.modelspace().delete_entity(victim)
    smaller = str(tmp_path / "smaller.dxf")
    doc.saveas(smaller)
    keep = s.dialux_import("demo", "4F", smaller, block_names=["LUM_A", "LUM_B"], transform=t)
    assert keep["removed_previous"] == []
    drop = s.dialux_import("demo", "4F", smaller, block_names=["LUM_A", "LUM_B"], transform=t,
                           replace_previous=True)
    assert len(drop["removed_previous"]) == 1
    s.apply_changes("demo", "4F", drop["changeset_id"])
    assert len([d for d in s.list_devices("demo", "4F")["devices"]]) == 4


def test_misaligned_import_is_flagged_not_hidden(env, export):
    s = env["server"]
    # no transform: metre values land inside a millimetre room, so only the scale check catches it
    out = s.dialux_import("demo", "4F", export, block_names=["LUM_A", "LUM_B"])
    assert out["outside_all_rooms"] == 0 and out["extent_check"]["suspicious"]
    assert any("SCALE/UNITS LOOK WRONG" in a for a in out["assumptions"])
    far = s.dialux_import("demo", "4F", export, block_names=["LUM_A"], dx=1_000_000, scale=1000)
    assert far["outside_all_rooms"] == 3
    assert any("outside every room" in a for a in far["assumptions"])
    good = s.dialux_import("demo", "4F", export, block_names=["LUM_A", "LUM_B"],
                           transform=_align(export))
    assert not good["extent_check"]["suspicious"]
    with pytest.raises(ValueError, match="No matching blocks"):
        s.dialux_import("demo", "4F", export, block_names=["NOPE"])


def test_live_import_is_one_undo_step_and_dry_run_first(export, monkeypatch, tmp_path):
    app = App()
    live = LiveSession(PlainCom(app), tmp_path / "st")
    monkeypatch.setattr(server, "_LIVE", live)
    t = _align(export)
    dry = server.live_import_luminaires("4F", export, block_names=["LUM_A", "LUM_B"], transform=t)
    assert dry["dry_run"] and dry["found_in_file"] == 5
    assert not [e for e in app.ActiveDocument.ModelSpace]  # nothing drawn
    done = server.live_import_luminaires("4F", export, block_names=["LUM_A", "LUM_B"], transform=t,
                                         watts_by_type={"LUM_A": 18, "LUM_B": 25}, dry_run=False)
    assert done["created"] == 5 and done["already_present"] == 0
    assert len(app.ActiveDocument._marks) == 1  # ONE undo mark for the whole import
    lines = [e for e in app.ActiveDocument.ModelSpace if e.ObjectName == "AcDbLine"]
    assert len(lines) == 5 * 6  # reference-style luminaire = 6 lines
    listed = live.list_devices()["devices"]
    assert sorted(d["watts"] for d in listed) == [18, 18, 18, 25, 25]
    assert all(d["luminaire_type"] in ("LUM_A", "LUM_B") for d in listed)
    again = server.live_import_luminaires("4F", export, block_names=["LUM_A", "LUM_B"],
                                          transform=t, dry_run=False)
    assert again["created"] == 0 and again["already_present"] == 5
    server.live_set_mode("schematic")
    with pytest.raises(ValueError, match="Schematic mode is on"):
        server.live_import_luminaires("4F", export, block_names=["LUM_A"], dry_run=False)
