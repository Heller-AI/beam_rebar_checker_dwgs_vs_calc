"""The Excel reader and the record-based comparison must give exactly the old results."""

from unittest import mock

import pandas as pd
import pytest

from beam_checker import checker
from beam_checker.checker import ScheduleRecord, check_span, read_excel_schedule

REQ = {"req_t1": 500.0, "req_b2": 300.0, "req_t3": 450.0, "req_asv_l": 0.5, "req_asv_m": 0.3, "req_asv_r": 0.5}
PDF_BEAMS = {"B101": {1: REQ, 2: REQ}, "B102": {1: REQ}}


def row(**cells):
    return {k: cells.get(k, "") for k in checker.CELL_KEYS}


def write_schedule(path, fmt):
    c = checker.EXCEL_FORMATS[fmt]
    grid = []

    def add(mark=None, **cells):
        r = [None] * 16
        r[c["mark"]] = mark
        for k, v in cells.items():
            r[c[k]] = v
        grid.append(r)

    add("MARK")
    add("B101-1", t1="3H16", t2="2H16", t3="→", b1="2H16", st_l="2H10-200", st_m="2H10-250")
    add(None, t1="1H12", b2="2H16+2H13")                     # second bar layer
    add("B101-2", t1="-", t3="3H20", b1="3H16", st_m="2H10-150")  # cantilever-style blank end
    add("B102", t1="←", t2="2H13", t3="2H13", b1="2H13", st_l="H8-200", st_m="H8-200", st_r="H8-200")
    add("X1", t1="2H16")                                     # not a valid mark, ignored
    pd.DataFrame(grid).to_excel(path, sheet_name="BEAM SCHEDULE", header=False, index=False)


@pytest.mark.parametrize("fmt", list(checker.EXCEL_FORMATS))
def test_excel_reader_keeps_cells_as_written(tmp_path, fmt):
    path = tmp_path / "s.xlsx"
    write_schedule(path, fmt)
    records = read_excel_schedule(path, "BEAM SCHEDULE", fmt)
    assert [r.mark for r in records] == ["B101-1", "B101-2", "B102", "X1"]
    assert records[0].rows[0]["t3"] == "→"            # arrows are resolved later, by check_span
    assert records[0].rows[1] == row(t1="1H12", b2="2H16+2H13")


@pytest.mark.parametrize("fmt", list(checker.EXCEL_FORMATS))
def test_record_path_matches_excel_path(tmp_path, fmt):
    path = tmp_path / "s.xlsx"
    write_schedule(path, fmt)
    with mock.patch.object(checker, "extract_all_beams_from_pdf", return_value=PDF_BEAMS):
        via_excel = checker.run_comparison(path, "unused.pdf", "BEAM SCHEDULE", fmt)
        records = read_excel_schedule(path, "BEAM SCHEDULE", fmt)
        via_records = checker.run_comparison_records(records, "unused.pdf")
    assert via_excel.rows == via_records.rows and via_excel.matched_count == 3
    assert via_excel.excel_only == via_records.excel_only


def test_check_span_rules_on_a_record():
    rec = ScheduleRecord("B101-1", [row(t1="→", t2="2H16", t3="-", b1="2H13", st_m="2H10-200")])
    left, mid, right = check_span(rec, REQ)
    assert left[3] == "2H16"                      # arrow -> mid column value
    assert right[3] == "2H16"                     # existing cantilever rule copies the filled end
    assert mid[3] == "2H13" and left[8] == "2H10-200"   # bottom falls back to B1; stirrups fall back to mid
    assert [r[13] for r in (left, mid, right)] == list(checker.EXCEL_REMARKS)


def test_custom_remarks():
    rec = ScheduleRecord("B101-1", [row(t1="2H16", t3="2H16", b1="2H16")])
    rows = check_span(rec, REQ, remarks=("a", "b", "c"))
    assert [r[13] for r in rows] == ["a", "b", "c"]


def test_too_few_columns_gives_a_clear_error(tmp_path):
    path = tmp_path / "narrow.xlsx"
    pd.DataFrame([["MARK", "x"], ["B101", "2H16"]]).to_excel(path, header=False, index=False)
    with pytest.raises(ValueError, match="needs at least 14"):
        read_excel_schedule(path, None, "Format 2")
