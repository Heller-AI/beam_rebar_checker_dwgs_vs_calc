"""The assistant tab in the real Streamlit page, with a scripted fake model (no API calls)."""

from types import SimpleNamespace as NS
from unittest import mock

from streamlit.testing.v1 import AppTest

from beam_checker import agent
from beam_checker.checker import CheckResult

ROWS = [["B101-1", "Left Support (Pos Start)", "975.1", "3H20", "942.5", "96.7%", "FAIL (Deficit)", "0.500",
         "2H10-200", "0.785", "157.0%", "OK", "FAIL", "r"]]


def run_question(final_turn):
    replies = [NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="get_failures",
                                                     input={"with_fixes": True})]), final_turn]
    fake = NS(beta=NS(messages=NS(create=lambda **kw: replies.pop(0))))
    with mock.patch.object(agent, "make_client", return_value=fake):
        at = AppTest.from_file("../app.py", default_timeout=60)
        for k, v in {"ANTHROPIC_API_KEY": "sk-ant-FAKE-KEY-1234", "APP_PASSWORD": "code-5678",
                     "ACTIVE_PROVIDER": "anthropic", "ANTHROPIC_MODEL": "claude-sonnet-5-5"}.items():
            at.secrets[k] = v
        at.session_state["result"] = CheckResult(rows=ROWS, matched_count=1)
        at.session_state["result_source"] = "Excel schedule"
        at.run()
        at.sidebar.text_input(key="access_code_input").input("code-5678")
        next(b for b in at.sidebar.button if b.label == "Sign in").click().run()
        at.chat_input[0].set_value("Suggest fixes").run()
        live = at
        at.run()                                                     # history is redrawn on the next run
        return live, at


def failure_tables(at):
    return [d.value for d in at.dataframe if "Provided / required (%)" in d.value.columns]


def test_table_is_drawn_by_the_app_and_survives_a_rerun():
    _, at = run_question(NS(stop_reason="end_turn", content=[NS(type="text", text="One FAIL, see the table.")]))
    assert not at.exception
    tables = failure_tables(at)
    assert len(tables) == 1 and tables[0].iloc[0]["Shortfall"] == "32.6"
    assert any(m.value.startswith("*Flexure: Required, Provided, Shortfall and Suggested provides in mm²") for m in at.markdown)
    assert any("1 fail flexure" in m.value for m in at.markdown)
    assert [e.label for e in at.expander if "tool call" in e.label] == ["🔧 1 tool call(s), 2 model call(s)"]


def test_cut_off_answer_shows_stop_reason_and_table():
    _, at = run_question(NS(stop_reason="max_tokens", content=[NS(type="text", text="| Beam |")]))
    assert len(failure_tables(at)) == 1
    assert any("stop reason: max_tokens" in w.value and "MAX_OUTPUT_TOKENS = 8000" in w.value for w in at.warning)


def test_untraceable_numbers_are_warned_not_blocked():
    _, at = run_question(NS(stop_reason="end_turn", content=[NS(type="text", text="Short by 32.6; also 4321.0.")]))
    assert any("Short by 32.6" in m.value for m in at.markdown)            # the answer is still shown
    assert any("could not be traced" in w.value and w.value.endswith("4321.0") for w in at.warning)


def test_excel_mode_buttons():
    _, at = run_question(NS(stop_reason="end_turn", content=[NS(type="text", text="ok")]))
    labels = [b.label for b in at.button]
    assert "Summarise the failures" in labels and "Which beams are missing from the schedule or the Prokon report?" in labels
    assert not any(label.startswith("Suggest the smallest") for label in labels)
    assert not any("pre-update" in i.value for i in at.info)

