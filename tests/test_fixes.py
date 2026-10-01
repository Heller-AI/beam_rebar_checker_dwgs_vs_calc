"""Failure tables and fix suggestions computed in code (no API calls)."""

import json
from types import SimpleNamespace as NS

from beam_checker import agent, fixes
from beam_checker.checker import CheckResult

# B101-1: left support fails flexure only; B101-2: middle fails shear only; B102: left fails both
ROWS = [
    ["B101-1", "Left Support (Pos Start)", "975.1", "3H20", "942.5", "96.7%", "FAIL (Deficit)", "0.500", "2H10-200", "0.785", "157.0%", "OK", "FAIL", "r"],
    ["B101-1", "Mid-Span (Max Bot)", "300.0", "2H16", "402.1", "134.0%", "OK", "0.500", "2H10-200", "0.785", "157.0%", "OK", "OK", "r"],
    ["B101-2", "Mid-Span (Max Bot)", "300.0", "2H16", "402.1", "134.0%", "OK", "1.000", "2H10-200", "0.785", "78.5%", "FAIL (Deficit)", "FAIL", "r"],
    ["B102", "Left Support (Pos Start)", "5105.6", "3H20", "942.5", "18.5%", "FAIL (Deficit)", "1.200", "2H10-200", "0.785", "65.4%", "FAIL (Deficit)", "FAIL", "r"],
]
RESULT = CheckResult(rows=ROWS, matched_count=3)


def test_failure_rows_one_unit_per_row():
    rows = fixes.failure_rows(RESULT)
    assert [(r["beam"], r["position"], r["check"]) for r in rows] == [
        ("B101-1", "Left", "Flexure"), ("B101-2", "Middle", "Shear"), ("B102", "Left", "Flexure"), ("B102", "Left", "Shear")]
    first = rows[0]
    assert first["unit"] == "mm²" and first["required"] == 975.1 and first["provided"] == 942.5
    assert first["shortfall"] == 32.6 and first["provided_over_required_pct"] == 96.7
    shear = rows[1]
    assert shear["unit"] == "Asv/sv (mm²/mm)" and shear["shortfall"] == 0.215 and shear["provided_over_required_pct"] == 78.5


def test_counts_are_computed_facts():
    c = fixes.failure_counts(RESULT)
    assert (c["rows_fail_flexure"], c["rows_fail_shear"], c["rows_fail_both"], c["spans_with_fail"]) == (2, 2, 1, 3)
    assert c["fact"] == "3 of 4 position rows fail in 3 span(s): 2 fail flexure, 2 fail shear, 1 fail both."


def test_bar_suggestion_fits_the_width():
    s = fixes.suggest_bars(975.1, width=300)
    assert s["fit"] == fixes.FIT_OK and s["area"] >= 975.1
    # every group must fit one layer of a 300 mm beam
    for group in s["suggestion"].split("+"):
        n, d = group.split("H")
        assert int(n) <= fixes.bars_per_layer(300, int(d))


def test_bar_suggestion_that_does_not_fit_the_width():
    s = fixes.suggest_bars(12000, width=200)          # far too much steel for a 200 mm beam in 2 layers
    assert s["fit"] == fixes.FIT_NO and s["suggestion"] and s["area"] >= 12000


def test_suggestion_keeps_or_steps_up_the_current_bar_size():
    s = fixes.suggest_bars(1156.7, width=300, current="3H16")
    assert s["suggestion"] == "6H16" and s["fit"] == fixes.FIT_OK      # add bars of the same size
    t = fixes.suggest_bars(4302.8, width=800, current="6H20")
    assert all(int(g.split("H")[1]) >= 20 for g in t["suggestion"].split("+"))
    assert "H10" not in fixes.suggest_bars(2157.7, width=800, current="")["suggestion"]


def test_no_valid_option():
    s = fixes.suggest_bars(10_000_000, width=300)
    assert s == {"suggestion": None, "area": None, "fit": fixes.NO_OPTION}
    t = fixes.suggest_stirrups(1000.0, width=300)
    assert t == {"suggestion": None, "asv_sv": None, "fit": fixes.NO_OPTION}


def test_width_unknown_is_labelled():
    assert fixes.suggest_bars(975.1)["fit"] == fixes.FIT_UNKNOWN
    assert fixes.suggest_stirrups(1.0)["fit"] == fixes.FIT_UNKNOWN


def test_stirrup_legs_limited_by_width():
    # 150 mm fits only 2 legs; 2 legs give at most 2 x 201 / 75 = 5.36, so 6.0 needs 3+ legs: does not fit
    assert fixes.suggest_stirrups(5.0, width=150)["fit"] == fixes.FIT_OK
    narrow = fixes.suggest_stirrups(6.0, width=150)
    assert narrow["fit"] == fixes.FIT_NO and int(narrow["suggestion"].split("H")[0]) > 2
    wide = fixes.suggest_stirrups(1.2, width=300)
    assert wide["fit"] == fixes.FIT_OK and wide["asv_sv"] >= 1.2


def test_failures_table_with_fixes_and_widths():
    table = fixes.failures_table(RESULT, with_fixes=True, widths={"B101-1": 300, "b 102": 200})
    rows = {(r["beam"], r["check"]): r for r in table["rows"]}
    assert rows[("B101-1", "Flexure")]["beam_width_mm"] == 300 and rows[("B101-1", "Flexure")]["fit"] == fixes.FIT_OK
    assert rows[("B102", "Flexure")]["beam_width_mm"] == 200                 # matched loosely ("b 102")
    assert rows[("B101-2", "Shear")]["fit"] == fixes.FIT_UNKNOWN              # no width for this span
    assert all(r["suggested_provides"] >= r["required"] for r in table["rows"] if r["suggested_provides"])
    assert "engineer" in table["note"] and table["counts"]["rows_fail_both"] == 1


def test_get_failures_tool_and_one_round_trip():
    """'Suggest fixes' takes one tool call and one answer: 2 model calls, not one call per row."""
    calls = []

    def create(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="get_failures",
                                                          input={"with_fixes": True})])
        return NS(stop_reason="end_turn", content=[NS(type="text", text="Table above.")])

    messages = [{"role": "user", "content": "Suggest fixes"}]
    agent.ask(NS(beta=NS(messages=NS(create=create))), messages, RESULT, widths={"B101-1": 300})
    assert len(calls) == 2
    payload = json.loads(messages[2]["content"][0]["content"])
    assert len(payload["rows"]) == 4 and payload["rows"][0]["fit"] == fixes.FIT_OK
    assert "get_failures" == agent.TOOLS[0]["name"]
