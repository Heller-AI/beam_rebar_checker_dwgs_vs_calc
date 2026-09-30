"""Agent tool tests plus a loop test with a fake client (no API calls, no cost)."""

import json
from types import SimpleNamespace as NS

from beam_checker import agent
from beam_checker.checker import CheckResult

ROWS = [
    ["B101-1", "Left Support (Pos Start)", "500.0", "2H16", "402.1", "80.4%", "FAIL (Deficit)", "0.500", "2H10-200", "0.785", "157.0%", "OK", "FAIL", "r"],
    ["B101-1", "Mid-Span (Max Bot)", "300.0", "2H16", "402.1", "134.0%", "OK", "0.500", "2H10-200", "0.785", "157.0%", "OK", "OK", "r"],
    ["B101-2", "Left Support (Pos Start)", "300.0", "2H16", "402.1", "134.0%", "OK", "1.000", "2H10-200", "0.785", "78.5%", "FAIL (Deficit)", "FAIL", "r"],
]
RESULT = CheckResult(rows=ROWS, pdf_beam_names={"B101", "B999"}, excel_beam_names={"B101"}, pdf_matched_bases={"B101"}, matched_count=2)


def test_summary_and_filters():
    s = agent.execute_tool("get_summary", {}, RESULT)
    assert s["rows_fail"] == 2 and s["rows_fail_flexure"] == 1 and s["rows_fail_shear"] == 1
    assert s["in_pdf_missing_in_excel"] == ["B999"]
    assert agent.execute_tool("list_rows", {"status": "FAIL", "check": "SHEAR"}, RESULT)["count"] == 1


def test_beam_detail_base_and_span_mark():
    assert len(agent.execute_tool("get_beam_detail", {"beam_mark": "b101"}, RESULT)["rows"]) == 3
    assert len(agent.execute_tool("get_beam_detail", {"beam_mark": "B101-2"}, RESULT)["rows"]) == 1
    assert "error" in agent.execute_tool("get_beam_detail", {"beam_mark": "X1"}, RESULT)


def test_evaluate_and_suggest():
    assert agent.execute_tool("evaluate_rebar", {"notation": "3H16", "required_as_mm2": 500}, RESULT)["ok"]
    assert not agent.execute_tool("evaluate_stirrup", {"notation": "2H10-200", "required_asv_sv": 1.0}, RESULT)["ok"]
    bars = agent.execute_tool("suggest_bars", {"required_as_mm2": 500, "max_bars": 4}, RESULT)["options"]
    assert bars and all(o["area_mm2"] >= 500 for o in bars)
    assert bars == sorted(bars, key=lambda o: o["area_mm2"])
    links = agent.execute_tool("suggest_stirrups", {"required_asv_sv": 1.0, "max_spacing_mm": 150}, RESULT)["options"]
    assert links and all(o["asv_sv"] >= 1.0 for o in links)


class FakeClient:
    """Replays scripted responses and records the requests."""

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw["messages"], default=str)))
        return self.responses.pop(0)


def test_agent_loop_runs_tools_then_answers():
    client = FakeClient([
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="get_summary", input={})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="2 rows fail.")]),
    ])
    messages = [{"role": "user", "content": "Summarise"}]
    called = []
    answer = agent.ask(client, messages, RESULT, on_tool=lambda n, a: called.append(n))

    assert answer == "2 rows fail."
    assert called == ["get_summary"]
    tool_result = messages[2]["content"][0]
    assert tool_result["tool_use_id"] == "t1" and json.loads(tool_result["content"])["rows_fail"] == 2
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]


def test_agent_loop_reports_tool_errors_to_model():
    client = FakeClient([
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="nope", input={})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="ok")]),
    ])
    messages = [{"role": "user", "content": "x"}]
    agent.ask(client, messages, RESULT)
    assert messages[2]["content"][0]["is_error"] is True


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self.data


def test_zhipu_loop_runs_tools_then_answers():
    replies = [
        FakeResp({"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_beam_detail", "arguments": '{"beam_mark": "B101"}'}}]}}]}),
        FakeResp({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "B101-1 fails flexure."}}]}),
    ]
    bodies = []

    def post(url, headers, json, timeout):
        bodies.append(json)
        return replies.pop(0)

    messages = [{"role": "user", "content": "Why does B101 fail?"}]
    answer = agent.ask_zhipu("k", messages, RESULT, post=post)

    assert answer == "B101-1 fails flexure."
    assert bodies[0]["messages"][0]["role"] == "system" and bodies[0]["tool_choice"] == "auto"
    assert messages[2]["role"] == "tool" and messages[2]["tool_call_id"] == "c1"
    assert len(json.loads(messages[2]["content"])["rows"]) == 3
    assert [m["role"] for m in bodies[1]["messages"]] == ["system", "user", "assistant", "tool"]


def test_zhipu_bad_key_raises_friendly_error():
    import pytest
    with pytest.raises(agent.ProviderError, match="rejected"):
        agent.ask_zhipu("bad", [{"role": "user", "content": "x"}], RESULT, post=lambda *a, **k: FakeResp({}, 401))
