"""Readable answers: stop reasons, call caps, kept tables and the number-tracing check (mocked, no API calls)."""

from types import SimpleNamespace as NS

from beam_checker import agent
from beam_checker.access import BudgetExceeded
from beam_checker.checker import CheckResult

ROWS = [
    ["B101-1", "Left Support (Pos Start)", "975.1", "3H20", "942.5", "96.7%", "FAIL (Deficit)", "0.500", "2H10-200", "0.785", "157.0%", "OK", "FAIL", "r"],
    ["B101-1", "Mid-Span (Max Bot)", "300.0", "2H16", "402.1", "134.0%", "OK", "0.500", "2H10-200", "0.785", "157.0%", "OK", "OK", "r"],
]
RESULT = CheckResult(rows=ROWS, matched_count=1)
TOOL_TURN = NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="get_failures", input={"with_fixes": True})])


def client_with(*replies):
    replies = list(replies)
    calls = []

    def create(**kw):
        calls.append(kw)
        return replies.pop(0)

    return NS(beta=NS(messages=NS(create=create))), calls


def test_normal_answer_keeps_the_table_and_uses_low_effort():
    client, calls = client_with(TOOL_TURN, NS(stop_reason="end_turn", content=[NS(type="text", text="One flexure FAIL.")]))
    out = agent.ask(client, [{"role": "user", "content": "Suggest fixes"}], RESULT)
    assert (out.stop, out.requests, out.text) == ("end_turn", 2, "One flexure FAIL.")
    assert len(out.tables) == 1 and out.tables[0]["rows"][0]["shortfall"] == 32.6
    assert calls[0]["output_config"] == {"effort": "low"} and out.stop_message == ""


def test_cut_off_answer_reports_the_stop_reason_and_keeps_the_table():
    client, _ = client_with(TOOL_TURN, NS(stop_reason="max_tokens", content=[NS(type="text", text="| Beam | Pos")]))
    out = agent.ask(client, [{"role": "user", "content": "Suggest fixes"}], RESULT, max_tokens=500)
    assert out.stop == "max_tokens" and "output limit" in out.stop_message and "MAX_OUTPUT_TOKENS" in out.stop_message
    assert out.text == "| Beam | Pos" and len(out.tables) == 1          # partial text and the full table


def test_cut_off_with_no_text_at_all_still_has_the_table():
    client, _ = client_with(TOOL_TURN, NS(stop_reason="max_tokens", content=[NS(type="thinking", thinking="")]))
    out = agent.ask(client, [{"role": "user", "content": "x"}], RESULT)
    assert out.stop == "max_tokens" and out.text == "" and len(out.tables) == 1


def test_call_cap_reached_mid_question_returns_what_was_computed():
    used = []

    def on_request():
        if used:
            raise BudgetExceeded("This session has used its 1 AI calls on the shared key.")
        used.append(1)

    client, calls = client_with(TOOL_TURN)
    out = agent.ask(client, [{"role": "user", "content": "x"}], RESULT, on_request=on_request)
    assert out.stop == "call_cap" and "1 AI calls" in out.text and out.requests == 1 and len(calls) == 1
    assert len(out.tables) == 1


def test_round_limit():
    client, calls = client_with(*[TOOL_TURN] * agent.MAX_TOOL_ROUNDS)
    out = agent.ask(client, [{"role": "user", "content": "x"}], RESULT)
    assert out.stop == "round_limit" and out.requests == agent.MAX_TOOL_ROUNDS == 6 and len(calls) == 6


def test_zhipu_returns_the_same_result_shape():
    class Resp:
        def __init__(self, data):
            self.data, self.status_code, self.text = data, 200, ""

        def json(self):
            return self.data

    replies = [Resp({"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                   {"id": "c1", "type": "function", "function": {"name": "get_failures", "arguments": '{"with_fixes": false}'}}]}}]}),
               Resp({"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]})]
    out = agent.ask_zhipu("k", [{"role": "user", "content": "x"}], RESULT, post=lambda *a, **k: replies.pop(0))
    assert (out.stop, out.text, out.requests, len(out.tables)) == ("max_tokens", "partial", 2, 1)


def test_tracing_check_lists_only_numbers_not_in_tool_results():
    client, _ = client_with(TOOL_TURN, NS(stop_reason="end_turn", content=[NS(type="text", text="")]))
    messages = [{"role": "user", "content": "Why does B101-1 fail?"}]
    agent.ask(client, messages, RESULT)
    answer = ("B101-1 left support needs 975.1 mm² but 3H20 gives 942.5 (96.7 %), short by 32.6. "
              "Use 2H10-200 links in the 300x1000 beam. Invented: 1234.5 and 77.7 %.")
    assert agent.untraceable_numbers(answer, messages, "Why does B101-1 fail?") == ["1234.5", "77.7"]


def test_tracing_check_ignores_small_counts_and_rounding():
    messages = [{"role": "tool", "content": '{"required": 975.12, "pct": 96.66}'}]
    assert agent.untraceable_numbers("2 layers, 975.1 required, 96.7 %", messages) == []
