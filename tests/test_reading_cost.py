"""AI reading allowance: counted once a response arrives; partial readings kept; failed pages re-read only."""

from datetime import date

import pytest

from beam_checker import access, drawing_reader as dr
from tests.test_drawing_reader import FakeSend, msg, page, rec, tool


class FakeAPIError(Exception):
    pass


def submit(mark):
    return msg(tool("submit_records", {"records": [rec(mark)], "page_note": ""}))


def pages(n):
    out = []
    for i in range(1, n + 1):
        p = page()
        p.number = i
        out.append(p)
    return out


def test_check_call_counts_nothing_and_count_call_counts():
    state, counter, day = {}, access.DailyCounter(), date(2026, 1, 1)
    access.check_call(state, "drawing", 2, counter, 5, today=day)
    assert access.session_calls_used(state, "drawing") == 0 and counter.used(day) == 0
    access.count_call(state, "drawing", counter, today=day)
    access.count_call(state, "drawing", counter, today=day)
    assert access.session_calls_used(state, "drawing") == 2 and counter.used(day) == 2
    with pytest.raises(access.BudgetExceeded, match="2 AI calls"):
        access.check_call(state, "drawing", 2, counter, 5, today=day)


def test_failed_request_is_not_counted():
    counted = []

    def send(**kw):
        raise FakeAPIError("overloaded")

    with pytest.raises(FakeAPIError):
        dr.extract_page(send, page(), "m", "rules", on_response=lambda: counted.append(1))
    assert counted == []
    dr.extract_page(FakeSend([submit("B101-1")]), page(), "m", "rules", on_response=lambda: counted.append(1))
    assert counted == [1]


def test_pages_read_before_an_error_are_kept():
    class Send(FakeSend):
        def __call__(self, **kw):
            if not self.replies:
                raise FakeAPIError("overloaded")
            return super().__call__(**kw)

    ex = dr.extract_drawing(pages(3), Send([submit("B101-1")]), "m", stop_on=(FakeAPIError,))
    assert isinstance(ex.stopped, FakeAPIError)
    assert list(ex.table["Beam mark"]) == ["B101-1"]
    assert ex.page_errors == {2: dr.STOPPED_NOTE, 3: dr.STOPPED_NOTE}

    # reading again sends only pages 2 and 3; page 1 is kept, not paid for again
    send = FakeSend([submit("B102"), submit("B103")])
    again = dr.extract_drawing(pages(3), send, "m", stop_on=(FakeAPIError,), previous=ex)
    assert len(send.calls) == 2 and again.page_errors == {} and again.stopped is None
    assert sorted(again.table["Beam mark"]) == ["B101-1", "B102", "B103"]
    assert again.requests == ex.requests + 2


def test_budget_stop_keeps_pages_when_asked_and_raises_otherwise():
    calls = []

    def on_request():
        if len(calls) == 1:
            raise access.BudgetExceeded("limit")
        calls.append(1)

    ex = dr.extract_drawing(pages(2), FakeSend([submit("B101-1")]), "m", on_request=on_request,
                            stop_on=(access.BudgetExceeded,))
    assert list(ex.table["Beam mark"]) == ["B101-1"] and list(ex.page_errors) == [2]
    with pytest.raises(access.BudgetExceeded):
        dr.extract_drawing(pages(1), FakeSend([]), "m", on_request=lambda: (_ for _ in ()).throw(
            access.BudgetExceeded("limit")))
