"""Possible-typo screening: highlight only, never changes what the checker counts or OK/FAIL."""

from unittest import mock

import pandas as pd
import pytest

from beam_checker import checker, drawing_reader as dr
from beam_checker import plausibility as pl


def test_width_from_size():
    assert pl.beam_width("300x1000") == 300
    assert pl.beam_width("200x225/175") == 200          # tapered depth: width is still the first number
    assert pl.beam_width(" 250 X 600 ") == 250
    assert pl.beam_width("") is None and pl.beam_width("-") is None and pl.beam_width("see note") is None


def test_bars_per_layer_uses_width_and_diameter():
    # 300 wide: clear 300 - 2*(25+10) = 230 mm
    assert pl.bars_per_layer(300, 25) == 5                # (230 + 25) / (25 + 25) = 5.1
    assert pl.bars_per_layer(300, 32) == 4                # (230 + 32) / (32 + 32) = 4.09
    assert pl.bars_per_layer(600, 25) == 11
    assert pl.bars_per_layer(120, 25) == 2                # never below the two corner bars


@pytest.mark.parametrize("notation,width,flagged", [
    ("33H25", 300, True),             # extra digit: the typo this check is for
    ("3H25", 300, False),             # the intended value
    ("6H32+6H25+6H25", 300, False),   # multi-layer notation: each group checked on its own
    ("6H32+66H25", 300, True),        # one bad group in a multi-layer notation
    ("10H25", 300, False),            # exactly two full layers
    ("11H25", 300, True),             # one more than two layers
    ("2H13+2H13", 200, False),
    ("H16", 200, False),              # count omitted = 1 bar
    ("", 300, False), ("-", 300, False), ("→", 300, False), ("not bars", 300, False),
])
def test_check_notation(notation, width, flagged):
    assert bool(pl.check_notation(notation, width)) is flagged


def test_row_issues_name_the_field_and_explain():
    row = {"Size": "300x1000", "T1": "3H25+3H25", "T2": "3H25+3H25", "T3": "33H25",
           "B1": "3H25+3H25", "B2": "3H25+3H25", "B3": "3H25"}
    issues = pl.row_issues(row)
    assert [(i["field"], i["group"], i["bars"], i["limit"]) for i in issues] == [("T3", "33H25", 33, 10)]
    assert pl.describe(issues[0]) == "T3 = 33H25: more than 10 bars of H25 cannot fit a 300 mm wide beam in 2 layers"
    assert pl.row_issues({**row, "T3": "3H25"}) == []
    assert pl.row_issues({**row, "Size": "unknown"}) == []          # no width: no guess


def rec(mark, **kw):
    base = {"beam_mark": mark, "size": "300x1000", "T1": "3H25+3H25", "T2": "3H25+3H25", "T3": "3H25",
            "B1": "3H25+3H25", "B2": "3H25+3H25", "B3": "3H25", "side_bars": "", "link_type": "Normal",
            "S1": "2H10-200", "S2": "2H10-200", "S3": "2H10-200", "remark": "", "confidence": "high",
            "flags": [], "source_note": "row", "row_box": []}
    return {**base, **kw}


def test_review_table_shows_possible_typo_for_text_layer_and_ai_rows():
    table = dr.records_to_table([(1, rec("B136", T3="33H25"), {"read": dr.READ_TEXT}),
                                 (1, rec("B137"), {"read": dr.READ_TEXT}),
                                 (1, rec("B138", B2="6H32+66H25"), {"read": dr.READ_VISION})])
    by_mark = table.set_index("Beam mark")
    assert by_mark.loc["B136", "Review"] == "⚠ possible typo" and "possible_typo" in by_mark.loc["B136", "Flags"]
    assert by_mark.loc["B137", "Review"] == "" and by_mark.loc["B137", "Flags"] == ""
    assert by_mark.loc["B138", "Review"] == "⚠ possible typo"
    assert by_mark.loc["B136", "Confidence"] == "high"         # a typo on the drawing is not a reading problem


def test_flag_never_changes_cells_or_ok_fail():
    """The same schedule with and without the flag gives identical records and identical results."""
    flagged = dr.records_to_table([(1, rec("B136-1", T3="33H25"), {"read": dr.READ_TEXT})])
    plain = flagged.assign(Flags="", Review="")
    assert dr.table_to_records(flagged)[0].rows == dr.table_to_records(plain)[0].rows
    req = {"req_t1": 866.6, "req_b2": 4375.9, "req_t3": 866.6, "req_asv_l": 0.7, "req_asv_m": 0.7, "req_asv_r": 0.51}
    with mock.patch.object(checker, "extract_all_beams_from_pdf", return_value={"B136": {1: req}}):
        a = checker.run_comparison_records(dr.table_to_records(flagged), "x.pdf")
        b = checker.run_comparison_records(dr.table_to_records(plain), "x.pdf")
    assert a.rows == b.rows
    right = a.rows[2]
    assert right[3] == "33H25" and right[12] == "OK"            # the checker still counts what is written


def test_findings_issues_are_recomputed_from_the_reviewed_table():
    table = dr.records_to_table([(1, rec("B136", T3="33H25"), {"read": dr.READ_TEXT})])
    assert dr.typo_rows(table)[0][0] == "B136"
    table.loc[0, "T3"] = "3H25"                                  # the reviewer corrected the typo
    assert dr.typo_rows(table) == []
    assert dr.typo_rows(pd.DataFrame(columns=table.columns)) == []


def test_flagged_row_is_shown_first_but_results_keep_reading_order():
    table = dr.records_to_table([(1, rec("B101"), {"read": dr.READ_TEXT}),
                                 (1, rec("B102"), {"read": dr.READ_TEXT}),
                                 (1, rec("B103", T3="33H25"), {"read": dr.READ_TEXT})])
    assert list(table["Beam mark"]) == ["B103", "B101", "B102"]          # review table: rows to check first
    assert [r.mark for r in dr.table_to_records(table)] == ["B101", "B102", "B103"]   # comparison: drawing order
    import io
    sched = pd.read_excel(io.BytesIO(dr.table_to_type2_excel(table)), sheet_name=dr.SCHEDULE_SHEET, header=None)
    assert list(sched[checker.EXCEL_FORMATS["Format 2"]["mark"]][1:]) == ["B101", "B102", "B103"]

