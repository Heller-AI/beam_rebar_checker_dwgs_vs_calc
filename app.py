"""Streamlit front-end for the Prokon vs Beam Schedule reinforcement checker.

Run locally:  streamlit run app.py
"""

import hashlib
import io
import math
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st

from beam_checker import EXCEL_FORMATS, run_comparison, run_comparison_records
from beam_checker import access, agent, drawing_reader
from beam_checker.prompts import FLAG_DESCRIPTIONS

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


EXCEL_MODE = "Excel schedule (no AI)"
DRAWING_MODE = "Drawing (PDF or image)"


def fingerprint_df(df):
    return hashlib.sha256(df.to_csv(index=False).encode("utf-8")).hexdigest()


@st.cache_data(show_spinner="Reading the drawing...", max_entries=4)
def load_drawing_pages(files, max_pages):
    return drawing_reader.load_pages(list(files), max_pages)


@st.cache_data(show_spinner="Reading the Prokon report...", max_entries=4)
def prokon_beam_marks(pdf_bytes):
    from beam_checker import extract_all_beams_from_pdf
    return sorted(extract_all_beams_from_pdf(io.BytesIO(pdf_bytes)))


TEXT_METHOD = "PDF text layer (free, exact)"
VISION_METHOD = "AI vision (paid)"
HIGHLIGHT = "background-color: #FFE8A3; color: #5C4400"


def render_drawing_mode(prokon_up, provider, api_key, key_mode, model, limits, extra_rules, secrets):
    """Upload a schedule drawing, read it (text layer first, AI vision if needed), review it, compare."""
    st.caption("Upload the beam schedule drawing as a PDF, or as PNG/JPG images of the pages. "
               "Drawings may be older than the calculation: this mode is for surfacing discrepancies "
               "for a person to double-check.")
    uploads = st.file_uploader("Beam schedule drawing (.pdf, .png, .jpg)", type=["pdf", "png", "jpg", "jpeg"],
                               accept_multiple_files=True, key="drawing_files")
    if not uploads:
        return

    files = tuple((u.name, u.getvalue()) for u in uploads)
    try:
        n_pages = drawing_reader.count_pages(files)
    except Exception as e:
        st.error(f"Cannot read the drawing: {e}")
        return
    if n_pages > limits["max_pages"]:
        st.error(f"The upload has {n_pages} pages; the limit is {limits['max_pages']}. Upload only the schedule sheets.")
        return
    pages = load_drawing_pages(files, limits["max_pages"])
    extractions = st.session_state.setdefault("drawing_extractions", {})

    # 1) free check first: is the schedule in the PDF text layer?
    text_rows = drawing_reader.text_layer_summary(pages)
    fp_text = drawing_reader.files_fingerprint(files, "text-layer")
    if text_rows:
        pages_txt = ", ".join(str(p) for p in text_rows)
        st.success(f"📄 Schedule table found in the PDF text layer: **{sum(text_rows.values())} row(s)** on page(s) "
                   f"{pages_txt}. Read directly from the drawing's text: exact, no AI, **cost $0**, nothing is sent.")
        if fp_text not in extractions:
            extractions[fp_text] = drawing_reader.read_text_layer(pages)
        method = st.radio("Reading method", [TEXT_METHOD, VISION_METHOD], horizontal=True, key="reading_method",
                          help="AI vision is only needed for scanned drawings or images without a text layer.")
    else:
        st.info("No schedule table found in the PDF text layer (scanned drawing or image), so it has to be read "
                "with AI vision.")
        method = VISION_METHOD

    # 2) AI vision, with cost estimate and consent
    if method == VISION_METHOD:
        if provider != "anthropic":
            st.warning("AI vision uses Anthropic (Claude). Switch the AI provider in the sidebar.")
            return
        if not api_key:
            st.info("Sign in with the access code, or enter your own Anthropic API key in the sidebar, to use AI vision.")
            return
        active_fp = drawing_reader.files_fingerprint(files, model, extra_rules)
        est = drawing_reader.estimate_cost(pages, model)
        cost = (f"about ${est['low']:.2f}–{est['high']:.2f}" if est["low"] is not None
                else "unknown (no price on file for this model)")
        st.markdown(f"**{est['pages']} page(s)**, sent as {est['images']} image(s) (overview plus close-up tiles). "
                    f"Estimated cost with `{model}`: **{cost}**.")
        consent = st.checkbox("I confirm I am allowed to send this drawing to Anthropic", key="drawing_consent")
        cached = active_fp in extractions
        if cached:
            st.info("Showing the AI reading already made for these files and this model (no new AI calls).")
        label = "🔁 Read again with AI (new AI calls)" if cached else "🤖 Read the drawing with AI"
        if st.button(label, disabled=not consent, type="secondary" if cached else "primary"):
            def on_request():
                if key_mode == "shared":
                    access.consume_call(st.session_state, "drawing", limits["drawing_session"], daily_counter(),
                                        limits["daily"])

            bar = st.progress(0.0, text="Starting...")
            try:
                extractions[active_fp] = drawing_reader.extract_drawing(
                    pages, drawing_reader.stream_send(agent.make_client(api_key)), model, extra_rules,
                    on_request=on_request, on_progress=lambda f, t: bar.progress(min(f, 1.0), text=t),
                    max_tokens=limits["drawing_max_tokens"],
                )
            except access.BudgetExceeded as e:
                st.error(f"🛑 {e}")
            except anthropic.APIError as e:
                st.error(access.redact(f"❌ {agent.describe_anthropic_error(e, model)}", secrets))
            finally:
                bar.empty()
    else:
        active_fp = fp_text

    extraction = extractions.get(active_fp)
    if extraction is not None:
        render_review(extraction, active_fp, files, pages, prokon_up, limits)


def render_review(extraction, fp, files, pages, prokon_up, limits):
    """Editable review table: every row must be ticked; coverage on top; crop preview."""
    st.divider()
    st.subheader("2. Review the drawing schedule")
    if extraction.method == drawing_reader.READ_TEXT:
        st.info("Read from the PDF text layer: the values are the drawing's own text, exactly as written. "
                "Still tick each row after checking it; the drawing may be older than the calculation.")
    else:
        st.warning("These values were read by AI from the drawing images. Check every row against the drawing, "
                   "especially the highlighted ones, and correct the table. A misread value can hide a discrepancy.")
    for page_no, err in extraction.page_errors.items():
        st.error(f"Page {page_no}: {err}")
    for page_no, note in extraction.page_notes.items():
        st.info(f"Page {page_no}: {note}")

    coverage_slot = st.container()

    with st.expander("What the flags and highlights mean"):
        st.markdown("Highlighted rows (yellow status cells) have medium/low confidence or a flag and need extra care.")
        st.markdown("\n".join(f"- `{k}`: {v}" for k, v in FLAG_DESCRIPTIONS.items()))
        st.markdown("Bottom bars **B3** are shown for review but, as in Excel mode, the checker does not use them. "
                    "For a blank support end the existing checker rules still copy the other end's top bars.")

    def highlight(row):
        style = HIGHLIGHT if row["Review"] else ""
        return [style if c in drawing_reader.LOCKED_COLUMNS else "" for c in row.index]

    edited = st.data_editor(
        extraction.table.style.apply(highlight, axis=1), key=f"review_{fp}", num_rows="dynamic", hide_index=True,
        width="stretch", height=480,
        column_config={
            "Reviewed": st.column_config.CheckboxColumn("Reviewed ✓", help="Tick after checking this row against the drawing"),
            **{c: st.column_config.Column(disabled=True) for c in drawing_reader.LOCKED_COLUMNS},
            "Confidence": st.column_config.SelectboxColumn(options=["high", "medium", "low"], width="small"),
        },
    )
    n_done = int(edited["Reviewed"].fillna(False).astype(bool).sum())
    n_flag = int((edited["Review"].fillna("") != "").sum())
    st.caption(f"**{n_done} of {len(edited)} row(s) ticked as reviewed** · {n_flag} highlighted for extra care. "
               "Edit cells to correct them; add or delete rows (select a row, then press Delete).")

    # coverage, shown prominently above the table
    with coverage_slot:
        if prokon_up:
            cov = drawing_reader.coverage(edited, prokon_beam_marks(prokon_up.getvalue()))
            ok = not cov["missing"]
            box = st.success if ok else st.error
            box(f"### 🔎 Coverage: found **{len(cov['found'])} of {cov['total']}** Prokon beam marks on the drawing")
            if cov["missing"] or cov["drawing_only"]:
                c1, c2 = st.columns(2)
                c1.markdown(f"**In Prokon, not found on the drawing ({len(cov['missing'])})**")
                c1.write(", ".join(cov["missing"]) or "none")
                c2.markdown(f"**On the drawing, not in Prokon ({len(cov['drawing_only'])})**")
                c2.write(", ".join(cov["drawing_only"]) or "none")
        else:
            st.info("Upload the Prokon report to see the coverage (found X of Y Prokon beam marks on the drawing).")

    # crop preview while reviewing
    marks = [m for m in edited["Beam mark"].fillna("").astype(str) if m.strip()]
    if extraction.boxes and marks:
        pick = st.selectbox("Show the drawing crop for a row", ["(choose a beam mark)"] + marks, key=f"crop_pick_{fp}")
        if pick != "(choose a beam mark)":
            show_row_crop(edited, extraction.boxes, pages, pick)

    conflicts = drawing_reader.table_conflicts(edited)
    if conflicts:
        st.error("These beam marks appear more than once. Keep one row per span (delete or rename the others): "
                 + ", ".join(conflicts))
    ack = True
    if extraction.page_errors:
        ack = st.checkbox("Continue without the page(s) that could not be read")
    reviewed = drawing_reader.all_reviewed(edited)
    if not reviewed:
        st.info("Tick **Reviewed** on every row to enable the comparison.")
    if not prokon_up:
        st.info("Upload the Prokon report to run the comparison.")

    if st.button("✅ Confirm and run comparison", type="primary",
                 disabled=bool(conflicts) or not ack or not prokon_up or not reviewed):
        bar = st.progress(0.0, text="Starting...")
        try:
            result = run_comparison_records(
                drawing_reader.table_to_records(edited), io.BytesIO(prokon_up.getvalue()),
                progress=lambda f, t: bar.progress(min(f, 1.0), text=t), remarks=drawing_reader.DRAWING_REMARKS,
            )
            st.session_state.update(
                result=result, result_source=DRAWING_MODE, sheet="drawing", result_table=fingerprint_df(edited),
                chat_api=[], chat_display=[],
                drawing_result_ctx={"table": edited.copy(), "boxes": extraction.boxes, "files": files,
                                    "max_pages": limits["max_pages"]},
            )
        except Exception as e:
            st.session_state.pop("result", None)
            st.error(f"Execution error: {e}")
        finally:
            bar.empty()
    st.session_state["current_table"] = fingerprint_df(edited)


def show_row_crop(table, boxes, pages, mark, caption_extra=""):
    """Show the drawing region of the table row with this beam mark (if its position is known)."""
    rows = table[table["Beam mark"].fillna("").astype(str).str.strip() == mark.strip()]
    if rows.empty:
        st.caption(f"{mark}: not in the reviewed table.")
        return
    row = rows.iloc[0]
    box = boxes.get(str(row["Row ID"]))
    if not box:
        st.caption(f"{mark}: no position on the drawing is known for this row (added by hand, or not given by the AI).")
        return
    page = next((p for p in pages if p.number == box["page"]), None)
    if page is None:
        return
    st.image(drawing_reader.crop_row(page, box["row"], box["header"]),
             caption=f"Page {box['page']} · {mark} · read from {row['Read from']}{caption_extra}", width="stretch")


FINDING_COLUMNS = ["Position / Location", "Req. As (mm²)", "Provided Bars", "Prov. As (mm²)", "Flex Status",
                   "Req. Asv/sv", "Provided Stirrup", "Prov. Asv/sv", "Shear Status"]


def render_findings(df_all, result, ctx):
    """Every FAIL span and every unmatched beam, each next to the cropped drawing region."""
    if not ctx:
        return
    pages = load_drawing_pages(ctx["files"], ctx["max_pages"])
    fails = df_all[df_all["Overall Status"] == "FAIL"]
    st.markdown(f"**{fails['Beam Mark'].nunique()} span(s) with a FAIL**, "
                f"**{len(result.excel_only)} drawing beam(s) not in Prokon**, "
                f"**{len(result.pdf_only)} Prokon beam(s) not on the drawing**. "
                "Compare each one with the drawing crop; the drawing may be pre-update.")

    for mark, rows in fails.groupby("Beam Mark", sort=False):
        with st.container(border=True):
            left, right = st.columns([3, 2])
            with left:
                show_row_crop(ctx["table"], ctx["boxes"], pages, str(mark))
            with right:
                st.markdown(f"#### ❌ {mark}")
                st.dataframe(rows[FINDING_COLUMNS], hide_index=True, width="stretch")

    if result.excel_only:
        st.markdown("#### On the drawing, not in the Prokon report")
        for base in result.excel_only:
            marks = [m for m in ctx["table"]["Beam mark"].fillna("").astype(str)
                     if m.strip() and drawing_reader.normalize_str(drawing_reader.clean_suffix(m)[0])
                     == drawing_reader.normalize_str(base)]
            for m in marks:
                with st.container(border=True):
                    show_row_crop(ctx["table"], ctx["boxes"], pages, m, " · not in the Prokon report")
    if result.pdf_only:
        st.markdown("#### In the Prokon report, not found on the drawing")
        st.write(", ".join(result.pdf_only))
    if fails.empty and not result.excel_only and not result.pdf_only:
        st.success("No FAILs and no unmatched beams. Still spot-check a few beams against the drawing.")


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


PLACEHOLDERS = ("your-key-here", "choose-a-long-random-code", "conventions go here")


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
        "drawing_session": int_setting("MAX_DRAWING_CALLS_PER_SESSION", env, 40),
        "drawing_max_tokens": int_setting("DRAWING_MAX_OUTPUT_TOKENS", env, drawing_reader.DEFAULT_MAX_OUTPUT_TOKENS),
        "max_pages": int_setting("MAX_DRAWING_PAGES", env, drawing_reader.DEFAULT_MAX_PAGES),
    }
    extra_rules = get_setting("EXTRA_READING_RULES", env)[0]  # private; never displayed
    calls_left_slot = st.empty()  # filled at the end of the script, after any AI calls this run
    secrets_to_hide = [
        *(get_setting(p["key"], env)[0] for p in PROVIDERS.values()),
        get_setting("APP_PASSWORD", env)[0],
        api_key,
        extra_rules,
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
    if st.session_state.get("schedule_source") == DRAWING_MODE:
        st.caption("Drawing mode reads the PDF text layer on the server when it can (nothing is sent to AI). "
                   "Only the AI vision option sends the drawing pages to Anthropic. The AI assistant only sees "
                   "the comparison results table. The app stores nothing.")
    else:
        st.caption("The assistant only sees the comparison results table, not your PDF or Excel files.")

st.title("🏗️ Multi-Beam Reinforcement Checker")
st.caption("Compare required steel from a **Prokon** continuous-beam report against the provided steel in a "
           "**beam schedule**: an Excel file, or a schedule drawing.")

# ---------------------------------------------------------------- 1. Inputs
with st.container(border=True):
    st.subheader("1. Input files")
    source = st.radio("Schedule source", [EXCEL_MODE, DRAWING_MODE], horizontal=True, key="schedule_source",
                      help="Excel mode needs no API key. Drawing mode reads the schedule from the PDF text layer when it can (free), otherwise with Claude vision.")

    if source == EXCEL_MODE:
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
    else:
        run = False
        prokon_up = st.file_uploader("Prokon report (.pdf)", type=["pdf"], key="prokon_drawing_mode")
        render_drawing_mode(prokon_up, ai_provider, api_key, key_mode, ai_model, limits, extra_rules, secrets_to_hide)

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
        st.session_state["result_source"] = EXCEL_MODE
        st.session_state["sheet"] = sheet_name
        st.session_state["chat_api"] = []      # new results -> fresh AI conversation
        st.session_state["chat_display"] = []
    except Exception as e:
        st.session_state.pop("result", None)
        st.error(f"Execution error: {e}")
    finally:
        bar.empty()

result = st.session_state.get("result")
from_drawing = st.session_state.get("result_source") == DRAWING_MODE
if result is not None and from_drawing != (source == DRAWING_MODE):
    result = None  # results belong to the other schedule source
if result is not None and from_drawing and st.session_state.get("current_table") != st.session_state.get("result_table"):
    st.warning("The reviewed table changed after the comparison ran. Click **Confirm and run comparison** again.")
    result = None
schedule_word = "drawing" if from_drawing else "Excel"

# ---------------------------------------------------------------- 2. Results
if result is not None:
    df_all = result.to_dataframe()
    n_fail = int((df_all["Overall Status"] == "FAIL").sum())

    if from_drawing:
        ctx = st.session_state.get("drawing_result_ctx", {})
        read_from = {str(m).strip(): r for m, r in zip(ctx["table"]["Beam mark"], ctx["table"]["Read from"])}
        df_all.insert(0, "Read from", [read_from.get(str(m).strip(), "") for m in df_all["Beam Mark"]])
        df_all.insert(0, "Schedule source", drawing_reader.SOURCE_LABEL)
        st.header(f"📐 {drawing_reader.SOURCE_LABEL}")
        n_found, n_total = len(result.pdf_matched_bases), len(result.pdf_beam_names)
        (st.success if n_found == n_total else st.error)(
            f"### 🔎 Coverage: found **{n_found} of {n_total}** Prokon beam marks on the drawing")
        st.warning("The drawing may be older than the calculation. Every FAIL and every unmatched beam is a "
                   "**discrepancy for a person to double-check** against the drawing crop in the Findings tab.")
    if result.matched_count == 0:
        where = "the reviewed drawing table" if from_drawing else f"sheet '{st.session_state.get('sheet')}'"
        st.warning(f"None of the beams in the PDF matched {where}. Check the beam marks"
                   + ("." if from_drawing else " and the Excel format option."))
    else:
        st.success(f"Checked {result.matched_count} beam span(s).")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spans checked", result.matched_count)
    m2.metric("Position rows", len(df_all))
    m3.metric("FAIL rows", n_fail)
    m4.metric("Unmatched beams", len(result.pdf_only) + len(result.excel_only))

    if from_drawing:
        tab_results, tab_findings, tab_ai = st.tabs(["📋 Results", "🔎 Findings to check", "🤖 AI Assistant"])
    else:
        tab_results, tab_ai = st.tabs(["📋 Results", "🤖 AI Assistant"])
        tab_findings = None

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

    unmatched_title = "Unmatched beams (" + ("drawing" if from_drawing else f"sheet: {st.session_state.get('sheet')}") + ")"
    with tab_results, st.expander(unmatched_title, expanded=bool(result.pdf_only or result.excel_only)):
        u1, u2 = st.columns(2)
        u1.markdown(f"**In PDF, missing in {schedule_word} ({len(result.pdf_only)})**")
        u1.dataframe(pd.DataFrame({"Beam": result.pdf_only}), hide_index=True, width="stretch")
        u2.markdown(f"**In {schedule_word}, missing in PDF ({len(result.excel_only)})**")
        u2.dataframe(pd.DataFrame({"Beam": result.excel_only}), hide_index=True, width="stretch")

    if tab_findings is not None:
        with tab_findings:
            render_findings(df_all, result, st.session_state.get("drawing_result_ctx", {}))

    with tab_ai:
        render_assistant(result, ai_provider, api_key, ai_model, key_mode, limits, secrets_to_hide)
elif source == EXCEL_MODE:
    st.info("Upload both files and click **Run comparison** to start.")

if key_mode == "shared":
    left = limits["session"] - access.session_calls_used(st.session_state, "chat")
    left_d = limits["drawing_session"] - access.session_calls_used(st.session_state, "drawing")
    calls_left_slot.caption(f"AI calls left this session: assistant {max(0, left)} of {limits['session']}, "
                            f"drawing reading {max(0, left_d)} of {limits['drawing_session']}")
