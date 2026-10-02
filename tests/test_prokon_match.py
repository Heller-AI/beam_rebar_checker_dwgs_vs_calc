"""Spans without a Prokon result are never OK; span fallback and zero requirements are flagged (generic data)."""

from streamlit.testing.v1 import AppTest

from beam_checker import agent, checker, fixes
from beam_checker.checker import (FALLBACK_NOTE, NO_RESULT_NOTE, NOT_CHECKED, NOTE_COLUMN, NOMINAL_COLUMN, ZERO_NOTE,
                                  CheckResult, ScheduleRecord, match_span)

REQ = {"req_t1": 900.0, "req_b2": 700.0, "req_t3": 900.0, "req_asv_l": 0.5, "req_asv_m": 0.3, "req_asv_r": 0.5,
       "nom_asv_l": 0.18, "nom_asv_m": 0.18, "nom_asv_r": 0.18}
ZERO = dict.fromkeys(("req_t1", "req_b2", "req_t3", "req_asv_l", "req_asv_m", "req_asv_r"), 0.0)
BEAMS = {"B101": {1: REQ, 2: {**REQ, "req_t1": 1200.0}}, "B102": {1: REQ}, "B103": {}, "B104": {1: ZERO},
         "B105": {1: REQ}}


def record(mark):
    return ScheduleRecord(mark, [{"t1": "3H20", "t2": "2H16", "t3": "3H20", "b1": "3H20", "b2": "3H20",
                                  "st_l": "2H10-150", "st_m": "2H10-200", "st_r": "2H10-150"}])


def run(marks, monkeypatch):
    monkeypatch.setattr(checker, "extract_all_beams_from_pdf", lambda pdf, progress=None: BEAMS)
    return checker.run_comparison_records([record(m) for m in marks], "prokon.pdf")


def test_match_span_cases():
    assert match_span("B101-2", BEAMS).note == "" and match_span("B101-2", BEAMS).data["req_t1"] == 1200.0
    m = match_span("B102-2", BEAMS)                                    # span 2 not in the report
    assert m.checked and m.span_used == 1 and m.note == FALLBACK_NOTE.format(used=1, span=2)
    assert m.note == "Prokon span 1 used, span 2 not in report"
    m = match_span("B999", BEAMS)
    assert not m.checked and m.matched is None and m.note == NO_RESULT_NOTE
    m = match_span("B103-1", BEAMS)                                    # in the report, but nothing was read
    assert not m.checked and m.matched == "B103" and m.note.startswith("No Prokon result, not checked")
    m = match_span("B104", BEAMS)
    assert m.checked and m.note == ZERO_NOTE
    assert checker.span_requirements("B103-1", BEAMS) == (None, None)


def test_rows_of_matched_spans_are_unchanged(monkeypatch):
    res = run(["B101-1", "B101-2"], monkeypatch)
    direct = checker.check_span(record("B101-1"), REQ) + checker.check_span(record("B101-2"), BEAMS["B101"][2])
    assert res.rows == direct and res.notes == {} and res.unchecked == []


def test_spans_without_a_result_are_not_checked_and_listed(monkeypatch):
    res = run(["B101-1", "B102-2", "B103-1", "B104", "B999-1"], monkeypatch)
    assert res.matched_count == 3                                        # B101-1, B102-2 (fallback), B104 (zero)
    assert {r[0] for r in res.rows} == {"B101-1", "B102-2", "B104"}      # no OK/FAIL row for B103-1 or B999-1
    assert res.unchecked == [("B103-1", match_span("B103-1", BEAMS).note), ("B999-1", NO_RESULT_NOTE)]
    assert res.notes == {"B102-2": "Prokon span 1 used, span 2 not in report", "B104": ZERO_NOTE}
    assert res.excel_only == ["B999"] and res.no_data_bases == {"B103"}
    assert res.pdf_only == ["B105"]                                      # B103 is on the schedule: listed apart

    df = res.to_dataframe()
    assert len(df) == len(res.rows) + 2
    not_checked = df[df["Overall Status"] == NOT_CHECKED]
    assert list(not_checked["Beam Mark"]) == ["B103-1", "B999-1"]
    assert set(not_checked["Flex Status"]) == {""} and set(not_checked["Shear Status"]) == {""}
    assert set(df.loc[df["Beam Mark"] == "B102-2", NOTE_COLUMN]) == {"Prokon span 1 used, span 2 not in report"}
    assert set(df.loc[df["Beam Mark"] == "B101-1", NOTE_COLUMN]) == {""}
    assert list(df.loc[df["Beam Mark"] == "B101-1", NOMINAL_COLUMN]) == ["0.180", "0.180", "0.180"]
    assert list(df.columns[:len(checker.RESULT_COLUMNS)]) == checker.RESULT_COLUMNS


def test_assistant_counts_not_checked_and_notes(monkeypatch):
    res = run(["B101-1", "B102-2", "B103-1", "B999-1"], monkeypatch)
    summary = agent.execute_tool("get_summary", {}, res)
    assert summary["check_notes_fact"] == ("2 span(s) not checked (no Prokon result; neither OK nor FAIL); "
                                           "1 checked span(s) with a check note.")
    assert summary["rows_total"] == 6                                    # only checked spans count as rows
    detail = agent.execute_tool("get_beam_detail", {"beam_mark": "B102-2"}, res)
    assert all(r[NOTE_COLUMN] == "Prokon span 1 used, span 2 not in report" for r in detail["rows"])
    assert agent.execute_tool("get_beam_detail", {"beam_mark": "B999-1"}, res) == {
        "not_checked": [{"beam": "B999-1", "note": NO_RESULT_NOTE}]}
    table = fixes.failures_table(res)
    assert table["check_notes"]["spans_not_checked"][0]["beam"] == "B103-1"
    assert all(r["beam"] not in ("B103-1", "B999-1") for r in table["rows"])


def test_results_page_warns_and_filters(monkeypatch):
    res = run(["B101-1", "B102-2", "B999-1"], monkeypatch)
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.session_state["result"] = res
    at.session_state["result_source"] = "Excel schedule"
    at.run()
    assert not at.exception
    assert any("1 span(s) not checked" in w.value and "1 checked span(s) with a check note" in w.value
               for w in at.warning)
    assert [m.value for m in at.metric][:2] == ["2", "6"]
    status = next(s for s in at.selectbox if s.label == "Status")
    status.set_value("Not checked").run()
    shown = next(d.value for d in at.dataframe if "Overall Status" in d.value.columns)
    assert list(shown["Beam Mark"]) == ["B999-1"] and list(shown[NOTE_COLUMN]) == [NO_RESULT_NOTE]
    unmatched = [d.value for d in at.dataframe if list(d.value.columns) == ["Beam mark", "Where"]]
    assert unmatched and "B999" in list(unmatched[0]["Beam mark"])
