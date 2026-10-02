"""The review step: meaningful highlights, Prokon concerns, ticks and unlock rules (mocked readings, no API)."""

from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

import beam_checker
from beam_checker import drawing_reader as dr

REQ = {"req_t1": 900.0, "req_b2": 700.0, "req_t3": 900.0, "req_asv_l": 0.5, "req_asv_m": 0.3, "req_asv_r": 0.5}
BEAMS = {"B101": {1: REQ, 2: REQ}, "B102": {1: REQ}, "B103": {}, "B104": {1: dict.fromkeys(REQ, 0.0)},
         "B105": {1: REQ}, "B106": {1: REQ}}


def rec(mark, confidence="high", flags=(), **cells):
    base = {f: "" for f in dr.RECORD_FIELDS}
    base.update(beam_mark=mark, size="300x600", T1="3H20", T2="2H16", T3="3H20", B1="3H20", B2="3H20",
                S1="2H10-150", S2="2H10-200", S3="2H10-150", confidence=confidence, flags=list(flags), source_note="")
    base.update(cells)
    return base


def test_only_genuine_concerns_highlight():
    assert not dr.needs_review("high", "legs_not_stated, ditto_unconfirmed, cantilever_one_end, tapered_size")
    assert not dr.needs_review("medium", "")
    assert dr.needs_review("low", "")
    for flag in ("notation_invalid", "unreadable", "possible_typo", "conflict", "continuity_mismatch"):
        assert dr.needs_review("high", flag)


def test_typical_flags_alone_do_not_highlight_a_row():
    table = dr.records_to_table([
        (1, rec("B101-1", S1="H10-150", S2="H10-200", S3="H10-150")),        # legs not stated
        (1, rec("B101-2", T1="→")),                                          # arrow read as written
        (1, rec("B102", confidence="medium")),
        (1, rec("B105", T1="3X20")),                                         # does not parse
    ])
    review = dict(zip(table["Beam mark"], table["Review"]))
    assert review == {"B101-1": "", "B101-2": "", "B102": "", "B105": "⚠ check"}
    assert "legs_not_stated" in table.loc[table["Beam mark"] == "B101-1", "Flags"].item()


def test_prokon_concerns_per_row():
    table = dr.records_to_table([(1, rec(m)) for m in ("B101-1", "B102-2", "B103-1", "B104", "B999", "B105")])
    by_mark = {table.loc[table["Row ID"] == i, "Beam mark"].item(): n
               for i, n in dr.prokon_concerns(table, BEAMS).items()}
    assert by_mark == {"B102-2": "Prokon span 1 used, span 2 not in report",
                       "B103-1": by_mark["B103-1"], "B104": "Required steel is zero in flexure and shear: check the "
                                                            "Prokon match", "B999": "No Prokon result, not checked"}
    assert by_mark["B103-1"].startswith("No Prokon result, not checked")


# ------------------------------------------------------------------ the real page, AI reading mocked

def ai_extraction(records):
    table, boxes = dr.build_table([(1, r, {"read": dr.READ_VISION}) for r in records])
    return dr.DrawingExtraction(table, {}, {}, 3, boxes, dr.READ_VISION)


@contextmanager
def drawing_page(extraction, prokon=True):
    """Drawing mode with one uploaded drawing whose reading is `extraction` (no rendering, no API)."""
    real = st.file_uploader

    def uploader(label, *args, **kwargs):
        if kwargs.get("key") == "drawing_files":
            return [NS(name="schedule.pdf", getvalue=lambda: b"%PDF-generated")]
        if kwargs.get("key") == "prokon_pdf":
            return NS(name="prokon.pdf", getvalue=lambda: b"%PDF-prokon") if prokon else None
        return real(label, *args, **kwargs)

    with mock.patch.object(st, "file_uploader", side_effect=uploader), \
            mock.patch.object(dr, "count_pages", return_value=1), \
            mock.patch.object(dr, "load_pages", return_value=[]), \
            mock.patch.object(dr, "text_layer_summary", return_value={1: len(extraction.table)}), \
            mock.patch.object(dr, "read_text_layer", return_value=extraction), \
            mock.patch.object(beam_checker, "extract_all_beams_from_pdf", return_value=BEAMS):
        at = AppTest.from_file("../app.py", default_timeout=60)
        at.session_state["schedule_source"] = "Drawing PDF or image"
        at.run()
        yield at


def test_review_page_lists_prokon_concerns():
    ex = ai_extraction([rec("B101-1"), rec("B102-2"), rec("B999")])
    with drawing_page(ex) as at:
        assert not at.exception
        exp = next(e for e in at.expander if "need a look against the Prokon report" in e.label)
        assert exp.label.startswith("⚠ 2 row(s)")
        assert any("2 highlighted (yellow)" in c.value for c in at.caption)


# ------------------------------------------------------------------ ticks and unlock rules

def review_table():
    """Five AI-read rows: two plain, one highlighted (does not parse), one duplicate pair."""
    return dr.records_to_table([(1, r, {"read": dr.READ_VISION}) for r in (
        rec("B101-1"), rec("B101-2", S1="H10-150"), rec("B105", T1="3X20"), rec("B106"), rec("b106", T1="4H20"))])


def test_tick_all_unflagged_ticks_only_rows_without_a_concern():
    t = review_table()
    conflicts = dr.table_conflicts(t)
    concern = dr.concern_rows(t, {}, conflicts)
    assert dict(zip(t["Beam mark"], concern)) == {"B105": True, "B106": True, "b106": True,
                                                  "B101-1": False, "B101-2": False}
    ticked = dr.tick_unflagged(t, concern)
    assert dict(zip(ticked["Beam mark"], ticked["Reviewed"])) == {"B105": False, "B106": False, "b106": False,
                                                                  "B101-1": True, "B101-2": True}
    # a Prokon concern also keeps the row out of the bulk tick
    row_id = t.loc[t["Beam mark"] == "B101-2", "Row ID"].item()
    assert dr.concern_rows(t, {row_id: "Prokon span 1 used, span 2 not in report"}, conflicts)[
        t["Beam mark"] == "B101-2"].item()


def test_unlock_rules():
    t = review_table().query("`Beam mark` != 'b106'")                       # no duplicates left
    concern = dr.concern_rows(t, {}, [])
    reasons = dr.run_blockers(t, confirmed=False, has_prokon=True, concerns=concern)
    assert reasons == ["4 of 4 AI-read row(s) not ticked, 1 of them highlighted: tick each one after checking it "
                       "against the drawing. AI vision can misread a value.",
                       f'Tick "{dr.CONFIRM_LABEL}" (needed when rows were read by AI vision).']
    t = dr.tick_unflagged(t, concern)
    assert dr.run_blockers(t, True, True, concerns=concern)[0].startswith("1 of 4 AI-read row(s) not ticked, 1 of")
    t.loc[:, "Reviewed"] = True
    assert dr.run_blockers(t, confirmed=False, has_prokon=True, concerns=concern) == [
        f'Tick "{dr.CONFIRM_LABEL}" (needed when rows were read by AI vision).']
    assert dr.run_blockers(t, confirmed=True, has_prokon=True, concerns=concern) == []
    assert dr.run_blockers(t, True, has_prokon=False) == ["Upload the Prokon report."]
    assert "appear more than once" in dr.run_blockers(review_table(), True, True, ["B106", "b106"])[0]


def test_text_layer_rows_need_no_ticks_and_no_confirmation():
    t = dr.records_to_table([(1, rec("B101-1"), {"read": dr.READ_TEXT}), (1, rec("B105", T1="3X20"), {"read": dr.READ_TEXT})])
    assert dr.run_blockers(t, confirmed=False, has_prokon=True, concerns=dr.concern_rows(t)) == []


def test_review_page_tick_button_confirmation_and_reasons():
    ex = ai_extraction([rec("B101-1"), rec("B101-2"), rec("B105", T1="3X20")])
    with drawing_page(ex) as at:
        run = next(b for b in at.button if b.label.startswith("▶ Run comparison"))
        assert run.disabled
        assert any("Run comparison is disabled because" in m.value and "3 of 3 AI-read row(s) not ticked" in m.value
                   for m in at.markdown)
        tick = next(b for b in at.button if b.label.startswith("☑ Tick all unflagged rows"))
        assert tick.label.endswith("(2)")
        tick.click().run()
        table = at.dataframe[0].value
        assert dict(zip(table["Beam mark"], table["Reviewed"])) == {"B105": False, "B101-1": True, "B101-2": True}
        assert any("1 of 3 AI-read row(s) not ticked, 1 of them highlighted" in m.value for m in at.markdown)
        at.checkbox(key=next(k for k in at.session_state.filtered_state if k.startswith("confirm_"))).check().run()
        assert not any("Tick \"I have compared" in m.value for m in at.markdown)
        assert next(b for b in at.button if b.label.startswith("▶ Run comparison")).disabled
