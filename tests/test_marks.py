"""Beam-mark keys: a span suffix is never confused with a beam number (generic marks, no API)."""

from beam_checker import agent, checker, drawing_reader as dr, fixes
from beam_checker.checker import CheckResult
from beam_checker.parsers import loose_match, mark_key, normalize_str


def rec(mark, **cells):
    base = {f: "" for f in dr.RECORD_FIELDS}
    base.update(beam_mark=mark, size="300x600", T1="3H20", T2="2H16", T3="3H20", B1="3H20", B2="3H20",
                S1="H10-150", S2="H10-200", S3="H10-150", confidence="high", flags=[], source_note="")
    base.update(cells)
    return base


def test_mark_key_is_exact_apart_from_case_and_outer_spaces():
    assert mark_key(" b1-1 ") == mark_key("B1-1") == "B1-1"
    assert mark_key("B1-1") != mark_key("B11") and mark_key("B2-2") != mark_key("B22")
    assert normalize_str("B1-1") == normalize_str("B11")          # why the loose key must not decide


def test_loose_match_never_picks_one_of_two_loose_candidates():
    assert loose_match("B11", ["B1-1", "B11"]) == "B11"            # exact wins
    assert loose_match("b11", ["B1-1", "B11"]) == "B11"            # case-insensitive wins over loose
    assert loose_match("B 11", ["B1-1", "B11"]) is None           # loose would be ambiguous: no match
    assert loose_match("B-101", ["B101"]) == "B101"               # loose fallback when unique
    assert loose_match("B102", ["B101"]) is None


def test_span_one_and_another_beam_are_not_duplicates():
    table = dr.records_to_table([(1, rec("B1-1")), (1, rec("B11")), (1, rec("B2-2")), (1, rec("B22"))])
    assert dr.table_conflicts(table) == []
    assert "conflict" not in " ".join(table["Flags"])
    assert len(table) == 4                                         # identical cells: still four beams, none dropped


def test_true_duplicates_are_still_found():
    table = dr.records_to_table([(1, rec("B1-1")), (1, rec("b1-1 ", T1="4H20"))])
    assert dr.table_conflicts(table) == ["B1-1", "b1-1"]
    assert all("conflict" in f for f in table["Flags"])
    same = dr.records_to_table([(1, rec("B1-1")), (2, rec("B1-1"))])  # same row read twice (overlap)
    assert len(same) == 1


def test_prokon_matching_keeps_span_and_beam_apart():
    beams = {"B1": {1: {"req_t1": 100.0}, 2: {"req_t1": 200.0}}, "B11": {1: {"req_t1": 1100.0}}}
    assert checker.span_requirements("B1-1", beams) == ("B1", {"req_t1": 100.0})
    assert checker.span_requirements("B1-2", beams) == ("B1", {"req_t1": 200.0})
    assert checker.span_requirements("B11", beams) == ("B11", {"req_t1": 1100.0})
    assert checker._match_pdf_base("b11", beams) == "B11"


def test_assistant_beam_lookup_keeps_pairs_apart():
    row = ["", "Left", "1.0", "", "1.0", "", "OK", "0.1", "", "0.1", "", "OK", "OK", ""]
    result = CheckResult(rows=[["B1-1", *row[1:]], ["B1-2", *row[1:]], ["B11", *row[1:]]], matched_count=3)
    assert {r["Beam Mark"] for r in agent.execute_tool("get_beam_detail", {"beam_mark": "B11"}, result)["rows"]} == {"B11"}
    assert {r["Beam Mark"] for r in agent.execute_tool("get_beam_detail", {"beam_mark": "B1"}, result)["rows"]} \
        == {"B1-1", "B1-2"}
    assert {r["Beam Mark"] for r in agent.execute_tool("get_beam_detail", {"beam_mark": "b1-1"}, result)["rows"]} \
        == {"B1-1"}


def test_beam_width_lookup_keeps_pairs_apart():
    widths = {"B1-1": 300, "B11": 200}
    assert fixes.width_for("B11", widths) == 200 and fixes.width_for("b1-1", widths) == 300
