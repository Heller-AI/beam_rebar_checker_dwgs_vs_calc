"""Drawing reader tests with mocked model responses (no API calls)."""

import io
from types import SimpleNamespace as NS
from unittest import mock

import pandas as pd
import pytest
from PIL import Image, ImageDraw

from beam_checker import checker, drawing_reader as dr
from beam_checker.prompts import EXTRACTION_RULES, extraction_system_prompt


def rec(mark="B101-1", **kw):
    base = {"beam_mark": mark, "size": "200x450", "T1": "2H16", "T2": "2H16", "T3": "2H16", "B1": "2H16",
            "B2": "2H16", "B3": "2H16", "side_bars": "", "link_type": "A1", "S1": "2H10-150", "S2": "2H10-200",
            "S3": "2H10-150", "remark": "", "confidence": "high", "flags": [], "source_note": "row 1",
            "row_box": []}
    return {**base, **kw}


def page(w=800, h=600, text=""):
    img = Image.new("L", (w, h), 255)
    ImageDraw.Draw(img).text((20, 20), "B101-1  2H16  2H10-150", fill=0)
    return dr.Page(1, "file 1, page 1", img, text)


def tool(name, inp, id_="t1"):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def msg(*blocks, stop="tool_use"):
    return NS(stop_reason=stop, content=list(blocks))


class FakeSend:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})  # snapshot: the loop keeps appending
        return self.replies.pop(0)


# ------------------------------------------------------------------ notation

@pytest.mark.parametrize("text,kind,ok", [
    ("3H20", "bar", True), ("2H13+2H13", "bar", True), ("", "bar", True), ("-", "bar", True),
    ("→", "bar", True), ("3H2O", "bar", False), ("3H20+abc", "bar", False),
    ("2H10-150", "stirrup", True), ("H10-150", "stirrup", True), ("2H10/150", "stirrup", True),
    ("2H10", "stirrup", False), ("2H10-200+2H8-200", "stirrup", False),
])
def test_check_notation(text, kind, ok):
    assert dr.check_notation(text, kind)["ok"] is ok


def test_legs_not_stated_note():
    assert "legs" in dr.check_notation("H10-150", "stirrup")["note"]


# ------------------------------------------------------------------ schema

def test_submission_schema_accepts_valid_and_rejects_bad_input():
    dr.Submission.model_validate({"records": [rec()], "page_note": ""})
    with pytest.raises(dr.ValidationError):
        dr.Submission.model_validate({"records": [rec(confidence="sure")], "page_note": ""})
    with pytest.raises(dr.ValidationError):
        dr.Submission.model_validate({"records": [{**rec(), "extra": 1}], "page_note": ""})
    with pytest.raises(dr.ValidationError):
        bad = rec(); del bad["S2"]
        dr.Submission.model_validate({"records": [bad], "page_note": ""})


def test_tool_schema_matches_pydantic_fields():
    props = dr.EXTRACTION_TOOLS[1]["input_schema"]["properties"]["records"]["items"]
    assert set(props["properties"]) == set(dr.ExtractedRecord.model_fields) == set(props["required"])
    assert all(t["eager_input_streaming"] for t in dr.EXTRACTION_TOOLS)


# ------------------------------------------------------------------ pages, images, cost

def test_small_page_is_sent_as_one_image():
    assert [label for label, _ in dr.plan_images(page())] == ["Overview of the whole page"]


def test_large_sheet_gets_overview_plus_tiles_within_vision_limits():
    big = dr.Page(1, "file 1, page 1", Image.new("L", (5046, 3564), 255), "", 841, 594)  # A1 at 6 px/mm
    images = dr.plan_images(big)
    assert len(images) == 1 + 9
    for _, img in images:
        w, h = img.size
        assert max(w, h) <= dr.MAX_IMAGE_EDGE and w * h <= dr.MAX_IMAGE_PIXELS
    assert images[1][1].size == (dr.TILE_PX, dr.TILE_PX)  # tiles keep full resolution


def test_load_pages_from_pdf_and_image_and_page_limit():
    img = page().image
    pdf_buf, png_buf = io.BytesIO(), io.BytesIO()
    img.save(pdf_buf, format="PDF", resolution=72)  # 800 x 600 pt page
    img.save(png_buf, format="PNG")
    files = [("a.pdf", pdf_buf.getvalue()), ("b.png", png_buf.getvalue())]
    assert dr.count_pages(files) == 2
    pages = dr.load_pages(files)
    assert [p.source for p in pages] == ["file 1, page 1", "file 2"]
    assert pages[0].image.mode == "L" and round(pages[0].width_mm) == 282
    with pytest.raises(ValueError, match="limit is 1"):
        dr.load_pages(files, max_pages=1)


def test_cost_estimate():
    est = dr.estimate_cost([page()], "claude-sonnet-5-5")
    assert est["pages"] == 1 and est["images"] == 1 and 0 < est["low"] < est["high"] < 1
    assert dr.estimate_cost([page()], "some-unknown-model")["low"] is None


def test_page_content_has_labelled_images_and_text_layer():
    blocks = dr.page_content(page(text="B101-1 2H16"))
    assert [b["type"] for b in blocks] == ["text", "text", "image", "text", "text"]
    assert "B101-1 2H16" in blocks[3]["text"]
    assert "no text layer" in dr.page_content(page())[3]["text"]


# ------------------------------------------------------------------ the extraction loop

def test_validate_then_submit_finishes_without_extra_call():
    send = FakeSend([
        msg(tool("validate_notation", {"items": [{"text": "2H16", "kind": "bar"}, {"text": "2H1O", "kind": "bar"}]})),
        msg(tool("submit_records", {"records": [rec()], "page_note": ""}, "t2")),
    ])
    res = dr.extract_page(send, page(), "claude-sonnet-5-5", "rules")
    assert res.requests == 2 and res.records[0]["beam_mark"] == "B101-1"
    first = send.calls[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["tools"] == dr.EXTRACTION_TOOLS and first["fallbacks"] == "default"
    # the validation tool result went back to the model with the parser's verdicts
    results = send.calls[1]["messages"][-1]["content"][0]["content"]
    assert '"ok": true' in results and '"ok": false' in results


def test_unparseable_notation_gets_one_chance_to_resubmit():
    send = FakeSend([
        msg(tool("submit_records", {"records": [rec(T1="2H1G")], "page_note": ""})),
        msg(tool("submit_records", {"records": [rec(T1="2H16")], "page_note": ""}, "t2")),
    ])
    res = dr.extract_page(send, page(), "m", "rules")
    assert res.records[0]["T1"] == "2H16" and res.requests == 2
    feedback = send.calls[1]["messages"][-1]["content"][0]["content"]
    assert "fields_that_do_not_parse" in feedback and "T1" in feedback


def test_model_may_keep_text_as_written_and_finish():
    send = FakeSend([
        msg(tool("submit_records", {"records": [rec(T1="2H1G")], "page_note": ""})),
        msg(NS(type="text", text="done"), stop="end_turn"),
    ])
    assert dr.extract_page(send, page(), "m", "rules").records[0]["T1"] == "2H1G"


def test_invalid_tool_input_is_returned_as_error_and_retried():
    send = FakeSend([
        msg(tool("submit_records", {"records": [{"beam_mark": "B1"}], "page_note": ""})),
        msg(tool("submit_records", {"records": [rec()], "page_note": ""}, "t2")),
    ])
    dr.extract_page(send, page(), "m", "rules")
    err = send.calls[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True and "INVALID_INPUT" in err["content"]


def test_text_reply_is_nudged_once_then_fails():
    send = FakeSend([msg(NS(type="text", text="Here are the beams..."), stop="end_turn"),
                     msg(tool("submit_records", {"records": [], "page_note": "no schedule"}))])
    assert dr.extract_page(send, page(), "m", "rules").note == "no schedule"

    send = FakeSend([msg(NS(type="text", text="a"), stop="end_turn"), msg(NS(type="text", text="b"), stop="end_turn")])
    with pytest.raises(dr.ExtractionError, match="did not submit"):
        dr.extract_page(send, page(), "m", "rules")


@pytest.mark.parametrize("stop,match", [("max_tokens", "more rows"), ("refusal", "declined")])
def test_truncated_or_refused_page_fails_clearly(stop, match):
    send = FakeSend([msg(tool("submit_records", {"records": [rec()], "page_note": ""}), stop=stop)])
    with pytest.raises(dr.ExtractionError, match=match):
        dr.extract_page(send, page(), "m", "rules")


def test_unreadable_stream_is_reissued_once():
    calls = []

    def send(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("bad json")
        return msg(tool("submit_records", {"records": [rec()], "page_note": ""}))

    assert dr.extract_page(send, page(), "m", "rules").requests == 2


def test_on_request_counts_every_model_call():
    n = []
    send = FakeSend([msg(tool("validate_notation", {"items": []})),
                     msg(tool("submit_records", {"records": [rec()], "page_note": ""}, "t2"))])
    dr.extract_page(send, page(), "m", "rules", on_request=lambda: n.append(1))
    assert len(n) == 2


def test_failed_page_does_not_stop_other_pages_but_budget_does():
    p1, p2 = page(), page()
    p2.number = 2
    send = FakeSend([msg(NS(type="text", text="?"), stop="end_turn"), msg(NS(type="text", text="?"), stop="end_turn"),
                     msg(tool("submit_records", {"records": [rec()], "page_note": ""}))])
    ex = dr.extract_drawing([p1, p2], send, "m")
    assert list(ex.page_errors) == [1] and list(ex.table["Beam mark"]) == ["B101-1"]

    from beam_checker.access import BudgetExceeded

    def stop():
        raise BudgetExceeded("limit")

    with pytest.raises(BudgetExceeded):
        dr.extract_drawing([p1], FakeSend([]), "m", on_request=stop)


# ------------------------------------------------------------------ checks after extraction

def flags_of(table, mark):
    return set(table.loc[table["Beam mark"] == mark, "Flags"].iloc[0].split(", ")) - {""}


def test_post_checks_flag_conflicts_continuity_symbols_and_bad_notation():
    table = dr.records_to_table([
        (1, rec("B101-1", T3="3H20")),
        (1, rec("B101-2", T1="2H20")),                      # does not match B101-1 T3
        (1, rec("B102", T1="→")),                            # symbol left as written
        (1, rec("B108", T2="-", T3="")),                     # a plain dash is not a ditto mark
        (1, rec("B103", S1="H10-150", S2="H10-200", S3="H10-150")),
        (1, rec("B104", B1="2HI6")),
        (1, rec("B105", size="200x225/175")),
        (1, rec("B106", T2="2H20")), (2, rec("B106", T2="3H20")),   # same mark, different values
        (2, rec("B107")), (2, rec("B107")),                  # identical duplicate (tile overlap)
    ])
    assert "continuity_mismatch" in flags_of(table, "B101-1") and "continuity_mismatch" in flags_of(table, "B101-2")
    assert "ditto_unconfirmed" in flags_of(table, "B102")
    assert flags_of(table, "B108") == set()
    assert "legs_not_stated" in flags_of(table, "B103")
    assert "notation_invalid" in flags_of(table, "B104")
    assert table.loc[table["Beam mark"] == "B104", "Confidence"].iloc[0] == "low"
    assert flags_of(table, "B105") == {"tapered_size"}
    assert table.loc[table["Beam mark"] == "B105", "Review"].iloc[0] == ""   # info flag only
    assert (table["Beam mark"] == "B106").sum() == 2 and "conflict" in flags_of(table, "B106")
    assert (table["Beam mark"] == "B107").sum() == 1
    assert list(table["Review"]).index("") == (table["Review"] != "").sum()  # rows to check come first
    assert dr.table_conflicts(table) == ["B106"]


def test_reviewed_table_runs_through_the_existing_comparison():
    table = dr.records_to_table([(1, rec("B101-1", T1="3H16", T3="", B2="", B3="9H40", S3=""))])
    records = dr.table_to_records(table)
    assert records[0].rows == [{"t1": "3H16", "t2": "2H16", "t3": "", "b1": "2H16", "b2": "",
                                "st_l": "2H10-150", "st_m": "2H10-200", "st_r": ""}]
    req = {"req_t1": 500.0, "req_b2": 300.0, "req_t3": 450.0, "req_asv_l": 0.5, "req_asv_m": 0.3, "req_asv_r": 0.5}
    with mock.patch.object(checker, "extract_all_beams_from_pdf", return_value={"B101": {1: req}}):
        res = checker.run_comparison_records(records, "unused.pdf", remarks=dr.DRAWING_REMARKS)
    left, mid, right = res.rows
    assert left[3] == "3H16" and right[3] == "3H16"     # existing cantilever rule: blank end copies the other end
    assert mid[3] == "2H16"                             # B2 blank -> B1; B3 is not used
    assert [r[13] for r in res.rows] == list(dr.DRAWING_REMARKS)


def test_table_to_records_skips_blank_marks_and_handles_nan():
    table = dr.records_to_table([(1, rec("B101"))])
    table.loc[len(table)] = [None] * len(table.columns)
    table.loc[0, "T2"] = float("nan")
    records = dr.table_to_records(table)
    assert len(records) == 1 and records[0].rows[0]["t2"] == ""


def test_fingerprint_changes_with_files_model_and_rules():
    files = [("a.pdf", b"123")]
    base = dr.files_fingerprint(files, "m")
    assert base == dr.files_fingerprint([("renamed.pdf", b"123")], "m")
    assert base != dr.files_fingerprint([("a.pdf", b"124")], "m")
    assert base != dr.files_fingerprint(files, "m2")
    assert base != dr.files_fingerprint(files, "m", "extra")


# ------------------------------------------------------------------ prompts

def test_extra_rules_are_appended_only_when_set():
    assert extraction_system_prompt("") == EXTRACTION_RULES
    assert extraction_system_prompt("  Rule X  ").endswith("Rule X")


def test_assumed_link_type_without_legs_is_not_highlighted_but_noted():
    from beam_checker.parsers import ASSUMED_LINK

    no_legs = {"S1": "H10-150", "S2": "H10-200", "S3": "H10-150"}
    table = dr.records_to_table([
        (1, rec("B101", link_type=ASSUMED_LINK.link_type, **no_legs)),      # assumed default: not highlighted
        (1, rec("B102", link_type="a1 ", **no_legs)),                       # same type, written loosely
        (1, rec("B103", link_type="A2", **no_legs)),                        # another type: highlighted
        # no link type: not highlighted (Excel/PDF/AI layouts often have no link type column; the 2-leg assumption
        # for them is unchanged existing behaviour), the flag stays in the Flags column
        (1, rec("B104", link_type="", **no_legs)),
        (1, rec("B105", link_type=ASSUMED_LINK.link_type)),                 # legs stated: nothing to note
    ])
    review = dict(zip(table["Beam mark"], table["Review"]))
    assert all("legs_not_stated" in flags_of(table, m) for m in ("B101", "B102", "B103", "B104"))   # flag kept
    assert review["B101"] == review["B102"] == review["B104"] == "" and review["B103"] == "⚠ check"
    assert dr.assumed_legs_note(table) == (f"2 rows: link type {ASSUMED_LINK.link_type} has no leg count; "
                                           f"{ASSUMED_LINK.legs} legs assumed. Confirm against the drawing legend.")
    one = dr.records_to_table([(1, rec("B101", link_type="A1", **no_legs))])
    assert dr.assumed_legs_note(one).startswith("1 row: link type A1")
    assert dr.assumed_legs_note(dr.records_to_table([(1, rec("B101")), (1, rec("B103", link_type="A2", **no_legs))])) == ""
    assert dr.needs_review("high", "legs_not_stated", "A2") and not dr.needs_review("high", "legs_not_stated", "A1")
    assert not dr.needs_review("high", "legs_not_stated") and not dr.needs_review("high", "legs_not_stated", "")
    assert dr.needs_review("high", "legs_not_stated, continuity_mismatch", "A1")       # other concerns still count


def test_tick_all_skips_exactly_the_highlighted_leg_rows():
    no_legs = {"S1": "H10-150", "S2": "H10-200", "S3": "H10-150"}
    table = dr.records_to_table([(1, rec("B101", link_type="A1", **no_legs)), (1, rec("B102", link_type="", **no_legs)),
                                 (1, rec("B103", link_type="A2", **no_legs))])
    concern = dict(zip(table["Beam mark"], dr.concern_rows(table)))
    assert concern == {"B101": False, "B102": False, "B103": True}                   # same rows as the highlight
