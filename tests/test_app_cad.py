"""Drawing mode with a DXF in the real Streamlit page (generated DXF files, no AI calls)."""

from types import SimpleNamespace as NS
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

from beam_checker import cad_reader as cr
from tests.test_cad_reader import COLS, ROWS, add_schedule, new_doc, to_bytes


def run_with_upload(name, data):
    """Run the page in drawing mode with one uploaded drawing file (AppTest cannot upload files itself)."""
    real = st.file_uploader

    def uploader(label, *args, **kwargs):
        if kwargs.get("key") == "drawing_files":
            return [NS(name=name, getvalue=lambda: data)]
        return real(label, *args, **kwargs)

    with mock.patch.object(st, "file_uploader", side_effect=uploader):
        at = AppTest.from_file("../app.py", default_timeout=60)
        at.session_state["schedule_source"] = "Drawing (PDF, DXF or image)"
        at.run()
    return at


def two_schedules():
    doc = new_doc()
    add_schedule(doc.modelspace())
    fewer_cols = [c for c in COLS if not c[0].startswith("Stirrups")]
    add_schedule(doc.layouts.new("Sheet 2"),
                 [[v for (lbl, _), v in zip(COLS, row) if not lbl.startswith("Stirrups")] for row in ROWS[:2]],
                 cols=fewer_cols)
    return to_bytes(doc)


def test_dxf_is_read_without_ai_and_all_tables_are_chosen():
    at = run_with_upload("schedule.dxf", two_schedules())
    assert not at.exception
    assert any("CAD files:" in i.value and "schedule sheet only" in i.value for i in at.info)
    pick = at.multiselect[0]
    assert pick.label == "Schedule tables to read" and pick.value == [0, 1] and len(pick.options) == 2
    assert any("5 rows found" in s.value and "no AI involved" in s.value for s in at.success)
    assert any("Read from the DXF's text" in c.value for c in at.caption)
    table = at.dataframe[0].value                                     # the review table (data editor)
    assert set(table["Read from"]) == {"CAD text"}
    assert sorted(table["Beam mark"]) == ["B101-1", "B101-1", "B101-2", "B101-2", "B102a"]
    assert any(b.label == "▶ Run comparison (all beams)" for b in at.button)


def test_dwg_upload_shows_save_as_dxf_message():
    at = run_with_upload("schedule.dwg", b"AC1032" + b"\x00" * 64)
    assert not at.exception
    assert any(cr.DWG_MESSAGE in e.value for e in at.error)


def test_unmatched_headers_are_listed():
    doc = new_doc()
    add_schedule(doc.modelspace(), cols=[(lbl.replace("Top", "Upper").replace("Bottom", "Lower"), x) for lbl, x in COLS])
    at = run_with_upload("schedule.dxf", to_bytes(doc))
    msg = next(e.value for e in at.error if "No beam schedule table" in e.value)
    assert "'Upper Left'" in msg and "Top Left / Middle / Right" in msg


def test_dxf_with_a_pdf_is_refused():
    real = st.file_uploader

    def uploader(label, *args, **kwargs):
        if kwargs.get("key") == "drawing_files":
            return [NS(name="a.dxf", getvalue=lambda: b""), NS(name="b.pdf", getvalue=lambda: b"")]
        return real(label, *args, **kwargs)

    with mock.patch.object(st, "file_uploader", side_effect=uploader):
        at = AppTest.from_file("../app.py", default_timeout=60)
        at.session_state["schedule_source"] = "Drawing (PDF, DXF or image)"
        at.run()
    assert any("Upload one CAD file on its own" in e.value for e in at.error)


def test_assumed_leg_count_note_above_the_review_table():
    from tests.test_cad_reader import ARROWS, grid_table_dxf

    L, R = (ARROWS, "!"), (ARROWS, '"')
    rows = [["B101", "200x500", L, "2H16", R, L, "2H20", R, "-", "A1", L, "H10-200", R, None, None],
            ["B102", "200x500", L, "2H16", R, L, "2H20", R, "-", "A1", "H10-150", "H10-200", "H10-150", None, None],
            ["B103", "300x600", L, "3H16", R, L, "3H20", R, "-", "A2", L, "H10-200", R, None, None]]
    at = run_with_upload("schedule.dxf", grid_table_dxf([("BEAM SCHEDULE", rows, (0, 0))]))
    assert not at.exception
    notes = [i.value for i in at.info if "legs assumed" in i.value]
    assert notes == ["2 rows: link type A1 has no leg count; 2 legs assumed. Confirm against the drawing legend."]
    table = at.dataframe[0].value
    assert dict(zip(table["Beam mark"], table["Review"])) == {"B101": "", "B102": "", "B103": "⚠ check"}


def test_no_leg_count_note_when_legs_are_stated():
    at = run_with_upload("schedule.dxf", two_schedules())       # stirrups written with legs, or not of type A1
    assert not at.exception and not [i for i in at.info if "legs assumed" in i.value]
