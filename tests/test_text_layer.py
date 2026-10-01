"""Text-layer table reading, review ticks, coverage and crops (synthetic data, no API)."""

import pandas as pd
from PIL import Image

from beam_checker import drawing_reader as dr
from beam_checker import text_layer as tl

W = tl.Word
H = 7  # text height in points


def word(text, cx, y):
    """A word centred on cx (CAD schedules centre text in its cell)."""
    w = 6 * len(text)
    return W(text, cx - w / 2, y, cx + w / 2, y + H)


HEADER_Y = 500
COLS = [("Mark", 80), ("Beam Size", 150), ("Top|Left", 300), ("Top|Middle", 385), ("Top|Right", 470),
        ("Bottom|Left", 555), ("Bottom|Middle", 640), ("Bottom|Right", 725), ("Side Bar (EF)", 810),
        ("Stirrups|Left", 905), ("Stirrups|Middle", 990), ("Stirrups|Right", 1075),
        ("Stirrups Type|Left", 1180), ("Span Type", 1290)]
ROWS = [
    ["B101-1", "200x450", "3H16", "2H16", "3H16", "2H16", "2H16+2H13", "2H16", "H10-250", "2H10-150", "2H10-200", "2H10-150", "Normal", ""],
    ["B101-2", "200x450", "3H16", "2H16", "3H20", "2H16", "2H16", "2H16", "H10-250", "2H10-150", "2H10-200", "2H10-150", "Normal", ""],
    ["B102a", "200x225/175", "-", "2H13", "2H13", "-", "2H13", "2H13", "-", "-", "H10-125", "H10-125", "Normal", "CANT."],
]


def synthetic_page_words(extra=()):
    words = []
    for label, x in COLS:
        parts = label.split("|")
        for i, part in enumerate(parts):              # two-line headers: first part on the upper line
            words.append(word(part, x, HEADER_Y + (len(parts) - 1 - i) * 11))
    for r, row in enumerate(ROWS):
        y = HEADER_Y - 30 - r * 28
        for (label, x), val in zip(COLS, row):
            if val:
                words.append(word(val, x, y))
    words.append(word("BEAM SCHEDULE", 600, HEADER_Y + 40))  # title above the header
    words.append(word("GENERAL NOTES", 80, 100))              # text far below the table
    return words + list(extra)


def test_header_mapping():
    f = tl._field_for_header
    assert [f("Top Left"), f("Top Middle"), f("Top Right")] == ["T1", "T2", "T3"]
    assert [f("Bottom Left"), f("Bottom Middle"), f("Bottom Right")] == ["B1", "B2", "B3"]
    assert [f("Stirrups Left"), f("Stirrups Middle"), f("Stirrups Right")] == ["S1", "S2", "S3"]
    assert f("Stirrups TypeLeft") == "link_type" and f("Type Mark") == "beam_mark"
    assert f("Beam Size") == "size" and f("Side Bar (EF)") == "side_bars" and f("BeamSpan Type") == "remark"
    assert f("T2") == "T2" and f("S3") == "S3" and f("Something else") is None


def test_reads_every_row_and_cell_as_written():
    tables = tl.find_tables(synthetic_page_words())
    assert len(tables) == 1
    recs = tables[0].records
    assert [r["beam_mark"] for r in recs] == ["B101-1", "B101-2", "B102a"]
    assert recs[0]["B2"] == "2H16+2H13" and recs[0]["S2"] == "2H10-200" and recs[0]["link_type"] == "Normal"
    assert recs[2]["T1"] == "-" and recs[2]["size"] == "200x225/175" and recs[2]["remark"] == "CANT."
    assert all(r["confidence"] == "high" and not r["flags"] for r in recs)
    assert tables[0].notes == [] and tables[0].unmapped_headers == []


def test_no_table_without_a_mark_header():
    assert tl.find_tables([w for w in synthetic_page_words() if w.text != "Mark"]) == []


def test_two_words_in_one_cell_are_flagged_not_guessed():
    stray = word("X", 300 + 25, HEADER_Y - 30)          # extra word inside B101-1's T1 cell
    rec = tl.find_tables(synthetic_page_words([stray]))[0].records[0]
    assert rec["T1"] == "3H16 X" and rec["flags"] == ["unreadable"]


def test_text_beside_the_table_is_ignored():
    rec = tl.find_tables(synthetic_page_words([word("REV A", 1900, HEADER_Y - 30)]))[0].records[0]
    assert "REV A" not in " ".join(str(v) for v in rec.values())


def _page_with_tables():
    tables = tl.find_tables(synthetic_page_words())
    return dr.Page(1, "file 1, page 1", Image.new("L", (3000, 1500), 255), "", tables=tables, scale=2.0, height_pt=700)


def test_text_layer_extraction_table_and_row_positions():
    page = _page_with_tables()
    ex = dr.read_text_layer([page])
    assert ex.method == dr.READ_TEXT and ex.requests == 0
    assert list(ex.table["Read from"].unique()) == [dr.READ_TEXT]
    assert not ex.table["Reviewed"].any()                       # nothing is pre-ticked
    assert set(ex.boxes) == set(ex.table["Row ID"])
    b = ex.boxes[ex.table["Row ID"].iloc[0]]
    x0, y0, x1, y1 = b["row"]
    assert x0 < x1 and y0 < y1 and b["header"][3] <= y0 + 1      # header sits above its row (image y grows down)
    assert dr.text_layer_summary([page]) == {1: 3}
    assert dr.read_text_layer([dr.Page(1, "x", Image.new("L", (10, 10)))]) is None


def test_ticks_are_optional_for_text_layer_rows_and_required_for_ai_rows():
    table = dr.read_text_layer([_page_with_tables()]).table
    assert dr.tick_status(table) == (0, 0) and dr.ready_to_compare(table)          # text layer: no ticks needed
    table.loc[0, "Read from"] = dr.READ_VISION                                       # pretend one row came from AI
    table.loc[1, "Read from"] = dr.READ_VISION
    assert dr.tick_status(table) == (0, 2) and not dr.ready_to_compare(table)
    table["Reviewed"] = table["Reviewed"].astype(object)   # rows added in the editor can have an empty tick
    table.loc[0, "Reviewed"] = True
    table.loc[1, "Reviewed"] = None
    assert dr.tick_status(table) == (1, 2) and not dr.ready_to_compare(table)
    table.loc[1, "Reviewed"] = True
    assert dr.ready_to_compare(table)
    assert not dr.ready_to_compare(table.iloc[0:0])


def test_coverage_counts_prokon_marks_found_on_the_drawing():
    table = pd.DataFrame({"Beam mark": ["B101-1", "B101-2", "B102a", "B999", None]})
    cov = dr.coverage(table, ["B101", "B102a", "B103"])
    assert cov["found"] == ["B101", "B102a"] and cov["missing"] == ["B103"] and cov["total"] == 3
    assert cov["drawing_only"] == ["B999"]


def test_type2_excel_export_is_verbatim_and_reads_back_identically(tmp_path):
    import io
    from unittest import mock

    from beam_checker import checker

    table = dr.read_text_layer([_page_with_tables()]).table
    table.loc[table["Beam mark"] == "B101-1", "T3"] = "→"          # symbols must survive unchanged
    data = dr.table_to_type2_excel(table)
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
    assert list(sheets) == [dr.SCHEDULE_SHEET, dr.SOURCE_SHEET]
    sched = sheets[dr.SCHEDULE_SHEET]
    c = checker.EXCEL_FORMATS["Format 2"]
    row = sched[sched[c["mark"]] == "B101-1"].iloc[0]
    assert row[c["t3"]] == "→" and row[c["b2"]] == "2H16+2H13" and row[c["st_m"]] == "2H10-200"
    assert sched[sched[c["mark"]] == "B102a"].iloc[0][c["t1"]] == "-"
    assert "Page" not in " ".join(str(v) for v in sched.iloc[0].tolist())  # position info is on the other sheet

    req = {"req_t1": 500.0, "req_b2": 300.0, "req_t3": 450.0, "req_asv_l": 0.5, "req_asv_m": 0.3, "req_asv_r": 0.5}
    beams = {"B101": {1: req, 2: req}, "B102a": {1: req}, "B103": {1: req}}
    with mock.patch.object(checker, "extract_all_beams_from_pdf", return_value=beams):
        via_records = checker.run_comparison_records(dr.table_to_records(table), "unused.pdf")
        via_excel = checker.run_comparison(io.BytesIO(data), "unused.pdf", dr.SCHEDULE_SHEET, "Format 2")
    assert via_records.rows == via_excel.rows
    assert via_records.excel_only == via_excel.excel_only and via_records.pdf_only == via_excel.pdf_only == ["B103"]


def test_schedule_summary_for_the_assistant():
    table = dr.read_text_layer([_page_with_tables()]).table
    table.loc[table["Beam mark"] == "B102a", "Confidence"] = "low"
    s = dr.schedule_summary(table, dr.READ_TEXT, ["B103"], ["B999"], 3, 2)
    assert s["ai_used_for_reading"] is False and s["coverage"] == "found 2 of 3 Prokon beam marks on the drawing"
    assert [r["mark"] for r in s["rows_needing_extra_care"]] == ["B102a"]
    assert s["prokon_beams_not_on_drawing"] == ["B103"] and len(s["rows"]) == 3


def test_beam_detail_table_shows_drawing_values_and_checks():
    from unittest import mock

    from beam_checker import checker

    table = dr.read_text_layer([_page_with_tables()]).table
    row = table[table["Beam mark"] == "B101-1"].iloc[0]
    req = {"req_t1": 500.0, "req_b2": 300.0, "req_t3": 700.0, "req_asv_l": 0.5, "req_asv_m": 0.3, "req_asv_r": 0.5}
    with mock.patch.object(checker, "extract_all_beams_from_pdf", return_value={"B101": {1: req}}):
        res = checker.run_comparison_records(dr.table_to_records(table[table["Beam mark"] == "B101-1"]), "x.pdf")
    detail = dr.beam_detail(row, res.rows)
    assert list(detail["Position"]) == ["Left", "Middle", "Right"]
    assert list(detail.columns[:2]) == ["Position", "Result"]                 # the verdict is visible first
    assert list(detail["Top bars"]) == ["3H16", "2H16", "3H16"]
    assert detail.loc[1, "Bottom bars"] == "2H16+2H13" and detail.loc[1, "Checked"] == "bottom"
    assert list(detail["Result"]) == [r[12] for r in res.rows] and detail.loc[2, "Result"] == "FAIL"
    assert detail.loc[2, "As req → prov (mm²)"] == f"{res.rows[2][2]} → {res.rows[2][4]}"
    assert detail.loc[2, "Flexure"] == "FAIL"
    assert "Result" not in dr.beam_detail(row).columns            # drawing values only (no Prokon match)


def test_span_requirements_matches_the_comparison():
    from beam_checker import checker
    req = {"req_t1": 1.0, "req_b2": 2.0, "req_t3": 3.0, "req_asv_l": 0.1, "req_asv_m": 0.2, "req_asv_r": 0.3}
    beams = {"B101": {1: req, 2: {**req, "req_t1": 9.0}}}
    assert checker.span_requirements("B101-2", beams) == ("B101", {**req, "req_t1": 9.0})
    assert checker.span_requirements("b 101-7", beams) == ("B101", req)      # unknown span -> span 1
    assert checker.span_requirements("B999", beams) == (None, None)



def test_word_flags_are_carried_into_the_record():
    words = synthetic_page_words()
    for w in words:
        if w.text == "2H16+2H13":
            w.flags = ("formatting_removed",)
    recs = tl.find_tables(words)[0].records
    assert recs[0]["flags"] == ["formatting_removed"] and recs[0]["B2"] == "2H16+2H13"
    assert recs[1]["flags"] == []


def test_header_report_lists_the_headers_found():
    # no table: the bar columns are labelled in a way the reader does not know
    words = [w for w in synthetic_page_words() if w.text not in ("Top", "Bottom")]
    assert tl.find_tables(words) == []
    found = tl.header_report(words)
    assert "Mark" in found and "Beam Size" in found and "Left" in found
    # no "Mark" header at all: header-like texts are listed instead
    found = tl.header_report([w for w in synthetic_page_words() if w.text != "Mark"])
    assert "Mark" not in found and "Top" in found and "Stirrups" in found
