"""Streamlit front-end of the Beam Schedule Checker vs Calculation Report.

A beam schedule (Excel, DXF or drawing) is checked against the required steel in a Prokon calculation report.

Run locally:  streamlit run app.py
"""

import hashlib
import io
import math
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st

from beam_checker import EXCEL_FORMATS, RESULT_COLUMNS, run_comparison, run_comparison_records
from beam_checker.checker import NOT_CHECKED, NOTE_COLUMN, check_span, match_span
from beam_checker import access, agent, cad_reader, drawing_reader, plausibility, text_layer
from beam_checker.prompts import FLAG_DESCRIPTIONS

APP_NAME = "Beam Schedule Checker vs Calculation Report"
st.set_page_config(page_title=APP_NAME, page_icon="🏗️", layout="wide")

FAIL_STYLE = "background-color: #FFC7CE; color: #9C0006"
WARN_STYLE = "background-color: #FFE8A3; color: #5C4400"
ODD_STYLE = "background-color: rgba(128, 128, 128, 0.08)"


def pick_default_sheet(sheets):
    for s in sheets:
        if "BEAM" in s.upper() or "SCHEDULE" in s.upper():
            return sheets.index(s)
    return 0


def style_results(df):
    """Red rows for FAIL, yellow for NOT CHECKED and for check notes, alternating shading per beam mark otherwise."""
    beam_group = (df["Beam Mark"] != df["Beam Mark"].shift()).cumsum()

    def row_style(row):
        if row["Overall Status"] == "FAIL":
            style = FAIL_STYLE
        elif row["Overall Status"] == NOT_CHECKED:
            style = WARN_STYLE
        elif beam_group[row.name] % 2:
            style = ODD_STYLE
        else:
            style = ""
        styles = [style] * len(row)
        if row.get(NOTE_COLUMN):                         # never let a span with a check note look like a plain OK
            for i, c in enumerate(row.index):
                if c in (NOTE_COLUMN, "Overall Status"):
                    styles[i] = WARN_STYLE
        return styles

    return df.style.apply(row_style, axis=1)


def to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Rebar & Stirrup Detail Check", index=False)
    return buf.getvalue()


# Example question buttons. Fix suggestions stay available by typing the question.
EXCEL_QUESTIONS = [
    "Summarise the failures",
    "Why does each failing beam fail, and by how much?",
    "Which beams are missing from the schedule or the Prokon report?",
]
DRAWING_QUESTIONS = [                      # drawing mode leads with discrepancies
    "List each FAIL with its shortfall",
    "Which beams are missing from the drawing or the Prokon report?",
    "Which rows have a possible typo?",
    "Which rows were uncertain, or only on the drawing or only in Prokon?",
]



PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "key": "ANTHROPIC_API_KEY", "model": "ANTHROPIC_MODEL",
                  "default_model": agent.DEFAULT_MODEL, "console": "console.anthropic.com"},
    "zhipu": {"label": "Zhipu (GLM)", "key": "ZHIPU_API_KEY", "model": "ZHIPU_MODEL",
              "default_model": agent.DEFAULT_ZHIPU_MODEL, "console": "open.bigmodel.cn"},
}


def render_sign_in_notice(provider):
    st.warning("🔑 **Sign in with the access code in the sidebar to use the assistant.** "
               f"(Or open \"Use my own API key instead\" there and enter a {PROVIDERS[provider]['label']} key.)")


def clear_chat():
    st.session_state["chat_display"], st.session_state["chat_api"] = [], []


TABLE_LABELS = {
    "beam": "Beam", "position": "Position", "provided_as": "Provided now", "required": "Required",
    "provided": "Provided", "shortfall": "Shortfall", "provided_over_required_pct": "Provided / required (%)",
    "suggested_change": "Suggested change", "fit": "Fit", "suggested_provides": "Suggested provides",
    "suggested_over_required_pct": "Suggested / required (%)",
}


def render_failure_tables(payload):
    """A get_failures result, drawn by the app itself: counts as a fact line, then one table per check
    so every cell has one unit (mm² for flexure, Asv/sv for shear)."""
    st.markdown(f"**{payload['counts']['fact']}**")
    rows = payload.get("rows", [])
    for check, unit, decimals in (("Flexure", "mm²", 1), ("Shear", "Asv/sv, mm²/mm", 3)):
        part = [r for r in rows if r["check"] == check]
        if not part:
            continue
        df = pd.DataFrame(part)
        cols = [c for c in TABLE_LABELS if c in df.columns]
        df = df[cols]
        for c in ("required", "provided", "shortfall", "suggested_provides"):
            if c in df.columns:
                df[c] = [f"{v:.{decimals}f}" if isinstance(v, (int, float)) else "-" for v in df[c]]
        # the unit is given once per table (each table has a single unit) so the columns fit the page
        st.markdown(f"*{check}: Required, Provided, Shortfall and Suggested provides in {unit}*")
        st.dataframe(df.rename(columns={c: TABLE_LABELS[c] for c in cols}), hide_index=True, width="stretch")
    if payload.get("note"):
        st.caption(payload["note"])


def render_answer(entry):
    """One assistant answer: tables computed by the app, then the model's text, then any warnings."""
    if entry.get("tools"):
        with st.expander(f"🔧 {len(entry['tools'])} tool call(s), {entry.get('requests', 0)} model call(s)"):
            for t in entry["tools"]:
                st.code(t, language="text")
    for payload in entry.get("tables", []):
        render_failure_tables(payload)
    if entry.get("text"):
        st.markdown(entry["text"])
    if entry.get("stop_message"):
        st.warning(entry["stop_message"])
    if entry.get("untraceable"):
        st.warning("⚠ These numbers in the answer could not be traced to a tool result in this conversation; "
                   "check them before relying on them: " + ", ".join(entry["untraceable"]))


def render_assistant(result, provider, api_key, model, key_mode, limits, secrets, schedule=None, widths=None):
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
        render_sign_in_notice(provider)
        return

    for msg in history:
        with st.chat_message(msg["role"]):
            if msg["role"] == "assistant":
                render_answer(msg)
            else:
                st.markdown(msg["text"])

    if schedule:
        st.info("The drawing may be pre-update: a FAIL can mean the drawing and the calculation differ, "
                "not that the beam is under-reinforced.")
    questions = DRAWING_QUESTIONS if schedule else EXCEL_QUESTIONS
    cols = st.columns(len(questions))
    clicked = next((q for c, q in zip(cols, questions) if c.button(q, width="stretch")), None)
    question = st.chat_input("Ask about the results...") or clicked
    if history:
        st.button("🗑 Clear conversation", on_click=clear_chat)

    if not question:
        return

    history.append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        tool_log = []
        entry = {"role": "assistant", "text": "", "tables": []}
        with st.status("Looking up the results...", expanded=False) as status:
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
            kwargs = dict(model=model, on_tool=on_tool, on_request=on_request, max_tokens=limits["max_tokens"],
                          schedule=schedule, widths=widths)
            try:
                if provider == "zhipu":
                    out = agent.ask_zhipu(api_key, api_messages, result, **kwargs)
                else:
                    out = agent.ask(agent.make_client(api_key), api_messages, result, **kwargs)
                entry.update(text=out.text, tables=out.tables, requests=out.requests)
                if out.stop != "end_turn":
                    detail = f" (stop reason: {out.stop}"
                    detail += f", MAX_OUTPUT_TOKENS = {limits['max_tokens']})" if out.stop == "max_tokens" else ")"
                    shown = " The tables computed so far are shown above." if out.tables else ""
                    entry["stop_message"] = out.stop_message + detail + shown
                    if out.stop == "call_cap":
                        entry["text"] = ""                 # the cap message is already in stop_message
                        entry["stop_message"] = f"🛑 {out.text}{shown}"
                entry["untraceable"] = agent.untraceable_numbers(entry["text"], api_messages, question)
                status.update(label=f"Done ({out.requests} model call(s), {len(tool_log)} tool call(s))",
                              state="complete" if out.stop == "end_turn" else "error")
            except agent.ProviderError as e:
                entry["text"] = f"❌ {e}"
                status.update(label="Error", state="error")
            except anthropic.APIError as e:
                entry["text"] = f"❌ {agent.describe_anthropic_error(e, model)}"
                status.update(label="Error", state="error")
        entry["text"] = access.redact(entry["text"], secrets)
        entry["tools"] = [access.redact(t, secrets) for t in tool_log]
        render_answer({**entry, "tools": []})          # the live tool log is already in the status box above

    history.append(entry)


EXCEL_MODE = "Excel schedule"
CAD_MODE = "CAD file (DXF)"
DRAWING_MODE = "Drawing PDF or image"
DRAWING_MODES = (CAD_MODE, DRAWING_MODE)   # schedule read from a drawing: review step and findings tab
SOURCE_CAPTIONS = {
    EXCEL_MODE: "No AI used, free",
    CAD_MODE: "No AI used, free",
    DRAWING_MODE: "Free from the PDF text layer; AI vision (paid) only for scans and images",
}
CAD_EXTENSIONS = (".dxf", ".dwg")
IMAGE_PDF_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg")
WRONG_FOR_CAD = ("This is a PDF or image, not a DXF. Choose **Schedule source → Drawing PDF or image** to read it "
                 "(PDF text layer free; AI vision only for scans and images).")
WRONG_FOR_DRAWING = ("DXF and DWG files are read in **Schedule source → CAD file (DXF)** (no AI, free). Choose that "
                     "option and upload the DXF there.")
RUN_LABEL = "▶ Run comparison (all beams)"


def fingerprint_df(df):
    return hashlib.sha256(df.to_csv(index=False).encode("utf-8")).hexdigest()


@st.cache_data(show_spinner="Reading the drawing...", max_entries=4)
def load_drawing_pages(files, max_pages):
    return drawing_reader.load_pages(list(files), max_pages)


@st.cache_data(show_spinner="Reading the DXF (no AI)...", max_entries=4)
def read_cad_file(data, max_mb, max_entities, timeout_s):
    return cad_reader.read_dxf(data, cad_reader.CadLimits(max_mb, max_entities, timeout_s))


@st.cache_data(show_spinner="Reading the Prokon report...", max_entries=4)
def prokon_beams(pdf_bytes):
    from beam_checker import extract_all_beams_from_pdf
    return extract_all_beams_from_pdf(io.BytesIO(pdf_bytes))


def prokon_beam_marks(pdf_bytes):
    return sorted(prokon_beams(pdf_bytes))


TEXT_METHOD = "PDF text layer (free, exact)"
VISION_METHOD = "AI vision (paid)"
HIGHLIGHT = "background-color: #FFE8A3; color: #5C4400"


def render_drawing_input(provider, api_key, key_mode, model, limits, extra_rules, secrets):
    """Left column of the input box in drawing mode: upload and read the drawing.

    Returns {"extraction", "fp", "files", "pages"} once a reading exists, else None.
    """
    # CAD types are accepted by the widget only to point them to the CAD option with a clear message
    uploads = st.file_uploader("Beam schedule drawing (.pdf, .png, .jpg)",
                               type=["pdf", "png", "jpg", "jpeg", "dxf", "dwg"], accept_multiple_files=True,
                               key="drawing_files",
                               help="The PDF text layer is read first (free). Scans and images are read with AI vision "
                                    "(paid, after your consent). A DXF goes in Schedule source → CAD file (DXF).")
    if not uploads:
        return None
    files = tuple((u.name, u.getvalue()) for u in uploads)
    if any(name.lower().endswith(CAD_EXTENSIONS) for name, _ in files):
        st.error(WRONG_FOR_DRAWING)
        return None
    try:
        n_pages = drawing_reader.count_pages(files)
    except Exception as e:
        st.error(f"Cannot read the drawing: {e}")
        return None
    if n_pages > limits["max_pages"]:
        st.error(f"The upload has {n_pages} pages; the limit is {limits['max_pages']}. Upload only the schedule sheets.")
        return None
    pages = load_drawing_pages(files, limits["max_pages"])
    extractions = st.session_state.setdefault("drawing_extractions", {})

    # free check first: is the schedule in the PDF text layer?
    text_rows = drawing_reader.text_layer_summary(pages)
    fp_text = drawing_reader.files_fingerprint(files, "text-layer")
    if text_rows:
        st.success(f"📄 **{sum(text_rows.values())} rows found** in the PDF text layer "
                   f"(page {', '.join(str(p) for p in text_rows)}), **cost $0**. Read exactly as written; "
                   "no AI involved, nothing sent.")
        if fp_text not in extractions:
            extractions[fp_text] = drawing_reader.read_text_layer(pages)
        method = st.radio("Reading method", [TEXT_METHOD, VISION_METHOD], horizontal=True, key="reading_method",
                          help="AI vision is only needed for scanned drawings or images without a text layer.")
    else:
        st.info("No schedule table in the PDF text layer (scanned drawing or image): it will be read with AI vision.")
        method = VISION_METHOD

    if method == TEXT_METHOD:
        fp = fp_text
    else:
        if provider != "anthropic":
            st.warning("AI vision uses Anthropic (Claude). Switch the AI provider in the sidebar.")
            return None
        if not api_key:
            st.warning("🔑 Sign in with the access code in the sidebar (or use your own Anthropic key) for AI vision.")
            return None
        fp = drawing_reader.files_fingerprint(files, model, extra_rules)
        est = drawing_reader.estimate_cost(pages, model)
        cost = (f"about ${est['low']:.2f}–{est['high']:.2f}" if est["low"] is not None
                else "unknown (no price on file for this model)")
        st.markdown(f"**{est['pages']} page(s)**, sent as {est['images']} image(s). Estimated cost with "
                    f"`{model}`: **{cost}**.")
        consent = st.checkbox("I confirm I am allowed to send this drawing to Anthropic", key="drawing_consent")
        # Readings are cached per browser session by file contents + model + rules: uploading the same drawing
        # again in this session makes no new AI calls. A new session (or a page refresh) starts without them.
        cached = extractions.get(fp)
        failed = sorted(cached.page_errors) if cached else []
        if cached:
            st.caption("Showing the AI reading already made for these files and this model (no new AI calls).")
        if failed:
            est_f = drawing_reader.estimate_cost([p for p in pages if p.number in failed], model)
            cost_f = f"about ${est_f['low']:.2f}–{est_f['high']:.2f}" if est_f["low"] is not None else "cost unknown"
            label = (f"🔁 Read the {len(failed)} page(s) that were not read (new AI calls, {cost_f}); "
                     "pages already read are kept")
        elif cached:
            label = f"🔁 Read all pages again with AI (new AI calls, {cost})"
        else:
            label = "🤖 Read the drawing with AI"
        if st.button(label, disabled=not consent):
            # The allowance is checked before each request and counted once a response has arrived, so a
            # request that fails with an error is not counted.
            def on_request():
                if key_mode == "shared":
                    access.check_call(st.session_state, "drawing", limits["drawing_session"], daily_counter(),
                                      limits["daily"])

            def on_response():
                if key_mode == "shared":
                    access.count_call(st.session_state, "drawing", daily_counter())

            bar = st.progress(0.0, text="Starting...")
            try:
                extractions[fp] = drawing_reader.extract_drawing(
                    pages, drawing_reader.stream_send(agent.make_client(api_key)), model, extra_rules,
                    on_request=on_request, on_progress=lambda f, t: bar.progress(min(f, 1.0), text=t),
                    max_tokens=limits["drawing_max_tokens"], on_response=on_response,
                    stop_on=(access.BudgetExceeded, anthropic.APIError), previous=cached if failed else None,
                )
            finally:
                bar.empty()
            stopped = extractions[fp].stopped
            if isinstance(stopped, access.BudgetExceeded):
                st.error(f"🛑 {stopped} Pages already read are kept.")
            elif stopped is not None:
                st.error(access.redact(f"❌ {agent.describe_anthropic_error(stopped, model)} Pages already read "
                                       "are kept; read the others again when the problem is solved.", secrets))

    extraction = extractions.get(fp)
    if extraction is None:
        return None
    return {"extraction": extraction, "fp": fp, "files": files, "pages": pages}


def render_cad_upload(limits):
    """CAD mode: upload one DXF and read it without AI. Returns the reading state like render_drawing_input."""
    # PDF and image types are accepted by the widget only to point them to the drawing option with a clear message
    upload = st.file_uploader("CAD schedule (.dxf)", type=["dxf", "dwg", "pdf", "png", "jpg", "jpeg"], key="cad_file",
                              help="Export only the schedule sheet as DXF. DWG is not read: save it as DXF first. "
                                   "No AI is used; free.")
    if upload is None:
        return None
    name = upload.name.lower()
    if name.endswith(IMAGE_PDF_EXTENSIONS):
        st.error(WRONG_FOR_CAD)
        return None
    if name.endswith(".dwg"):
        st.error(f"❌ {cad_reader.DWG_MESSAGE}")
        return None
    return render_cad_input(((upload.name, upload.getvalue()),), limits)


def render_cad_input(files, limits):
    """Drawing mode with a DXF: read its text (no AI) and let the user choose the schedule table(s)."""
    st.info("🔒 **CAD files:** upload the schedule sheet only (export it on its own as DXF). A whole-project DXF "
            "holds much more than the schedule; for confidential drawings run the app on your own PC (README: "
            "*Running locally for CAD files*). The app stores nothing.")
    data = files[0][1]
    try:
        reading = read_cad_file(data, limits["dxf_mb"], limits["dxf_entities"], limits["dxf_timeout"])
    except cad_reader.CadReadError as e:
        st.error(f"❌ {e}")
        return None
    for warning in reading.warnings:
        st.warning(warning)
    if not reading.tables:
        found = ", ".join(f"'{h}'" for h in reading.found_headers) or "none"
        st.error(f"No beam schedule table found in the DXF ({len(reading.layouts)} layout(s) checked). "
                 f"Header names found: {found}. Expected a 'Mark' column with the beam marks below it, and headers "
                 f"such as: {', '.join(text_layer.EXPECTED_HEADERS)}.")
        return None

    every = list(range(len(reading.tables)))
    if len(reading.tables) > 1:
        chosen = st.multiselect(
            "Schedule tables to read", every, default=every,
            format_func=lambda i: reading.tables[i].label(), key=f"cad_tables_{hashlib.sha256(data).hexdigest()}",
            help="Several schedule tables were found (e.g. a single-span and a continuous-span schedule). All are "
                 "selected; remove any that is not part of the schedule.")
        if not chosen:
            st.info("Choose at least one schedule table.")
            return None
    else:
        chosen = every
    chosen = sorted(chosen)
    n_rows = sum(len(reading.tables[i].table.records) for i in chosen)
    cells = f", {reading.table_cells:,} table cells" if reading.table_cells else ""
    st.success(f"📐 **{n_rows} rows found** in the DXF ({len(chosen)} of {len(reading.tables)} table(s){cells}), "
               "**cost $0**. Read exactly as typed; no AI involved, nothing sent.")
    fp = drawing_reader.files_fingerprint(files, "cad:" + ",".join(map(str, chosen)))
    extractions = st.session_state.setdefault("drawing_extractions", {})
    if fp not in extractions:
        extractions[fp] = drawing_reader.read_cad([reading.tables[i] for i in chosen])
    return {"extraction": extractions[fp], "fp": fp, "files": files, "pages": []}


def render_review(state, prokon_up, limits):
    """Step 2 in drawing mode: coverage, review table, per-beam check, Excel download, run."""
    extraction, fp, files, pages = state["extraction"], state["fp"], state["files"], state["pages"]
    st.subheader("2. Review drawing schedule")
    if extraction.method == drawing_reader.READ_CAD:
        st.caption("Read from the DXF's text: the values are the drawing's own text, copied exactly (Page = layout "
                   "number in the DXF). The drawing may be older than the calculation; the comparison will show "
                   "the differences.")
    elif extraction.method == drawing_reader.READ_TEXT:
        st.caption("Read from the PDF text layer: the values are the drawing's own text, copied exactly. "
                   "The drawing may be older than the calculation; the comparison will show the differences.")
    else:
        st.warning("These values were read by AI from the drawing images. Check every row against the drawing, "
                   "especially the highlighted ones, and correct the table.")
    for page_no, err in extraction.page_errors.items():
        st.error(f"Page {page_no}: {err}")
    where = "Layout" if extraction.method == drawing_reader.READ_CAD else "Page"
    for page_no, note in extraction.page_notes.items():
        st.info(f"{where} {page_no}: {note}")

    coverage_slot = st.container()
    legs_slot = st.container()         # assumed leg count, filled from the table as edited
    # Row highlights come from the table as last edited (Prokon concerns, duplicate marks); when an edit changes
    # them, the page reruns once so the highlight always matches the table on screen.
    marks_key = f"review_marks_{fp}"
    shown = st.session_state.get(marks_key, {"prokon": {}, "duplicates": []})

    def highlight(row):
        if drawing_reader.mark_key(row["Beam mark"]) in shown["duplicates"]:
            style = FAIL_STYLE
        elif row["Review"] or str(row["Row ID"]) in shown["prokon"]:
            style = HIGHLIGHT
        else:
            style = ""
        return [style if c in drawing_reader.LOCKED_COLUMNS else "" for c in row.index]

    # "Tick all unflagged rows" saves the edited table as the new starting table under a new editor key, so the
    # reviewer's edits are kept; a new reading of the drawing starts again from that reading.
    base_key, ver_key = f"review_base_{fp}", f"review_ver_{fp}"
    base_for, base = st.session_state.get(base_key, (None, None))
    if base_for != id(extraction):
        base = extraction.table
        if base_for is not None:
            st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
        st.session_state[base_key] = (id(extraction), base)
    edited = st.data_editor(
        base.style.apply(highlight, axis=1), key=f"review_{fp}_{st.session_state.get(ver_key, 0)}",
        num_rows="dynamic", hide_index=True,
        width="stretch", height=440,
        column_config={
            "Reviewed": st.column_config.CheckboxColumn(
                "Reviewed ✓", help="Required for rows read by AI vision; optional for PDF text-layer and CAD rows"),
            **{c: st.column_config.Column(disabled=True) for c in drawing_reader.LOCKED_COLUMNS},
            "Review": st.column_config.Column(disabled=True, width=140),
            "Confidence": st.column_config.SelectboxColumn(options=["high", "medium", "low"], width="small"),
        },
    )
    n_done, n_required = drawing_reader.tick_status(edited)
    beams = prokon_beams(prokon_up.getvalue()) if prokon_up else None
    concerns = drawing_reader.prokon_concerns(edited, beams) if beams else {}
    conflicts = drawing_reader.table_conflicts(edited)
    now = {"prokon": concerns, "duplicates": sorted({drawing_reader.mark_key(m) for m in conflicts})}
    if now != shown:
        st.session_state[marks_key] = now
        st.rerun()
    legs_note = drawing_reader.assumed_legs_note(edited)
    if legs_note:
        legs_slot.info(legs_note, icon="ℹ️")
    n_flag = int(((edited["Review"].fillna("") != "") | edited["Row ID"].astype(str).isin(concerns)).sum())
    if n_required:
        st.caption(f"**Why ticks:** AI vision can misread a value, so each AI-read row must be ticked after you check "
                   f"it against the drawing (**{n_done} of {n_required} ticked**). **Tick all unflagged rows** ticks "
                   "the rows without a concern; each highlighted row needs its own tick. Then confirm the whole "
                   "table once, below. Text-layer and CAD rows are the drawing's own text; ticking them is optional.")
    else:
        st.caption("**Why ticks:** they are an optional checklist here. These rows are the drawing's own text, copied "
                   "exactly; only rows read by AI vision must be ticked.")
    st.caption(f"{len(edited)} row(s) · {n_flag} highlighted (yellow) for extra care: low confidence, a bar or "
               "stirrup that does not parse, a possible typo, spans that do not continue, or a Prokon concern"
               + (" · duplicate marks in red" if conflicts else "") + ". Other flags are listed in the Flags column "
               "without a highlight. Edit cells to correct them; add or delete rows (select a row, then press Delete).")
    if concerns:
        by_id = dict(zip(edited["Row ID"].astype(str), edited["Beam mark"].astype(str)))
        with st.expander(f"⚠ {len(concerns)} row(s) need a look against the Prokon report"):
            st.dataframe(pd.DataFrame([(by_id.get(i, ""), n) for i, n in concerns.items()],
                                      columns=["Beam mark", "Prokon"]), hide_index=True, width="stretch")
    is_concern = drawing_reader.concern_rows(edited, concerns, conflicts)
    n_easy = int((~is_concern & ~edited["Reviewed"].fillna(False).astype(bool)).sum())
    if n_required and st.button(f"☑ Tick all unflagged rows ({n_easy})", disabled=not n_easy,
                                help="Ticks only rows without any concern. Highlighted rows, Prokon concerns and "
                                     "duplicate marks stay unticked: check each one and tick it yourself."):
        st.session_state[base_key] = (id(extraction), drawing_reader.tick_unflagged(edited, is_concern))
        st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
        st.rerun()
    with st.expander("What the flags and highlights mean"):
        st.markdown("\n".join(f"- `{k}`: {v}" for k, v in FLAG_DESCRIPTIONS.items()))
        st.markdown("Bottom bars **B3** are shown but, as in Excel mode, the checker does not use them. "
                    "For a blank support end the existing checker rules still copy the other end's top bars.")

    with coverage_slot:
        if prokon_up:
            cov = drawing_reader.coverage(edited, prokon_beam_marks(prokon_up.getvalue()))
            (st.success if not cov["missing"] else st.error)(
                f"🔎 **Coverage: found {len(cov['found'])} of {cov['total']} Prokon beam marks on the drawing**"
                + (f" · not on the drawing: {', '.join(cov['missing'])}" if cov["missing"] else "")
                + (f" · only on the drawing: {', '.join(cov['drawing_only'])}" if cov["drawing_only"] else ""))
        else:
            st.info("Upload the Prokon report to see the coverage (found X of Y Prokon beam marks on the drawing).")

    marks = [m for m in edited["Beam mark"].fillna("").astype(str) if m.strip()]
    if marks:
        pick = st.selectbox("Check one beam against Prokon", ["(choose a beam mark)"] + marks, key=f"beam_pick_{fp}")
        if pick != "(choose a beam mark)":
            show_beam_check(edited, pick, prokon_up)
    st.download_button(
            "⬇ Download schedule as Excel (Type 2 layout)", drawing_reader.table_to_type2_excel(edited),
            file_name="beam_schedule_from_drawing.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help="The schedule exactly as read (cells copied verbatim). Opens in Excel mode with Type 2. "
                 "Page and position per row are on a separate sheet.",
        )

    if conflicts:
        st.error("These beam marks appear more than once. Keep one row per span (delete or rename the others): "
                 + ", ".join(conflicts))
    ack = True
    if extraction.page_errors:
        ack = st.checkbox("Continue without the page(s) that could not be read")
    confirmed = st.checkbox(drawing_reader.CONFIRM_LABEL, key=f"confirm_{fp}") if n_required else True
    blockers = drawing_reader.run_blockers(edited, confirmed, bool(prokon_up), conflicts, is_concern, ack)

    run = st.button(RUN_LABEL, type="primary", key="run_drawing", disabled=bool(blockers))
    if blockers:
        st.markdown("**Run comparison is disabled because:**\n" + "\n".join(f"- {b}" for b in blockers))
    if run:
        bar = st.progress(0.0, text="Starting...")
        try:
            result = run_comparison_records(
                drawing_reader.table_to_records(edited), io.BytesIO(prokon_up.getvalue()),
                progress=lambda f, t: bar.progress(min(f, 1.0), text=t), remarks=drawing_reader.DRAWING_REMARKS,
            )
            st.session_state.update(
                result=result, result_source=st.session_state.get("schedule_source"), sheet="drawing",
                result_table=fingerprint_df(edited),
                chat_api=[], chat_display=[],
                drawing_result_ctx={"table": edited.copy(), "boxes": extraction.boxes, "files": files,
                                    "max_pages": limits["max_pages"], "method": extraction.method,
                                    "notes": dict(extraction.page_notes)},
            )
        except Exception as e:
            st.session_state.pop("result", None)
            st.error(f"Execution error: {e}")
        finally:
            bar.empty()
    st.session_state["current_table"] = fingerprint_df(edited)


def table_row_for(table, mark):
    rows = table[table["Beam mark"].fillna("").astype(str).str.strip() == mark.strip()]
    return None if rows.empty else rows.iloc[0]


def show_beam_table(row, checks, title, note=""):
    """A full-width, readable table for one beam span (Left / Middle / Right)."""
    page = _page_text(row)
    st.markdown(f"**{title}** · {page} · read from {row['Read from'] or 'manual entry'}" + (f" · {note}" if note else "")
                + "  " + chr(10) + "Top bars / Bottom bars / Stirrups are the drawing's values.")
    st.dataframe(drawing_reader.beam_detail(row, checks), hide_index=True, width="stretch")


def _page_text(row):
    page = str(row["Page"]).strip() if row["Page"] is not None else ""
    return f"page {page}" if page and page.lower() != "nan" else "page not known"


def show_beam_check(table, mark, prokon_up):
    """Review step: the selected beam's drawing values next to the Prokon requirement and OK/FAIL."""
    row = table_row_for(table, mark)
    if row is None:
        return
    if not prokon_up:
        show_beam_table(row, None, mark, "upload the Prokon report to see the requirement")
        return
    beams = prokon_beams(prokon_up.getvalue())
    m = match_span(mark, beams)
    if not m.checked:
        show_beam_table(row, None, mark, m.note)
        return
    if m.note:
        st.warning(f"⚠ {mark}: {m.note}")
    checks = check_span(drawing_reader.table_to_records(table[table.index == row.name])[0], m.data)
    show_beam_table(row, checks, mark, f"Prokon beam {m.matched}, span {m.span_used}")


def render_findings(df_all, result, ctx):
    """Possible typos, every FAIL span and every drawing-only beam as small readable tables (no AI, no images)."""
    if not ctx:
        return
    table = ctx["table"]
    fails = df_all[df_all["Overall Status"] == "FAIL"]
    typos = drawing_reader.typo_rows(table)
    typo_marks = {mark for mark, _, _ in typos}
    st.markdown(f"**{len(typos)} possible typo(s)**, "
                f"**{fails['Beam Mark'].nunique()} span(s) with a FAIL**, "
                f"**{len(result.excel_only)} drawing beam(s) not in Prokon**, "
                f"**{len(result.pdf_only)} Prokon beam(s) not on the drawing**. "
                "Check each one against the drawing (page shown); the drawing may be pre-update.")

    def span_checks(mark):
        span_rows = df_all[df_all["Beam Mark"] == mark]        # all three positions, not only the FAIL ones
        return span_rows[RESULT_COLUMNS].values.tolist() if len(span_rows) == 3 else None

    if typos:
        st.markdown("#### ⚠ Possible typos on the drawing")
        st.caption("A bar count here cannot fit the beam width even in two layers. The checker still uses the value "
                   "as written, so the result below may be a false OK or a false FAIL. Highlight only: OK/FAIL is "
                   "not changed; check the drawing.")
        for mark, row, issues in typos:
            with st.container(border=True):
                show_beam_table(row, span_checks(mark), f"⚠ possible typo · {mark}",
                                "; ".join(plausibility.describe(i) for i in issues))

    if not fails.empty:
        st.markdown("#### ❌ Spans with a FAIL")
    for mark in fails["Beam Mark"].unique():
        row = table_row_for(table, str(mark))
        checks = span_checks(mark)
        title = f"❌ {mark}" + (" · ⚠ possible typo" if mark in typo_marks else "")
        with st.container(border=True):
            if row is None or checks is None:
                st.markdown(f"**{title}**")
                st.dataframe(df_all[df_all["Beam Mark"] == mark], hide_index=True, width="stretch")
            else:
                show_beam_table(row, checks, title)

    if result.excel_only:
        st.markdown("#### On the drawing, not in the Prokon report")
        for base in result.excel_only:
            for m in table["Beam mark"].fillna("").astype(str):
                if m.strip() and drawing_reader.mark_key(drawing_reader.clean_suffix(m)[0]) == drawing_reader.mark_key(base):
                    with st.container(border=True):
                        show_beam_table(table_row_for(table, m), None, m, "not in the Prokon report")
    if result.pdf_only:
        st.markdown("#### In the Prokon report, not found on the drawing")
        st.write(", ".join(result.pdf_only))
    if result.unchecked or result.warned:
        st.markdown("#### ⚠ Not checked, or checked with a note")
        st.dataframe(pd.DataFrame(
            [(m, NOT_CHECKED, n) for m, n in result.unchecked] + [(m, "checked", n) for m, n in result.warned],
            columns=["Beam mark", "Status", "Note"]), hide_index=True, width="stretch")
    if fails.empty and not typos and not result.excel_only and not result.pdf_only and not result.unchecked \
            and not result.warned:
        st.success("No possible typos, no FAILs and no unmatched beams. Still spot-check a few beams against the drawing.")


def unmatched_table(result):
    rows = [(m, "In Prokon, not in schedule") for m in result.pdf_only]
    rows += [(m, "In schedule, not in Prokon") for m in result.excel_only]
    rows += [(m, f"In Prokon, but no results read for it: {NOT_CHECKED.lower()}") for m in sorted(result.no_data_bases)]
    return pd.DataFrame(rows, columns=["Beam mark", "Where"])


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


# Sign-in and sign-out run as button callbacks, before the script draws the page. Forcing a rerun
# from the sidebar instead would stop the run before the main page's widgets are drawn, and Streamlit
# would then reset them (e.g. the schedule source would jump back to Excel and results would vanish).
def submit_access_code(password):
    code = st.session_state.get("access_code_input", "")
    st.session_state["access_outcome"] = access.check_access_code(st.session_state, code, password)
    st.session_state["access_code_input"] = ""


def render_sign_in(password):
    wait = access.lock_remaining_seconds(st.session_state)
    if wait:
        st.error(f"Too many wrong codes. Try again in {math.ceil(wait / 60)} min.")
        return
    with st.form("access_form", border=False):
        st.text_input("Access code", type="password", key="access_code_input", help="Ask the app owner for the code.")
        st.form_submit_button("Sign in", type="primary", on_click=submit_access_code, args=(password,))
    outcome = st.session_state.pop("access_outcome", None)
    if outcome == "wrong":
        st.error(f"Wrong access code. {access.attempts_left(st.session_state)} attempt(s) left.")
    elif outcome == "empty":
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
            st.button("Sign out", on_click=lambda: access.sign_out(st.session_state))
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
        "max_tokens": int_setting("MAX_OUTPUT_TOKENS", env, 8000),
        "drawing_session": int_setting("MAX_DRAWING_CALLS_PER_SESSION", env, 40),
        "drawing_max_tokens": int_setting("DRAWING_MAX_OUTPUT_TOKENS", env, drawing_reader.DEFAULT_MAX_OUTPUT_TOKENS),
        "max_pages": int_setting("MAX_DRAWING_PAGES", env, drawing_reader.DEFAULT_MAX_PAGES),
        "dxf_mb": int_setting("MAX_DXF_MB", env, cad_reader.DEFAULT_MAX_MB),
        "dxf_entities": int_setting("MAX_DXF_ENTITIES", env, cad_reader.DEFAULT_MAX_ENTITIES),
        "dxf_timeout": int_setting("DXF_TIMEOUT_SECONDS", env, cad_reader.DEFAULT_TIMEOUT_S),
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
    if st.session_state.get("schedule_source") in DRAWING_MODES:
        st.caption("The assistant sees only the comparison results rows and a summary of how each schedule row "
                   "was read (method, page, flags). With the PDF text layer or a DXF, nothing from the drawing is sent to "
                   "any AI provider; only the AI vision option sends the drawing pages to Anthropic. "
                   "The app stores nothing.")
    else:
        st.caption("The assistant only sees the comparison results table, not your PDF or Excel files.")

st.title(f"🏗️ {APP_NAME}")
st.caption("Compare required steel from a **Prokon** continuous-beam report against the provided steel in a "
           "**beam schedule**: an Excel file, or a schedule drawing.")

# ---------------------------------------------------------------- 1. Input files (same in both modes)
drawing_state = None
run = False
with st.container(border=True):
    st.subheader("1. Input files")
    source = st.radio("Schedule source", list(SOURCE_CAPTIONS), horizontal=True, key="schedule_source",
                      captions=list(SOURCE_CAPTIONS.values()),
                      help="Excel and CAD (DXF) need no API key and use no AI. A drawing PDF is read from its text "
                           "layer (free); only scans and images need Claude vision.")
    col_s, col_p = st.columns(2)
    with col_p:
        pdf_up = st.file_uploader("Prokon report (.pdf)", type=["pdf"], key="prokon_pdf")
    with col_s:
        if source == EXCEL_MODE:
            excel_up = st.file_uploader("Excel beam schedule (.xlsx)", type=["xlsx", "xls"])
            sheet_name = None
            if excel_up:
                try:
                    sheets = pd.ExcelFile(io.BytesIO(excel_up.getvalue())).sheet_names
                    sheet_name = st.selectbox("Excel sheet", sheets, index=pick_default_sheet(sheets))
                except Exception as e:
                    st.error(f"Cannot read sheet list: {e}")
        elif source == CAD_MODE:
            drawing_state = render_cad_upload(limits)
        else:
            drawing_state = render_drawing_input(ai_provider, api_key, key_mode, ai_model, limits, extra_rules,
                                                 secrets_to_hide)

    if source == EXCEL_MODE:
        fmt = st.radio("Excel format", list(EXCEL_FORMATS), index=1,
                       format_func=lambda k: EXCEL_FORMATS[k]["label"], horizontal=True)
        run = st.button(RUN_LABEL, type="primary", key="run_excel", disabled=not (excel_up and pdf_up))
    elif drawing_state is None:
        st.caption("Upload the drawing to read its schedule. The review step and the comparison button appear next.")

# ---------------------------------------------------------------- 2. Review (drawing mode only)
if drawing_state is not None:
    with st.container(border=True):
        render_review(drawing_state, pdf_up, limits)

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
from_drawing = st.session_state.get("result_source") in DRAWING_MODES
if result is not None and st.session_state.get("result_source") != source:
    result = None  # results belong to another schedule source
if result is not None and from_drawing and drawing_state is None:
    result = None  # the drawing was removed
if result is not None and from_drawing and st.session_state.get("current_table") != st.session_state.get("result_table"):
    st.warning(f"The reviewed table changed after the comparison ran. Click **{RUN_LABEL}** again.")
    result = None

# ---------------------------------------------------------------- 3. Results (same in both modes)
# The tabs are always shown, so the AI Assistant is easy to find; they fill in once a comparison has run.
NOT_RUN_NOTE = "Run a comparison to see results."
df_all, schedule_ctx, beam_widths = None, None, None

if result is not None:
    df_all = result.to_dataframe()
    n_fail = int((df_all["Overall Status"] == "FAIL").sum())

    if from_drawing:
        ctx = st.session_state.get("drawing_result_ctx", {})
        read_from = {str(m).strip(): r for m, r in zip(ctx["table"]["Beam mark"], ctx["table"]["Read from"])}
        df_all.insert(0, "Read from", [read_from.get(str(m).strip(), "") for m in df_all["Beam Mark"]])
        df_all.insert(0, "Schedule source", drawing_reader.SOURCE_LABEL)
        n_found, n_total = len(result.pdf_matched_bases), len(result.pdf_beam_names)
        how = {drawing_reader.READ_TEXT: "read from the PDF text layer, **no AI was involved in reading the drawing**",
               drawing_reader.READ_CAD: "read from the DXF's CAD text, **no AI was involved in reading the drawing**",
               }.get(ctx.get("method"), "read with **AI vision** and reviewed by you")
        st.info(f"📐 **{drawing_reader.SOURCE_LABEL}** · coverage: found **{n_found} of {n_total}** Prokon beam marks "
                f"on the drawing · schedule {how}. The drawing may be pre-update: FAILs and unmatched beams are "
                "discrepancies to double-check.")
        schedule_ctx = drawing_reader.schedule_summary(ctx["table"], ctx.get("method"), result.pdf_only,
                                                       result.excel_only, n_total, n_found, ctx.get("notes"))
        # beam widths from the reviewed drawing table, for width-aware fix suggestions
        beam_widths = {str(m).strip(): plausibility.beam_width(sz)
                       for m, sz in zip(ctx["table"]["Beam mark"], ctx["table"]["Size"])
                       if str(m).strip() and plausibility.beam_width(sz)}

    if result.matched_count == 0:
        where = "the reviewed drawing table" if from_drawing else f"sheet '{st.session_state.get('sheet')}'"
        st.warning(f"None of the beams in the PDF matched {where}. Check the beam marks"
                   + ("." if from_drawing else " and the Excel format option."))
    else:
        st.success(f"Checked {result.matched_count} beam span(s).")
    if result.unchecked or result.warned:
        st.warning(f"⚠ **{len(result.unchecked)} span(s) not checked** (no Prokon result: neither OK nor FAIL) · "
                   f"**{len(result.warned)} checked span(s) with a check note** (e.g. another Prokon span used, or "
                   "zero required steel). They are highlighted in yellow in the table below and listed in the "
                   "exports' \"Check note\" column.")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spans checked", result.matched_count)
    m2.metric("Position rows", len(result.rows))
    m3.metric("FAIL rows", n_fail)
    m4.metric("Unmatched beams", len(result.pdf_only) + len(result.excel_only) + len(result.no_data_bases))

if source in DRAWING_MODES:
    tab_results, tab_findings, tab_ai = st.tabs(["📋 Results", "🔎 Findings to check", "🤖 AI Assistant"])
else:
    tab_results, tab_ai = st.tabs(["📋 Results", "🤖 AI Assistant"])
    tab_findings = None

with tab_results:
    if result is None:
        st.info(NOT_RUN_NOTE + (f" Upload both files and click **{RUN_LABEL}**." if source == EXCEL_MODE else ""))
    else:
        with st.container(border=True):
            st.subheader("Comparison results (3 position rows per span)")

            f1, f2 = st.columns([3, 1])
            search = f1.text_input("Search beam mark", placeholder="e.g. B101")
            status = f2.selectbox("Status", ["All", "FAIL Only", "OK Only", "Not checked", "With a check note"])

            df_view = df_all
            if search.strip():
                df_view = df_view[df_view["Beam Mark"].str.lower().str.contains(search.strip().lower(), regex=False)]
            if status == "FAIL Only":
                df_view = df_view[df_view["Overall Status"] == "FAIL"]
            elif status == "OK Only":
                df_view = df_view[df_view["Overall Status"] == "OK"]
            elif status == "Not checked":
                df_view = df_view[df_view["Overall Status"] == NOT_CHECKED]
            elif status == "With a check note":
                df_view = df_view[(df_view[NOTE_COLUMN] != "") & (df_view["Overall Status"] != NOT_CHECKED)]
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

        with st.container(border=True):
            unmatched = unmatched_table(result)
            st.subheader(f"Unmatched beams ({len(unmatched)})")
            if unmatched.empty:
                st.caption("None: every beam in the schedule is in the Prokon report and the other way round.")
            else:
                st.dataframe(unmatched, hide_index=True, width="stretch")

if tab_findings is not None:
    with tab_findings:
        if result is None:
            st.info(NOT_RUN_NOTE)
        else:
            render_findings(df_all, result, st.session_state.get("drawing_result_ctx", {}))

with tab_ai:
    if result is None:
        if not api_key:
            render_sign_in_notice(ai_provider)
        st.info(NOT_RUN_NOTE + " Then ask the assistant about them here.")
    else:
        render_assistant(result, ai_provider, api_key, ai_model, key_mode, limits, secrets_to_hide, schedule_ctx,
                         beam_widths)

if key_mode == "shared":
    left = limits["session"] - access.session_calls_used(st.session_state, "chat")
    left_d = limits["drawing_session"] - access.session_calls_used(st.session_state, "drawing")
    calls_left_slot.caption(f"AI calls left this session: assistant {max(0, left)} of {limits['session']}, "
                            f"drawing reading {max(0, left_d)} of {limits['drawing_session']}")
