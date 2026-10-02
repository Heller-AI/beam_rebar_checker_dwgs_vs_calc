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
        at.session_state["schedule_source"] = "Drawing (PDF or image)"
        at.run()
        yield at


def test_review_page_lists_prokon_concerns():
    ex = ai_extraction([rec("B101-1"), rec("B102-2"), rec("B999")])
    with drawing_page(ex) as at:
        assert not at.exception
        exp = next(e for e in at.expander if "need a look against the Prokon report" in e.label)
        assert exp.label.startswith("⚠ 2 row(s)")
        assert any("2 highlighted (yellow)" in c.value for c in at.caption)
