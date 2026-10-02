"""DXF schedule reading on small DXF files generated here with ezdxf (no real project files, no API)."""

import io
import math
import os
import time
from pathlib import Path

import ezdxf
import pytest

from beam_checker import cad_reader as cr
from beam_checker import text_layer as tl
from tests.test_text_layer import COLS, HEADER_Y, ROWS, synthetic_page_words

H = 7  # text height, as in the text-layer tests


def cells(rows=ROWS, cols=COLS):
    """(text, centre x, bottom y) of every header part and cell, laid out like the text-layer test page."""
    out = []
    for label, x in cols:
        parts = label.split("|")
        for i, part in enumerate(parts):
            out.append((part, x, HEADER_Y + (len(parts) - 1 - i) * 11))
    for r, row in enumerate(rows):
        for (_, x), val in zip(cols, row):
            if val:
                out.append((val, x, HEADER_Y - 30 - r * 28))
    return out


def turn(x, y, deg):
    a = math.radians(deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def add_text(space, text, x, y, rotation=0.0):
    """Centred TEXT, bottom-aligned at y (CAD schedules centre text in its cell)."""
    px, py = turn(x, y, rotation)
    space.add_text(text, dxfattribs={"height": H, "rotation": rotation, "halign": 1, "valign": 1,
                                     "insert": (px, py), "align_point": (px, py)})


def add_schedule(space, rows=ROWS, dx=0.0, dy=0.0, rotation=0.0, cols=COLS):
    for text, x, y in cells(rows, cols):
        add_text(space, text, x + dx, y + dy, rotation)


def to_bytes(doc):
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode("utf-8")


def new_doc():
    return ezdxf.new("R2018")


def values(recs):
    return [[r[f] for f in tl.FIELDS] for r in recs]


def expected_rows(rows=ROWS):
    """ROWS in record-field order (FIELDS), for comparison."""
    order = ["beam_mark", "size", "T1", "T2", "T3", "B1", "B2", "B3", "side_bars", "S1", "S2", "S3", "link_type",
             "remark"]
    return [[dict(zip(order, row))[f] for f in tl.FIELDS] for row in rows]


# ------------------------------------------------------------------ reading text

def test_plain_text_grid():
    doc = new_doc()
    add_schedule(doc.modelspace())
    reading = cr.read_dxf(to_bytes(doc))
    assert len(reading.tables) == 1 and reading.warnings == []
    t = reading.tables[0]
    assert (t.layout_no, t.layout_name, t.rotation) == (1, "Model", 0)
    assert values(t.table.records) == expected_rows()
    assert t.table.records[0]["source_note"] == "CAD text, layout 1, table row 1"
    assert all(not r["flags"] for r in t.table.records)


def test_verbatim_arrows_dashes_and_sums():
    rows = [["B101-1", "200x450", "2H13+2H13", "→", "←", "-", "—", "2H16", "-", "H10-150", "→", "H10-150",
             "Normal", "SEE NOTE 3"],
            ["B101-2", "200x225/175", "3H16", "2H13+2H13", "3H16", "2H16", "2H16", "2H16", "H10-250", "2H10-150",
             "2H10-200", "2H10-150", "Normal", ""]]
    doc = new_doc()
    add_schedule(doc.modelspace(), rows)
    recs = cr.read_dxf(to_bytes(doc)).tables[0].table.records
    assert values(recs) == expected_rows(rows)


def test_mtext_formatting_is_stripped_and_flagged():
    doc = new_doc()
    msp = doc.modelspace()
    for text, x, y in cells():
        if (text, x) in (("Top", 300), ("Left", 300)):
            continue                                   # replaced by the two-line MTEXT header below
        if text == "2H16+2H13":
            text = r"{\fArial|b1|i0;2H16+2H13}"          # font code around a cell value
        elif text == "3H20":
            text = r"3H20 \S1/2;"                        # stacked fraction
        msp.add_mtext(text, dxfattribs={"char_height": H, "attachment_point": 8, "insert": (x, y)})
    # a two-line header cell in one MTEXT ("Top" above "Left"), bottom-centre at the lower line
    msp.add_mtext(r"Top\PLeft", dxfattribs={"char_height": H, "attachment_point": 8, "insert": (300, HEADER_Y),
                                            "line_spacing_factor": 11 / (H * 5 / 3)})
    recs = cr.read_dxf(to_bytes(doc)).tables[0].table.records
    assert recs[0]["B2"] == "2H16+2H13" and recs[0]["flags"] == [cr.FONT_FLAG]           # font code only
    assert recs[1]["T3"] == "3H20 1/2" and recs[1]["flags"] == [cr.FORMATTING_FLAG]     # stacked fraction
    assert recs[0]["T1"] == "3H16" and recs[2]["flags"] == []


@pytest.mark.parametrize("raw, flags", [
    (r"3H16", ()),
    (r"Top\PLeft", ()),                                   # paragraph break is structure, not formatting
    (r"{\fArial|b1|i0;3H16}", (cr.FONT_FLAG,)),
    (r"\H2.5x;\C1;\W0.8;3H16", (cr.FONT_FLAG,)),
    (r"\L3H16\l", (cr.FORMATTING_FLAG,)),                 # underline
    (r"{\fArial;\O3H16}", (cr.FORMATTING_FLAG,)),         # overline beats font-only
    (r"3H20 \S1/2;", (cr.FORMATTING_FLAG,)),              # stacked fraction
    (r"C:\\path", ()),                                    # escaped backslash is text
])
def test_mtext_codes_review_or_font_only(raw, flags):
    assert cr._mtext_flags(raw) == flags


def test_text_underline_code_is_flagged_but_symbols_are_not():
    doc = new_doc()
    rows = [list(ROWS[0]), list(ROWS[1])]
    rows[0][2] = "%%u3H16"
    rows[1][1] = "%%c200"
    add_schedule(doc.modelspace(), rows)
    recs = cr.read_dxf(to_bytes(doc)).tables[0].table.records
    assert recs[0]["T1"] == "3H16" and recs[0]["flags"] == [cr.FORMATTING_FLAG]
    assert recs[1]["size"] == "Ø200" and recs[1]["flags"] == []


def test_attribute_text_in_blocks():
    doc = new_doc()
    blk = doc.blocks.new("CELL")
    blk.add_attdef("VAL", (0, 0), dxfattribs={"height": H})
    msp = doc.modelspace()
    for text, x, y in cells():
        ins = msp.add_blockref("CELL", (x, y))
        ins.add_attrib("VAL", text, (x, y), dxfattribs={"height": H, "halign": 1, "valign": 1, "align_point": (x, y)})
    reading = cr.read_dxf(to_bytes(doc))
    assert values(reading.tables[0].table.records) == expected_rows()


def test_plain_text_inside_a_block_reference():
    doc = new_doc()
    blk = doc.blocks.new("SCHEDULE")
    add_schedule(blk, dx=-1000, dy=-1000)                # block drawn around its own base point
    doc.modelspace().add_blockref("SCHEDULE", (1000, 1000))
    assert values(cr.read_dxf(to_bytes(doc)).tables[0].table.records) == expected_rows()


@pytest.mark.parametrize("rotation", [90, 270])
def test_rotated_schedule(rotation):
    doc = new_doc()
    add_schedule(doc.modelspace(), rotation=rotation)
    add_text(doc.modelspace(), "GENERAL NOTES", 80, 100)   # unrotated text elsewhere on the sheet
    t = cr.read_dxf(to_bytes(doc)).tables[0]
    assert t.rotation == rotation and values(t.table.records) == expected_rows()
    pos = t.position(t.table.records[0])
    _, v = turn(pos["x"], pos["y"], -rotation)          # back to the schedule's own frame: row 1 centre height
    assert abs(v - (HEADER_Y - 30 + H / 2)) < 1 and pos["layout"] == "Model"


def table_object_dxf():
    """A DXF with a CAD table object (ACAD_TABLE) whose cells are MTEXT in its anonymous *T block.

    ezdxf cannot create table objects, so the entity is written by hand next to its block.
    """
    doc = new_doc()
    blk = doc.blocks.new_anonymous_block(type_char="T")
    for text, x, y in cells():
        blk.add_mtext(text, dxfattribs={"char_height": H, "attachment_point": 8, "insert": (x - 500, y - 500)})
    owner = doc.modelspace().block_record_handle
    tags = ["0", "ACAD_TABLE", "5", "FFF0", "330", owner, "100", "AcDbEntity", "8", "0", "100", "AcDbBlockReference",
            "2", blk.name, "10", "500.0", "20", "500.0", "30", "0.0", "100", "AcDbTable", "280", "0",
            "343", blk.block_record_handle, "11", "1.0", "21", "0.0", "31", "0.0", "90", "22",
            "91", str(len(ROWS) + 2), "92", str(len(COLS))]
    text = to_bytes(doc).decode("utf-8")
    at = text.index("ENTITIES\n") + len("ENTITIES\n")
    return (text[:at] + "\n".join(tags) + "\n" + text[at:]).encode("utf-8")


def test_table_object():
    reading = cr.read_dxf(table_object_dxf())
    assert len(reading.tables) == 1
    assert values(reading.tables[0].table.records) == expected_rows()


# ------------------------------------------------------------------ table objects with cell lines (as CAD writes them)

GRID_H = 250                                   # text height in drawing units (mm), as in a real schedule
FONT = r"\fCalibri|b0|i0|c0;"
ARROWS = r"\fWingdings 3|b0|i0|c2;"           # in Wingdings 3, "!" is drawn as a left arrow and '"' a right one
# column widths: mark, size, T1-T3, B1-B3, side bars, link type, S1-S3, remark, and a helper column without a header
GRID_COLS = (1700, 2500, 2000, 3000, 2800, 2000, 3000, 2000, 2000, 2000, 2000, 2000, 2000, 3000, 3000)
TITLE_H, HEAD_H, ROW_H = 530, 390, 506


def grid_rows_single():
    """Rows as typed in the cells: (font code, text) for symbol cells, plain text otherwise; None = empty."""
    L, R = (ARROWS, "!"), (ARROWS, '"')
    return [
        ["B101", "200x500", L, "2H16", R, L, "2H20", R, "-", "A1", L, "H10-200", R, None, None],
        ["B102a", "300x600 U/S 400", "3H20", "3H20+3H20", "-", L, "3H16", R, "H10-250 E.F.", "A2", L, "2H10-150",
         "-", "CANTILEVER", None],
        ["B103", "200x400", "2H13", "2H13", "2H13", "2H13", "2H13", "2H13", "-", "A1", "H10-200", "H10-200",
         "H10-200", (FONT, "!"), None],                     # "!" in an ordinary font stays "!"
        [None, "#N/A", None, None, None, None, None, None, None, None, None, None, None, None, None],
    ]


def grid_rows_continuous():
    L, R = (ARROWS, "!"), (ARROWS, '"')
    return [
        ["B201-1", "400x900", L, "4H25+4H25+2H25", "4H25", "4H20", "4H20+4H20", "4H20", "H16-250 E.F.", "A2", L,
         "2H10-200", R, None, "B201"],
        ["B201-2", "400x900", "4H25", "4H25", (r"\fWebdings|b0|i0|c2;", "x"), L, "4H20", R, "H16-250 E.F.", "A2",
         "2H10-150", "2H10-200", "-", "CANTILEVER", "B201"],
    ]


def add_grid_table(doc, title, rows, size_header="SIZE WxD)"):
    """A CAD table object as CAD software writes one: an anonymous *T block of cell lines and one MTEXT per cell
    (centre-anchored at the cell centre, with the cell width; empty cells keep an empty MTEXT), with a title row,
    a group header row (MAIN REIFORCEMENT / LINKS) and a sub-header row (T1-T3, B1-B3) under TOP / BOTTOM BARS."""
    blk = doc.blocks.new_anonymous_block(type_char="T")
    xs = [0]
    for w in GRID_COLS:
        xs.append(xs[-1] + w)
    ys = [0, -TITLE_H, -TITLE_H - HEAD_H, -TITLE_H - 2 * HEAD_H, -TITLE_H - 3 * HEAD_H]
    for _ in rows:
        ys.append(ys[-1] - ROW_H)

    def cell(text, c0, c1, y_top, y_bottom, font=FONT):
        blk.add_mtext("{" + font + (text or "") + "}", dxfattribs={
            "char_height": GRID_H, "attachment_point": 5, "width": xs[c1 + 1] - xs[c0] - 12,
            "insert": ((xs[c0] + xs[c1 + 1]) / 2, (y_top + y_bottom) / 2)})

    for y in ys:
        blk.add_line((xs[0], y), (xs[-1], y))
    for i, x in enumerate(xs):                                   # lines inside merged header cells start lower
        top = ys[3] if i in (3, 4, 6, 7) else ys[2] if i in (11, 12, 13) else ys[1] if 0 < i < len(xs) - 1 else ys[0]
        blk.add_line((x, top), (x, ys[-1]))
    cell(title, 0, 13, ys[0], ys[1])
    cell("MAIN REIFORCEMENT", 2, 7, ys[1], ys[2])
    cell("LINKS", 9, 12, ys[1], ys[2])
    for text, c in (("BEAM MARK", 0), (size_header, 1), ("SIDE BARS", 8), ("Remark", 13)):
        cell(text, c, c, ys[2], ys[3])
    cell("TOP BARS", 2, 4, ys[2], ys[3])
    cell("BOTTOM BARS", 5, 7, ys[2], ys[3])
    for text, c in (("TYPE", 9), ("S1", 10), ("S2", 11), ("S3", 12)):        # centred on the line between rows
        cell(text, c, c, ys[2], ys[4])
    for text, c in (("T1", 2), ("T2", 3), ("T3", 4), ("B1", 5), ("B2", 6), ("B3", 7)):
        cell(text, c, c, ys[3], ys[4])
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            font, text = value if isinstance(value, tuple) else (FONT, value)
            cell(text, c, c, ys[4 + r], ys[5 + r], font)
    return blk


def grid_table_dxf(tables, size_header="SIZE WxD)"):
    """DXF with one ACAD_TABLE entity per (title, rows, insert point); the entity is written by hand (see above)."""
    doc = new_doc()
    blocks = [(add_grid_table(doc, title, rows, size_header), at) for title, rows, at in tables]
    owner = doc.modelspace().block_record_handle
    entities = []
    for n, (blk, (x, y)) in enumerate(blocks):
        entities += ["0", "ACAD_TABLE", "5", f"FFF{n}", "330", owner, "100", "AcDbEntity", "8", "0",
                     "100", "AcDbBlockReference", "2", blk.name, "10", str(x), "20", str(y), "30", "0.0",
                     "100", "AcDbTable", "280", "0", "343", blk.block_record_handle, "11", "1.0", "21", "0.0",
                     "31", "0.0", "90", "22", "91", "0", "92", "0"]
    text = to_bytes(doc).decode("utf-8")
    at = text.index("ENTITIES\n") + len("ENTITIES\n")
    return (text[:at] + "\n".join(entities) + "\n" + text[at:]).encode("utf-8")


def two_grid_tables():
    return grid_table_dxf([("SINGLE SPAN BEAM SCHEDULE", grid_rows_single(), (10000, -5000)),
                           ("CONTINUOUS SPAN BEAM SCHEDULE", grid_rows_continuous(), (60000, -5000))])


def test_table_object_with_cell_lines_is_read_by_its_grid():
    reading = cr.read_dxf(two_grid_tables())
    single, cont = reading.tables
    assert (single.kind, cont.kind) == ("single span", "continuous span")
    assert "table 1 (single span) · 3 rows" in single.label() and "table 2 (continuous span) · 2 rows" in cont.label()
    rec = single.table.records[0]
    assert {f: rec[f] for f in tl.FIELDS} == {
        "beam_mark": "B101", "size": "200x500", "T1": "←", "T2": "2H16", "T3": "→", "B1": "←", "B2": "2H20",
        "B3": "→", "side_bars": "-", "link_type": "A1", "S1": "←", "S2": "H10-200", "S3": "→", "remark": ""}
    assert rec["source_note"] == "CAD text, layout 1, single span table, row 1"
    b102a = single.table.records[1]
    assert (b102a["size"], b102a["T3"], b102a["S3"], b102a["remark"]) == ("300x600 U/S 400", "-", "-", "CANTILEVER")
    # wide neighbouring cells stay apart
    b201 = cont.table.records[0]
    assert (b201["T2"], b201["T3"], b201["B2"]) == ("4H25+4H25+2H25", "4H25", "4H20+4H20")
    assert cont.table.records[1]["source_note"] == "CAD text, layout 1, continuous span table, row 2"


def test_grid_symbol_fonts_and_flags():
    single, cont = cr.read_dxf(two_grid_tables()).tables
    b103 = single.table.records[2]
    assert b103["remark"] == "!" and b103["flags"] == [cr.FONT_FLAG]           # ordinary font: never mapped
    assert all(r["flags"] == [cr.FONT_FLAG] for r in single.table.records)     # font codes are informational only
    b201_2 = cont.table.records[1]
    assert b201_2["T3"] == "x" and cr.SYMBOL_FLAG in b201_2["flags"]           # unknown symbol font: kept, flagged


def test_grid_skips_rows_without_a_mark_and_ignores_a_column_without_a_header():
    single, cont = cr.read_dxf(two_grid_tables()).tables
    assert [r["beam_mark"] for r in single.table.records] == ["B101", "B102a", "B103"]
    assert "row without a beam mark skipped (after B103): #N/A" in single.table.notes
    assert "a column without a header was ignored (2 cell(s))" in cont.table.notes
    assert all("B201" not in r["remark"] for r in cont.table.records)
    assert cont.table.unmapped_headers == [] and "remark" in cont.table.columns


def test_grid_header_variants_and_counting():
    data = grid_table_dxf([("BEAM SCHEDULE", grid_rows_single(), (0, 0))])
    reading = cr.read_dxf(data)
    assert reading.tables[0].kind == "" and reading.tables[0].table.records[0]["source_note"] == \
        "CAD text, layout 1, table row 1"
    # cells with text: title + 18 headers, then 13 + 14 + 14 + 1 in the rows; empty cells are neither read nor counted
    assert reading.table_cells == 1 + 18 + 13 + 14 + 14 + 1
    lines = (len(GRID_COLS) + 1) + (5 + len(grid_rows_single()))
    assert reading.entities_read == 1 + reading.table_cells + lines          # the table object, its cells and lines
    for size in ("SIZE (WxD)", "SIZE"):
        data = grid_table_dxf([("BEAM SCHEDULE", grid_rows_single(), (0, 0))], size_header=size)
        assert cr.read_dxf(data).tables[0].table.records[0]["size"] == "200x500"


def test_grid_dxf_rows_feed_the_review_table():
    from beam_checker import drawing_reader as dr

    reading = cr.read_dxf(two_grid_tables())
    ex = dr.read_cad(reading.tables)
    t = ex.table
    assert sorted(t["Beam mark"]) == ["B101", "B102a", "B103", "B201-1", "B201-2"]
    assert "#N/A" in ex.page_notes[1] and "table 2: " in ex.page_notes[1]
    row = t[t["Beam mark"] == "B101"].iloc[0]
    assert (row["T1"], row["T3"], row["S1"], row["S3"]) == ("←", "→", "←", "→")


def test_symbol_font_mapping_needs_the_symbol_font():
    assert cr._symbol_text("!", ["Wingdings 3"]) == ("←", ())
    assert cr._symbol_text('"', ["WINGDNG3.TTF"]) == ("→", ())
    assert cr._symbol_text("!", ["Calibri"]) == ("!", ())
    assert cr._symbol_text("!", ["romans.shx"]) == ("!", ())
    assert cr._symbol_text("x", ["Wingdings 3"]) == ("x", (cr.SYMBOL_FLAG,))       # glyph not in the table
    assert cr._symbol_text("!", ["Wingdings"]) == ("!", (cr.SYMBOL_FLAG,))         # other symbol font
    assert cr._symbol_text("!", ["Wingdings 3", "Calibri"]) == ("!", (cr.SYMBOL_FLAG,))   # mixed fonts in a cell


# ------------------------------------------------------------------ choosing and reporting

def test_two_schedules_in_one_file_best_match_first():
    doc = new_doc()
    add_schedule(doc.modelspace())
    sheet = doc.layouts.new("Sheet 2")                   # after the default "Layout1", which stays empty
    fewer_cols = [c for c in COLS if not c[0].startswith("Stirrups")]
    fewer_rows = [[v for (lbl, _), v in zip(COLS, row) if not lbl.startswith("Stirrups")] for row in ROWS[:2]]
    add_schedule(sheet, fewer_rows, cols=fewer_cols)
    reading = cr.read_dxf(to_bytes(doc))
    assert [(t.layout_no, t.layout_name) for t in reading.tables] == [(1, "Model"), (3, "Sheet 2")]
    assert reading.best() is reading.tables[0]
    assert "Layout 'Sheet 2' · table 1 · 2 rows" in reading.tables[1].label()


def test_unmatched_headers_are_reported():
    doc = new_doc()
    renamed = [(lbl.replace("Top", "Upper").replace("Bottom", "Lower"), x) for lbl, x in COLS]
    add_schedule(doc.modelspace(), cols=renamed)
    reading = cr.read_dxf(to_bytes(doc))
    assert reading.tables == []
    assert "Mark" in reading.found_headers and "Upper Left" in reading.found_headers


def test_no_text_only_lines_warns_about_exploded_text():
    doc = new_doc()
    for i in range(20):
        doc.modelspace().add_line((0, i), (10, i))
    reading = cr.read_dxf(to_bytes(doc))
    assert reading.tables == [] and any("exploded into lines" in w for w in reading.warnings)


def test_xref_is_not_followed_and_warned():
    doc = new_doc()
    add_schedule(doc.modelspace())
    doc.add_xref_def("other.dxf", "XR")
    doc.modelspace().add_blockref("XR", (0, 0))
    reading = cr.read_dxf(to_bytes(doc))
    assert len(reading.tables) == 1 and any("external reference" in w for w in reading.warnings)


def test_embedded_object_is_warned():
    doc = new_doc()
    add_schedule(doc.modelspace())
    text = to_bytes(doc).decode("utf-8")
    ole = "\n".join(["0", "OLE2FRAME", "5", "FFF1", "330", doc.modelspace().block_record_handle,
                     "100", "AcDbEntity", "8", "0", "100", "AcDbOle2Frame", "70", "2", "3", "Excel",
                     "10", "0.0", "20", "0.0", "30", "0.0", "11", "10.0", "21", "10.0", "31", "0.0",
                     "71", "2", "72", "0", "90", "0", "1", "OLE"]) + "\n"
    at = text.index("ENTITIES\n") + len("ENTITIES\n")
    reading = cr.read_dxf((text[:at] + ole + text[at:]).encode("utf-8"))
    assert any("embedded object" in w for w in reading.warnings)


# ------------------------------------------------------------------ rejected files and limits

def test_dwg_is_rejected_with_save_as_dxf_message():
    for version in (b"AC1015", b"AC1032"):
        with pytest.raises(cr.CadReadError) as e:
            cr.read_dxf(version + b"\x00" * 100)
        assert str(e.value) == cr.DWG_MESSAGE
    assert cr.is_dwg(b"AC1027...") and not cr.is_dwg(b"  0\nSECTION")


def test_oversize_file_is_rejected():
    with pytest.raises(cr.CadReadError) as e:
        cr.read_dxf(b"0" * 2_100_000, cr.CadLimits(max_mb=2))
    assert "limit for this app is 2 MB" in str(e.value) and "schedule sheet" in str(e.value)
    assert "MAX_DXF_MB" in str(e.value)


def test_entity_limit():
    doc = new_doc()
    add_schedule(doc.modelspace())
    with pytest.raises(cr.CadReadError) as e:
        cr.read_dxf(to_bytes(doc), cr.CadLimits(max_entities=50))
    assert "more than 50 entities" in str(e.value) and "MAX_DXF_ENTITIES" in str(e.value)


def test_time_limit(monkeypatch):
    def slow(stream):
        time.sleep(2)

    monkeypatch.setattr(cr.recover, "read", slow)
    with pytest.raises(cr.CadReadError) as e:
        cr.read_dxf(b"  0\nSECTION\n", cr.CadLimits(timeout_s=1))
    assert "longer than 1 s" in str(e.value) and "DXF_TIMEOUT_SECONDS" in str(e.value)


def test_not_a_dxf_gives_a_clear_message():
    with pytest.raises(cr.CadReadError) as e:
        cr.read_dxf(b"this is not a drawing")
    assert "could not be read as a DXF" in str(e.value)


# ------------------------------------------------------------------ same table as the PDF text layer

def test_same_schedule_from_dxf_and_text_layer_gives_the_same_table():
    """One schedule, given as positioned PDF words and as a generated DXF, gives identical records."""
    from_pdf = tl.find_tables(synthetic_page_words())[0]
    doc = new_doc()
    add_schedule(doc.modelspace())
    add_text(doc.modelspace(), "BEAM SCHEDULE", 600, HEADER_Y + 40)
    add_text(doc.modelspace(), "GENERAL NOTES", 80, 100)
    from_dxf = cr.read_dxf(to_bytes(doc)).tables[0].table
    assert sorted(from_dxf.columns) == sorted(from_pdf.columns)
    drop = ("row_box", "source_note")
    assert ([{k: v for k, v in r.items() if k not in drop} for r in from_dxf.records]
            == [{k: v for k, v in r.items() if k not in drop} for r in from_pdf.records])
    assert from_dxf.notes == from_pdf.notes and from_dxf.unmapped_headers == from_pdf.unmapped_headers


# ------------------------------------------------------------------ optional: a real DXF (never committed)

def _sample_dxf():
    if os.environ.get("SAMPLE_DXF"):
        return Path(os.environ["SAMPLE_DXF"])
    found = sorted((Path(__file__).resolve().parent.parent / "sample_data").glob("*.dxf"))
    return found[0] if found else None


@pytest.mark.skipif(not os.environ.get("RUN_SAMPLE_DXF"),
                    reason="optional: set RUN_SAMPLE_DXF=1 (and SAMPLE_DXF, or put a .dxf in sample_data/)")
def test_real_dxf_reads_a_schedule():
    path = _sample_dxf()
    if path is None or not path.exists():
        pytest.skip("no DXF found")
    limits = cr.CadLimits(max_mb=500, max_entities=5_000_000, timeout_s=300)
    reading = cr.read_dxf(path.read_bytes(), limits)
    for w in reading.warnings:
        print("warning:", w)
    for t in reading.tables:
        print(t.label())
    assert reading.tables, f"no schedule found; headers found: {reading.found_headers}"
    assert sum(len(t.table.records) for t in reading.tables) > 0


# ------------------------------------------------------------------ into the review table

def test_cad_rows_feed_the_review_table_like_text_layer_rows():
    import zipfile

    from beam_checker import drawing_reader as dr

    doc = new_doc()
    sheet = doc.layouts.new("Schedule sheet")
    rows = [list(ROWS[0]), list(ROWS[1]), list(ROWS[2])]
    add_schedule(sheet, rows)
    reading = cr.read_dxf(to_bytes(doc))
    ex = dr.read_cad(reading.tables)
    t = ex.table
    assert ex.method == dr.READ_CAD and ex.requests == 0
    assert set(t["Read from"]) == {"CAD text"} and set(t["Page"]) == {3}
    assert dr.tick_status(t) == (0, 0) and dr.ready_to_compare(t)          # ticks optional, as for the text layer
    assert sorted(t["Beam mark"]) == ["B101-1", "B101-2", "B102a"]
    assert t.loc[t["Beam mark"] == "B102a", "Size"].item() == "200x225/175"
    # layout name and coordinates are position data only: not in the table, the notes or the Excel download
    pos = ex.boxes[t.loc[t["Beam mark"] == "B101-1", "Row ID"].item()]
    assert pos["layout"] == "Schedule sheet" and pos["layout_no"] == 3 and {"x", "y"} <= set(pos)
    assert "Schedule sheet" not in t.to_csv()
    xlsx = zipfile.ZipFile(io.BytesIO(dr.table_to_type2_excel(t)))
    assert not any(b"Schedule sheet" in xlsx.read(n) for n in xlsx.namelist())
    # the comparison input and the assistant summary work as for the text layer
    assert len(dr.table_to_records(t)) == 3
    summary = dr.schedule_summary(t, ex.method, [], [], 3, 3)
    assert summary["ai_used_for_reading"] is False and summary["page_means"] == "layout number in the DXF file"


def test_formatting_flag_puts_the_row_on_the_review_list():
    from beam_checker import drawing_reader as dr

    doc = new_doc()
    rows = [list(ROWS[0]), list(ROWS[1])]
    rows[1][2] = "%%u3H16"
    add_schedule(doc.modelspace(), rows)
    t = dr.read_cad(cr.read_dxf(to_bytes(doc)).tables).table
    flagged = t[t["Beam mark"] == "B101-2"].iloc[0]
    assert flagged["Flags"] == "formatting_removed" and flagged["Review"] == "⚠ check"
    assert t.iloc[0]["Beam mark"] == "B101-2"                              # rows to check come first


def test_font_only_flag_is_shown_but_not_highlighted():
    from beam_checker import drawing_reader as dr

    doc = new_doc()
    rows = [list(ROWS[0]), list(ROWS[1])]
    rows[1][1] = "200x450"
    add_schedule(doc.modelspace(), rows)
    reading = cr.read_dxf(to_bytes(doc))
    reading.tables[0].table.records[1]["flags"] = [cr.FONT_FLAG]
    t = dr.read_cad(reading.tables).table
    row = t[t["Beam mark"] == "B101-2"].iloc[0]
    assert row["Flags"] == "font_codes_removed" and row["Review"] == ""


def test_cad_only_flag_is_not_offered_to_the_ai():
    from beam_checker import drawing_reader as dr

    allowed = dr.RECORD_SCHEMA["properties"]["flags"]["items"]["enum"]
    assert "formatting_removed" not in allowed and "possible_typo" in allowed


def test_exact_symbol_arrows_in_end_columns_are_not_flagged_as_ditto():
    from beam_checker import drawing_reader as dr

    L, R = (ARROWS, "!"), (ARROWS, '"')
    rows = [
        ["B101", "200x500", L, "2H16", R, L, "2H20", R, "-", "A1", L, "2H10-200", R, None, None],     # exact arrows
        ["B102", "200x500", "2H16", L, "2H16", "2H16", "2H20", "2H16", "-", "A1", "2H10-200", "2H10-200",
         "2H10-200", None, None],                                     # symbol arrow in the middle column: flagged
        ["B103", "200x500", "→", "2H16", "←", "2H16", "2H20", "2H16", "-", "A1", "2H10-200", "2H10-200",
         "2H10-200", None, None],                                     # arrows typed as ordinary text: flagged
    ]
    t = dr.read_cad(cr.read_dxf(grid_table_dxf([("BEAM SCHEDULE", rows, (0, 0))])).tables).table
    flags = dict(zip(t["Beam mark"], t["Flags"]))
    assert "ditto_unconfirmed" not in flags["B101"] and t.loc[t["Beam mark"] == "B101", "Review"].item() == ""
    assert "ditto_unconfirmed" in flags["B102"] and "ditto_unconfirmed" in flags["B103"]
    b101 = t[t["Beam mark"] == "B101"].iloc[0]
    assert (b101["T1"], b101["T3"], b101["B1"], b101["B3"]) == ("←", "→", "←", "→")     # values unchanged
