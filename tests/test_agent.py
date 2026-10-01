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
    bars = agent.execute_tool("suggest_bars", {"required_as_mm2": 500, "beam_width_mm": 300}, RESULT)
    assert bars["area"] >= 500 and bars["fit"] == "fits the width" and bars["beam_width_mm"] == 300
    unknown = agent.execute_tool("suggest_bars", {"required_as_mm2": 500, "beam_width_mm": 0}, RESULT)
    assert unknown["fit"] == "width unknown, fit not checked"
    links = agent.execute_tool("suggest_stirrups", {"required_asv_sv": 1.0, "max_spacing_mm": 150,
                                                    "beam_width_mm": 300}, RESULT)
    assert links["asv_sv"] >= 1.0 and int(links["suggestion"].split("-")[1]) <= 150


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

    assert answer.text == "2 rows fail." and answer.stop == "end_turn" and answer.requests == 2
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

    assert answer.text == "B101-1 fails flexure." and answer.requests == 2
    assert bodies[0]["messages"][0]["role"] == "system" and bodies[0]["tool_choice"] == "auto"
    assert messages[2]["role"] == "tool" and messages[2]["tool_call_id"] == "c1"
    assert len(json.loads(messages[2]["content"])["rows"]) == 3
    assert [m["role"] for m in bodies[1]["messages"]] == ["system", "user", "assistant", "tool"]


def test_zhipu_bad_key_raises_friendly_error():
    import pytest
    with pytest.raises(agent.ProviderError, match="rejected"):
        agent.ask_zhipu("bad", [{"role": "user", "content": "x"}], RESULT, post=lambda *a, **k: FakeResp({}, 401))


def test_on_request_runs_before_every_model_call_and_can_stop_the_loop():
    client = FakeClient([
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="get_summary", input={})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="done")]),
    ])
    calls = []
    agent.ask(client, [{"role": "user", "content": "x"}], RESULT, on_request=lambda: calls.append(1))
    assert len(calls) == 2

    import pytest
    from beam_checker import access

    def stop():
        raise access.BudgetExceeded("limit")

    client = FakeClient([])
    out = agent.ask(client, [{"role": "user", "content": "x"}], RESULT, on_request=stop)
    assert out.stop == "call_cap" and out.text == "limit" and out.requests == 0
    assert client.requests == []  # nothing was sent


def test_model_list_and_default():
    assert agent.DEFAULT_MODEL == "claude-sonnet-5-5"
    assert list(agent.MODELS) == ["claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5"]


def _api_error(cls, status):
    import httpx2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=req), body=None)


def test_unknown_model_gives_clear_message_not_a_crash():
    import anthropic

    def create(**kw):
        raise _api_error(anthropic.NotFoundError, 404)

    client = NS(beta=NS(messages=NS(create=create)))
    try:
        agent.ask(client, [{"role": "user", "content": "x"}], RESULT, model="claude-old-model")
    except anthropic.APIError as e:
        msg = agent.describe_anthropic_error(e, "claude-old-model")
    assert "unknown or has been retired" in msg and "claude-old-model" in msg


def test_other_error_messages():
    import anthropic
    assert "rejected" in agent.describe_anthropic_error(_api_error(anthropic.AuthenticationError, 401), "m")
    assert "not allowed" in agent.describe_anthropic_error(_api_error(anthropic.PermissionDeniedError, 403), "m")
    assert "rate limit" in agent.describe_anthropic_error(_api_error(anthropic.RateLimitError, 429), "m")
    assert "(500)" in agent.describe_anthropic_error(_api_error(anthropic.InternalServerError, 500), "m")


def test_schedule_source_tool_only_in_drawing_mode():
    assert "get_schedule_source" not in [t["name"] for t in agent.tools_for(None)]
    assert "get_schedule_source" in [t["name"] for t in agent.tools_for({"x": 1})]
    assert [t["function"]["name"] for t in agent.zhipu_tools_for({"x": 1})][-1] == "get_schedule_source"
    schedule = {"reading_method": "PDF text layer", "drawing_beams_not_in_prokon": ["B999"]}
    assert agent.execute_tool("get_schedule_source", {}, RESULT, schedule) == schedule
    assert "error" in agent.execute_tool("get_schedule_source", {}, RESULT, None)


def test_assistant_uses_schedule_tool_with_claude():
    client = FakeClient([
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="get_schedule_source", input={})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="B999 is only on the drawing.")]),
    ])
    sent_tools = []
    real_create = client.beta.messages.create
    client.beta.messages.create = lambda **kw: (sent_tools.append([t["name"] for t in kw["tools"]]), real_create(**kw))[1]
    messages = [{"role": "user", "content": "Which beams are only on the drawing?"}]
    answer = agent.ask(client, messages, RESULT, schedule={"drawing_beams_not_in_prokon": ["B999"]})
    assert answer.text == "B999 is only on the drawing."
    assert "get_schedule_source" in sent_tools[0]
    assert "B999" in messages[2]["content"][0]["content"]
