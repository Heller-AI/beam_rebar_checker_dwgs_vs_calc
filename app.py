"""Streamlit front-end for the Prokon vs Beam Schedule reinforcement checker.

Run locally:  streamlit run app.py
"""

import io
import math
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st

from beam_checker import EXCEL_FORMATS, run_comparison
from beam_checker import access, agent

st.set_page_config(page_title="Beam Rebar Checker", page_icon="🏗️", layout="wide")

FAIL_STYLE = "background-color: #FFC7CE; color: #9C0006"
ODD_STYLE = "background-color: rgba(128, 128, 128, 0.08)"


def pick_default_sheet(sheets):
    for s in sheets:
        if "BEAM" in s.upper() or "SCHEDULE" in s.upper():
            return sheets.index(s)
    return 0


def style_results(df):
    """Red rows for FAIL, alternating shading per beam mark otherwise."""
    beam_group = (df["Beam Mark"] != df["Beam Mark"].shift()).cumsum()

    def row_style(row):
        if row["Overall Status"] == "FAIL":
            style = FAIL_STYLE
        elif beam_group[row.name] % 2:
            style = ODD_STYLE
        else:
            style = ""
        return [style] * len(row)

    return df.style.apply(row_style, axis=1)


def to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Rebar & Stirrup Detail Check", index=False)
    return buf.getvalue()


EXAMPLE_QUESTIONS = [
    "Summarise the failures",
    "Why does each failing beam fail, and by how much?",
    "Suggest the smallest bar or stirrup change to fix each FAIL",
    "Which beams are missing from the schedule or the Prokon report?",
]


PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "key": "ANTHROPIC_API_KEY", "model": "ANTHROPIC_MODEL",
                  "default_model": agent.DEFAULT_MODEL, "console": "console.anthropic.com"},
    "zhipu": {"label": "Zhipu (GLM)", "key": "ZHIPU_API_KEY", "model": "ZHIPU_MODEL",
              "default_model": agent.DEFAULT_ZHIPU_MODEL, "console": "open.bigmodel.cn"},
}


def render_assistant(result, provider, api_key, model, key_mode, limits, secrets):
    st.caption(
        "Ask questions about the results. Numbers come from the checker's own formulas via tools; "
        "suggestions still need an engineer's review."
    )
    # Claude and GLM use different message formats, so switching provider starts a new conversation
    if st.session_state.get("chat_provider") != provider:
        st.session_state["chat_provider"] = provider
        st.session_state["chat_display"], st.session_state["chat_api"] = [], []
    history = st.session_state.setdefault("chat_display", [])
    api_messages = st.session_state.setdefault("chat_api", [])

    if not api_key:
        st.info(f"Enter a {PROVIDERS[provider]['label']} API key or access code in the sidebar to use the assistant.")
        return

    for msg in history:
        with st.chat_message(msg["role"]):
            if msg.get("tools"):
                with st.expander(f"🔧 {len(msg['tools'])} tool call(s)"):
                    for t in msg["tools"]:
                        st.code(t, language="text")
            st.markdown(msg["text"])

    cols = st.columns(len(EXAMPLE_QUESTIONS))
    clicked = next((q for c, q in zip(cols, EXAMPLE_QUESTIONS) if c.button(q, width="stretch")), None)
    question = st.chat_input("Ask about the results...") or clicked
    if history and st.button("🗑 Clear conversation"):
        st.session_state["chat_display"], st.session_state["chat_api"] = [], []
        st.rerun()

    if not question:
        return

    history.append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        tool_log = []
        with st.status("Thinking...", expanded=False) as status:
            def on_tool(name, args):
                line = f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
                tool_log.append(line)
                status.update(label=f"Running {name}...")
                status.write(access.redact(line, secrets))

            def on_request():
                # Only the owner's shared key is capped; your own key or the owner's local .env key is not
                if key_mode == "shared":
                    access.consume_call(st.session_state, "chat", limits["session"], daily_counter(), limits["daily"])

            api_messages.append({"role": "user", "content": question})
            kwargs = dict(model=model, on_tool=on_tool, on_request=on_request, max_tokens=limits["max_tokens"])
            try:
                if provider == "zhipu":
                    answer = agent.ask_zhipu(api_key, api_messages, result, **kwargs)
                else:
                    answer = agent.ask(agent.make_client(api_key), api_messages, result, **kwargs)
                status.update(label=f"Done ({len(tool_log)} tool call(s))", state="complete")
            except access.BudgetExceeded as e:
                answer = f"🛑 {e}"
                status.update(label="Limit reached", state="error")
            except agent.ProviderError as e:
                answer = f"❌ {e}"
                status.update(label="Error", state="error")
            except anthropic.APIError as e:
                answer = f"❌ {agent.describe_anthropic_error(e, model)}"
                status.update(label="Error", state="error")
        answer = access.redact(answer, secrets)
        tool_log = [access.redact(t, secrets) for t in tool_log]
        st.markdown(answer)

    history.append({"role": "assistant", "text": answer, "tools": tool_log})


def read_env_file(path=Path(__file__).parent / ".env"):
    """Minimal .env reader (KEY=value lines). The .env file is git-ignored, so it only exists on your PC."""
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


PLACEHOLDERS = ("your-key-here", "choose-a-long-random-code")


def is_placeholder(value):
    """Example values from .env.example / secrets.toml.example must never act as real keys or codes."""
    return any(p in value for p in PLACEHOLDERS)


def get_setting(name, env):
    """Streamlit secrets (cloud) first, then the local .env file."""
    try:
        if name in st.secrets and not is_placeholder(str(st.secrets[name])):
            return str(st.secrets[name]), "secrets"
    except Exception:  # no secrets.toml
        pass
    value = env.get(name, "")
    if value and not is_placeholder(value):
        return value, ".env"
    return "", None


def int_setting(name, env, default):
    raw = get_setting(name, env)[0]
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@st.cache_resource
def daily_counter():
    """One counter for the whole server process (all sessions). Resets when the app restarts."""
    return access.DailyCounter()


def render_sign_in(password):
    wait = access.lock_remaining_seconds(st.session_state)
    if wait:
        st.error(f"Too many wrong codes. Try again in {math.ceil(wait / 60)} min.")
        return
    with st.form("access_form", clear_on_submit=True, border=False):
        code = st.text_input("Access code", type="password", help="Ask the app owner for the code.")
        submitted = st.form_submit_button("Sign in", type="primary")
    if not submitted:
        return
    outcome = access.check_access_code(st.session_state, code, password)
    if outcome in ("ok", "locked"):
        st.rerun()
    elif outcome == "wrong":
        st.error(f"Wrong access code. {access.attempts_left(st.session_state)} attempt(s) left.")
    else:
        st.warning("Enter the access code.")


def resolve_api_key(provider, env):
    """Return (api_key, mode). mode: "shared" (owner key via access code, capped),
    "owner" (owner key from the local .env without a code), "own" (user's key) or None.
    """
    cfg = PROVIDERS[provider]
    owner_key, key_source = get_setting(cfg["key"], env)
    password, _ = get_setting("APP_PASSWORD", env)
    code_mode = bool(password) and any(get_setting(p["key"], env)[0] for p in PROVIDERS.values())
    shared_key = ""

    if code_mode:
        if access.is_signed_in(st.session_state, password):
            if owner_key:
                st.success("Signed in with the access code.")
                shared_key = owner_key
            else:
                st.info(f"Signed in, but no shared {cfg['label']} key is set up. Switch provider or use your own key.")
            if st.button("Sign out"):
                access.sign_out(st.session_state)
                st.rerun()
        else:
            render_sign_in(password)
    elif owner_key and key_source == ".env":
        st.success("Using the API key from your local .env file.")
        return owner_key, "owner"
    elif owner_key:
        st.warning("A shared API key is configured but APP_PASSWORD is not set, so it is not used.")

    own_label = f"Your {cfg['label']} API key"
    own_help = f"Get one at {cfg['console']}. It is kept only in your browser session and is not stored."
    if code_mode:
        with st.expander("Use my own API key instead"):
            own_key = st.text_input(own_label, type="password", key=f"own_key_{provider}", help=own_help)
    else:
        own_key = st.text_input(own_label, type="password", key=f"own_key_{provider}", help=own_help)

    if own_key:
        return own_key, "own"
    if shared_key:
        return shared_key, "shared"
    return "", None


with st.sidebar:
    st.header("🤖 AI Assistant settings")
    env = read_env_file()
    default_provider = get_setting("ACTIVE_PROVIDER", env)[0].lower()
    provider_ids = list(PROVIDERS)
    ai_provider = st.radio(
        "AI provider",
        provider_ids,
        index=provider_ids.index(default_provider) if default_provider in provider_ids else 0,
        format_func=lambda p: PROVIDERS[p]["label"],
        horizontal=True,
    )
    api_key, key_mode = resolve_api_key(ai_provider, env)
    limits = {
        "session": int_setting("MAX_AI_CALLS_PER_SESSION", env, 30),
        "daily": int_setting("MAX_AI_CALLS_PER_DAY", env, 300),
        "max_tokens": int_setting("MAX_OUTPUT_TOKENS", env, 4000),
    }
    calls_left_slot = st.empty()  # filled at the end of the script, after any AI calls this run
    secrets_to_hide = [
        *(get_setting(p["key"], env)[0] for p in PROVIDERS.values()),
        get_setting("APP_PASSWORD", env)[0],
        api_key,
    ]

    env_model = get_setting(PROVIDERS[ai_provider]["model"], env)[0] or PROVIDERS[ai_provider]["default_model"]
    if ai_provider == "anthropic":
        model_ids = list(agent.MODELS) if env_model in agent.MODELS else [env_model, *agent.MODELS]
        ai_model = st.selectbox(
            "Model", model_ids, index=model_ids.index(env_model),
            format_func=lambda m: agent.MODELS.get(m, f"{m} (custom, from ANTHROPIC_MODEL)"),
            help="Sonnet 5.5 is the lowest-cost option. The Opus models give higher-quality answers at a higher price.",
        )
    else:
        ai_model = st.text_input("Model", value=env_model, help="Any GLM model name that supports function calling.")
    st.caption("The assistant only sees the comparison results table, not your PDF or Excel files.")

st.title("🏗️ Multi-Beam Reinforcement Checker")
st.caption("Compare required steel from a **Prokon** continuous-beam report against the provided steel in an **Excel beam schedule**.")

# ---------------------------------------------------------------- 1. Inputs
with st.container(border=True):
    st.subheader("1. Input files, sheet & format")
    col_x, col_p = st.columns(2)

    with col_x:
        excel_up = st.file_uploader("Excel beam schedule (.xlsx)", type=["xlsx", "xls"])
        sheet_name = None
        if excel_up:
            try:
                sheets = pd.ExcelFile(io.BytesIO(excel_up.getvalue())).sheet_names
                sheet_name = st.selectbox("Excel sheet", sheets, index=pick_default_sheet(sheets))
            except Exception as e:
                st.error(f"Cannot read sheet list: {e}")

    with col_p:
        pdf_up = st.file_uploader("Prokon report (.pdf)", type=["pdf"])

    fmt = st.radio(
        "Excel format",
        list(EXCEL_FORMATS),
        index=1,
        format_func=lambda k: EXCEL_FORMATS[k]["label"],
        horizontal=True,
    )

    run = st.button("▶ Run comparison (all beams)", type="primary", disabled=not (excel_up and pdf_up))

if run:
    bar = st.progress(0.0, text="Starting...")
    try:
        result = run_comparison(
            io.BytesIO(excel_up.getvalue()),
            io.BytesIO(pdf_up.getvalue()),
            sheet_name=sheet_name,
            fmt=fmt,
            progress=lambda frac, text: bar.progress(min(frac, 1.0), text=text),
        )
        st.session_state["result"] = result
        st.session_state["sheet"] = sheet_name
        st.session_state["chat_api"] = []      # new results -> fresh AI conversation
        st.session_state["chat_display"] = []
    except Exception as e:
        st.session_state.pop("result", None)
        st.error(f"Execution error: {e}")
    finally:
        bar.empty()

result = st.session_state.get("result")

# ---------------------------------------------------------------- 2. Results
if result is not None:
    df_all = result.to_dataframe()
    n_fail = int((df_all["Overall Status"] == "FAIL").sum())

    if result.matched_count == 0:
        st.warning(f"None of the beams in the PDF matched sheet '{st.session_state.get('sheet')}'. Check the Excel format option.")
    else:
        st.success(f"Checked {result.matched_count} beam span(s).")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spans checked", result.matched_count)
    m2.metric("Position rows", len(df_all))
    m3.metric("FAIL rows", n_fail)
    m4.metric("Unmatched beams", len(result.pdf_only) + len(result.excel_only))

    tab_results, tab_ai = st.tabs(["📋 Results", "🤖 AI Assistant"])

    with tab_results, st.container(border=True):
        st.subheader("2. Comparison results (3 position rows per span)")

        f1, f2 = st.columns([3, 1])
        search = f1.text_input("Search beam mark", placeholder="e.g. B101")
        status = f2.selectbox("Status", ["All", "FAIL Only", "OK Only"])

        df_view = df_all
        if search.strip():
            df_view = df_view[df_view["Beam Mark"].str.lower().str.contains(search.strip().lower(), regex=False)]
        if status == "FAIL Only":
            df_view = df_view[df_view["Overall Status"] == "FAIL"]
        elif status == "OK Only":
            df_view = df_view[df_view["Overall Status"] == "OK"]
        df_view = df_view.reset_index(drop=True)

        st.dataframe(style_results(df_view), hide_index=True, width="stretch", height=560)

        d1, d2, _ = st.columns([1, 1, 4])
        d1.download_button(
            "⬇ CSV report",
            df_view.to_csv(index=False).encode("utf-8-sig"),
            file_name="beam_rebar_check.csv",
            mime="text/csv",
            disabled=df_view.empty,
        )
        d2.download_button(
            "⬇ Excel report",
            to_excel_bytes(df_view),
            file_name="beam_rebar_check.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            disabled=df_view.empty,
        )

    with tab_results, st.expander(f"Unmatched beams (sheet: {st.session_state.get('sheet')})", expanded=bool(result.pdf_only or result.excel_only)):
        u1, u2 = st.columns(2)
        u1.markdown(f"**In PDF, missing in Excel ({len(result.pdf_only)})**")
        u1.dataframe(pd.DataFrame({"Beam": result.pdf_only}), hide_index=True, width="stretch")
        u2.markdown(f"**In Excel, missing in PDF ({len(result.excel_only)})**")
        u2.dataframe(pd.DataFrame({"Beam": result.excel_only}), hide_index=True, width="stretch")

    with tab_ai:
        render_assistant(result, ai_provider, api_key, ai_model, key_mode, limits, secrets_to_hide)
else:
    st.info("Upload both files and click **Run comparison** to start.")

if key_mode == "shared":
    left = limits["session"] - access.session_calls_used(st.session_state, "chat")
    calls_left_slot.caption(f"AI calls left this session: {max(0, left)} of {limits['session']}")
