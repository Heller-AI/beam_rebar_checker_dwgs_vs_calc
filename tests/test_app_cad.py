"""Schedule source "CAD file (DXF)" in the real Streamlit page (generated DXF files, no AI calls)."""

from types import SimpleNamespace as NS
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

from beam_checker import cad_reader as cr
from tests.test_cad_reader import COLS, ROWS, add_schedule, new_doc, to_bytes


CAD_MODE, DRAWING_MODE = "CAD file (DXF)", "Drawing PDF or image"


def run_with_upload(name, data, source=CAD_MODE, prokon=None):
    """Run the page with one uploaded schedule file in the given schedule source (AppTest cannot upload files).

    prokon: Prokon beams {mark: {span: requirements}} returned for an uploaded Prokon report, or None for no report.
    """
    at, patches = page_with_upload(name, data, source, prokon)
    try:
        at.run()
    finally:
        for p in patches:
            p.stop()
    return at


def page_with_upload(name, data, source=CAD_MODE, prokon=None):
    """(AppTest not yet run, patches) for tests that click; the patches stay active until the test ends."""
    import beam_checker

    real = st.file_uploader

    def uploader(label, *args, **kwargs):
        if kwargs.get("key") == "cad_file":
            return NS(name=name, getvalue=lambda: data)
        if kwargs.get("key") == "drawing_files":
            return [NS(name=name, getvalue=lambda: data)]
        if kwargs.get("key") == "prokon_pdf":
            # bytes unique per mocked report: the page caches the Prokon reading by file contents
            body = b"%PDF-prokon-cad " + repr(sorted((prokon or {}).items())).encode()
            return NS(name="prokon.pdf", getvalue=lambda: body) if prokon is not None else None
        return real(label, *args, **kwargs)

    patches = [mock.patch.object(st, "file_uploader", side_effect=uploader),
               mock.patch.object(beam_checker, "extract_all_beams_from_pdf", return_value=prokon or {})]
    for p in patches:
        p.start()
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.session_state["schedule_source"] = source
    return at, patches


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


def test_three_schedule_sources_and_the_dxf_option_is_labelled_free():
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.run()
    radio = at.radio(key="schedule_source")
    assert radio.options == ["Excel schedule", CAD_MODE, DRAWING_MODE] and radio.value == "Excel schedule"
    assert radio.proto.captions[1] == "No AI used, free"


def test_pdf_or_image_in_the_cad_option_points_to_the_drawing_option():
    for name in ("schedule.pdf", "schedule.png", "schedule.jpg"):
        at = run_with_upload(name, b"%PDF-1.7 not read")
        assert not at.exception
        assert any("not a DXF" in e.value and "Drawing PDF or image" in e.value for e in at.error)
        assert not at.dataframe                                            # nothing was read


def test_dxf_in_the_drawing_option_points_to_the_cad_option():
    for name in ("schedule.dxf", "schedule.dwg"):
        at = run_with_upload(name, to_bytes(new_doc()), source=DRAWING_MODE)
        assert not at.exception
        assert any("CAD file (DXF)" in e.value and "no AI" in e.value for e in at.error)
        assert not at.dataframe


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


def test_text_layer_pdf_in_the_drawing_option_reaches_the_review_table():
    from tests.test_cad_reader import cells
    from tests.test_text_layer import pdf_with_texts

    data = pdf_with_texts([(text, "Helvetica", x) for text, x, _ in cells()], ys=[y for _, _, y in cells()])
    at = run_with_upload("schedule.pdf", data, source=DRAWING_MODE)
    assert not at.exception
    assert any("rows found" in s.value and "PDF text layer" in s.value for s in at.success)
    table = at.dataframe[0].value
    assert set(table["Read from"]) == {"PDF text layer"} and sorted(table["Beam mark"]) == ["B101-1", "B101-2", "B102a"]
    assert any(b.label == "▶ Run comparison (all beams)" for b in at.button)


def test_app_name_in_the_header_and_the_browser_tab():
    name = "Beam Schedule Checker vs Calculation Report"
    with mock.patch.object(st, "set_page_config", wraps=st.set_page_config) as page_config:
        at = AppTest.from_file("../app.py", default_timeout=60)
        at.run()
    assert not at.exception
    assert at.title[0].value == f"🏗️ {name}"
    assert page_config.call_args.kwargs["page_title"] == name                         # browser tab


REQ = {"req_t1": 100.0, "req_b2": 100.0, "req_t3": 100.0, "req_asv_l": 0.1, "req_asv_m": 0.1, "req_asv_r": 0.1}


def tick_rows():
    from tests.test_cad_reader import ARROWS

    L, R = (ARROWS, "!"), (ARROWS, '"')
    return [["B101", "200x500", L, "2H16", R, L, "2H20", R, "-", "A1", L, "H10-200", R, None, None],   # A1: noted only
            ["B102", "200x500", L, "2H16", R, L, "2H20", R, "-", "A2", L, "H10-200", R, None, None],   # A2: highlighted
            ["B103", "200x500", L, "2H16", R, L, "2H20", R, "-", "A2", L, "2H10-200", R, None, None]]


def test_cad_ticks_never_block_run_and_tick_all_skips_highlighted_rows():
    from tests.test_cad_reader import grid_table_dxf

    data = grid_table_dxf([("BEAM SCHEDULE", tick_rows(), (0, 0))])
    at, patches = page_with_upload("schedule.dxf", data, prokon={m: {1: REQ} for m in ("B101", "B102", "B103")})
    try:
        at.run()
        assert not at.exception
        assert not next(b for b in at.button if b.label.startswith("▶ Run comparison")).disabled   # no ticks needed
        assert not any("Run comparison is disabled" in m.value for m in at.markdown)
        tick = next(b for b in at.button if b.label.startswith("☑ Tick all unflagged rows"))
        assert tick.label.endswith("(2)")
        tick.click().run()
        table = at.dataframe[0].value
        assert dict(zip(table["Beam mark"], table["Reviewed"])) == {"B102": False, "B101": True, "B103": True}
        assert not next(b for b in at.button if b.label.startswith("▶ Run comparison")).disabled
    finally:
        for p in patches:
            p.stop()


def test_text_layer_pdf_has_the_tick_all_button_and_run_is_enabled():
    from tests.test_cad_reader import cells
    from tests.test_text_layer import pdf_with_texts

    data = pdf_with_texts([(text, "Helvetica", x) for text, x, _ in cells()], ys=[y for _, _, y in cells()])
    at = run_with_upload("schedule.pdf", data, source=DRAWING_MODE,
                         prokon={m: {1: REQ, 2: REQ} for m in ("B101", "B102a")})
    assert not at.exception
    assert any(b.label.startswith("☑ Tick all unflagged rows") for b in at.button)
    assert not next(b for b in at.button if b.label.startswith("▶ Run comparison")).disabled


def test_a_real_duplicate_mark_blocks_run_and_says_why():
    from tests.test_cad_reader import grid_table_dxf

    rows = tick_rows()
    rows[2][0] = "B101"                                                      # the same mark twice
    at = run_with_upload("schedule.dxf", grid_table_dxf([("BEAM SCHEDULE", rows, (0, 0))]),
                         prokon={"B101": {1: REQ}, "B102": {1: REQ}})
    assert not at.exception
    assert next(b for b in at.button if b.label.startswith("▶ Run comparison")).disabled
    assert any("Run comparison is disabled because" in m.value and "appear more than once" in m.value
               for m in at.markdown)
