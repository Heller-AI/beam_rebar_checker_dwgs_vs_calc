# Beam Rebar Checker (Prokon vs Beam Schedule)

A web app that checks the reinforcement in a **beam schedule** against the **required steel from a Prokon continuous-beam PDF report**. The schedule can be an **Excel file** or a **schedule drawing** (PDF or PNG/JPG), read from the PDF text layer when possible or by Claude vision otherwise, and reviewed by you before the check runs.

For every beam span it reports 3 position rows (left support, mid-span, right support). For each row it compares:

| Check | Required (from Prokon) | Provided (from schedule) |
|---|---|---|
| Flexure | As top at start/end, max As bottom | Top bars (left/right), bottom bars |
| Shear | Asv/sv at start, max, end | Stirrups (left/mid/right zones) |

Any position where provided < required is flagged **FAIL**.

## Features

- Upload the Excel schedule and Prokon PDF in the browser. Nothing to install for end users.
- **Drawing mode**: upload the beam schedule drawing instead of an Excel file. Read from the PDF text layer when possible (free), otherwise with Claude vision; you can review and correct the table, and each finding is shown as a small table of drawing values vs Prokon requirement. See [Drawing mode](#drawing-mode).
- Picks the sheet automatically (the first one named *BEAM* or *SCHEDULE*).
- Two schedule layouts:
  - **Type 1**: mark in col A, top bars C–E, bottom F–G, stirrups K, L, M
  - **Type 2**: mark in col B, top bars E–G, bottom H–J, stirrups L, M, N
- Handles arrow symbols (→ ← "), cantilevers, multi-row bar layers and `-2` span suffixes.
- Filter by beam mark or status. FAIL rows are highlighted.
- Download the filtered results as CSV or Excel.
- Lists beams that are in the PDF but not the schedule, and the other way round.
- **AI Assistant** tab (optional, uses Claude): ask questions about the results, e.g. *"Why does B101-2 fail?"* or *"Suggest the smallest bar change to fix each FAIL"*. See [AI Assistant](#ai-assistant).

## Quick start (local)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

The app opens at http://localhost:8501.

## Deploy so anyone with the link can use it (Streamlit Community Cloud, free)

1. Push this folder to a GitHub repository (see *Data confidentiality* below first).
2. Go to <https://share.streamlit.io>, sign in with GitHub and click **Create app**.
3. Choose the repo and branch, and set **Main file path** to `app.py`.
4. Click **Deploy**. You get a URL like `https://<your-app>.streamlit.app` that you can share.

Every push to the branch redeploys the app automatically.

> **Data confidentiality:** a public repo and a public app URL can be seen by anyone.
> - `sample_data/` is git-ignored so project PDFs and schedules are never committed.
> - Uploaded files only stay in memory for the user's session and are not saved by the app.
> - For internal-only use, make the GitHub repo **private**. In the app's settings on Streamlit Cloud, you can then limit viewers to invited email addresses.

## AI Assistant

The assistant is an AI agent with tools. It can run on **Anthropic (Claude)** or **Zhipu (GLM)**, and you pick the provider in the sidebar. It looks up rows, summarises failures, and checks or suggests bar and stirrup arrangements. **All steel areas and Asv/sv values come from the app's own Python formulas** (`beam_checker/agent.py`). The model doesn't do the arithmetic, so every number can be traced. Its suggestions still need an engineer's review, because it can't check spacing, anchorage or detailing rules.

**It needs an API key, which is paid per use.** You can use an Anthropic key (console.anthropic.com), a Zhipu key (open.bigmodel.cn), or both. These are separate from any chat subscription.

### Access for colleagues: access code, no API key needed

On the online app, the owner stores their API keys in Streamlit secrets and gives colleagues an **access code**. Colleagues type only the code. They never see or need a key.

1. Create a key at <https://console.anthropic.com> → *API Keys* (and/or a Zhipu key), add credit, and **set a monthly spend limit**.
2. On Streamlit Cloud, open *App settings → Secrets* and paste the settings from `.streamlit/secrets.toml.example` with your real values. One `APP_PASSWORD` unlocks both providers' keys.
3. Share the code only with people you approve. Change `APP_PASSWORD` to revoke access; everyone signed in with the old code is signed out.

How it behaves:

- The sidebar shows only an **Access code** field. "Use my own API key instead" sits in a collapsed section for people who have their own key, which is never capped.
- An accepted code is remembered for the browser session (until the tab is closed or they sign out).
- **5 wrong codes lock the field for 10 minutes** in that session.
- **Cost limits on the shared key:** each model request counts as one AI call (one question can take several, because the assistant looks things up with tools; a detailed question used about 5 in testing).
  - `MAX_AI_CALLS_PER_SESSION` (default 30) per browser session.
  - `MAX_AI_CALLS_PER_DAY` (default 300) for the whole app, shared by all users. It is kept in memory, so it **resets when the app restarts** (reboot, redeploy, or Streamlit Cloud putting the app to sleep).
  - `MAX_OUTPUT_TOKENS` (default 8000) caps each response, including the model's internal reasoning. If an answer is cut off, the app says so with the stop reason and still shows the tables it computed.
- Keys and the code are never shown in the app, in error messages or in the tool-call log.

These limits make casual misuse expensive, not impossible: a new browser session starts a fresh session cap. The daily cap and the spend limit in the provider console are the real ceiling, so set both.

| Where the key is | `APP_PASSWORD` set? | Who can use your key |
|---|---|---|
| Streamlit Cloud secrets | Yes | Only people who type the access code, within the limits above |
| Streamlit Cloud secrets | No | Nobody: the key is ignored and users must bring their own |
| `.env` on your PC | Yes | Only people who type the access code |
| `.env` on your PC | No | Anyone who opens the app from your PC or over the office network, with no limits. Fine for running it only on your own PC |

**On your own PC:** copy `.env.example` to `.env` (git-ignored) and fill in your keys. `.env` is never uploaded, so it has no effect on the online app.

All settings (same names in `.env` and in Streamlit secrets): `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ZHIPU_API_KEY`, `ZHIPU_MODEL`, `ACTIVE_PROVIDER` (`anthropic` or `zhipu`, the sidebar default), `APP_PASSWORD`, `MAX_AI_CALLS_PER_SESSION`, `MAX_AI_CALLS_PER_DAY`, `MAX_OUTPUT_TOKENS`, and for drawing mode `MAX_DRAWING_PAGES`, `MAX_DRAWING_CALLS_PER_SESSION`, `DRAWING_MAX_OUTPUT_TOKENS`, `EXTRA_READING_RULES`.

### How answers are built

- Questions about all failures, shortfalls or fixes use one tool, `get_failures`, which returns a table computed by the app: one row per failing check (flexure and shear separately, one unit per cell), with required, provided, shortfall and **Provided / required (%)**, the counts as a fact sentence, and (for fixes) the smallest bar or stirrup change. The app draws this table itself above the model's comments, so it is readable even if the text is cut off.
- Fix suggestions use the beam width from the drawing's Size column: at most 2 layers that fit the width (cover, links, minimum clear gap), never smaller bars than those provided. They are marked "fits the width", "does not fit, needs engineer", "no valid option found, needs engineer", or in Excel mode "width unknown, fit not checked". Spacing, anchorage, laps and detailing are not checked.
- A typical question uses about 2 model calls (one tool request, one answer); at most 6 per question. The model runs with low reasoning effort, since the numbers come from code.
- **Number check:** every number in an answer is matched against the tool results in the conversation. Numbers that cannot be traced are listed in a warning under the answer (the answer is never blocked).

### Models and cost

Pick the Anthropic model in the sidebar. `ANTHROPIC_MODEL` sets the starting choice; any other model ID you put there appears as a "custom" option, and an unknown or retired ID gives a clear error message instead of a crash.

| Model | API ID | Price per million tokens (input / output) | Measured cost per question |
|---|---|---|---|
| Claude Sonnet 5.5 (default) | `claude-sonnet-5-5` | $2 / $10 | about $0.02–0.03 |
| Claude Opus 5.5 (higher quality) | `claude-opus-5-5` | $4 / $20 | about $0.04–0.06 |
| Claude Opus 5 (higher quality) | `claude-opus-5` | $5 / $25 | about $0.05–0.07 |

Prices are from the [Anthropic pricing page](https://platform.claude.com/docs/en/about-claude/pricing) (checked 2026-10-01). The cost per question was measured on Sonnet 5.5 for a summary and for a "suggest fixes" question on a 49-span sample; the Opus figures apply their prices to the same token counts. Follow-up questions in a long conversation cost more, because the earlier messages are sent again (prompt caching reduces this). Zhipu pricing is set by Zhipu; see open.bigmodel.cn.

**What the assistant sends to the AI provider:** only your question and the result rows that the assistant looks up (beam marks, bar notations, As values). The PDF and Excel files themselves are not sent. Drawing mode with AI vision is different: it sends the drawing pages (see [Drawing mode](#drawing-mode)). Check that this is allowed under your company's data policy.

## Drawing mode

Choose **Schedule source → Drawing (PDF or image)** to take the provided steel from a beam schedule drawing instead of an Excel file. Only the schedule source changes: the drawing is converted into a schedule table, and then the same flow as Excel mode runs (same Prokon upload, same **▶ Run comparison (all beams)** button, same results page, unmatched beams, downloads and AI Assistant). Excel mode needs no API key; drawing mode with the text layer doesn't either.

Drawings can be older than the calculation. The purpose of drawing mode is to **surface discrepancies for a person to double-check**, not to certify the design.

### How the drawing is read

1. **PDF text layer first (free, exact, nothing sent).** CAD-exported schedules usually keep every table cell as positioned text. The app finds the table from its header ("Mark", "Top Left", "Bottom Middle", "Stirrups Right"…) and reads each row from the word positions, exactly as written. No API key is needed and nothing leaves the server.
2. **AI vision only if needed.** For scans and images without a text layer, or if you choose it, Claude reads the page images. The app first shows the **page count and an estimated cost** and asks you to tick **"I confirm I am allowed to send this drawing to Anthropic"**. Each page is sent as an overview plus overlapping close-up tiles. Claude transcribes the rows through a structured tool call and checks every bar and stirrup string with the app's own parser. Uses Anthropic only; with the shared key it counts against `MAX_DRAWING_CALLS_PER_SESSION` and `MAX_AI_CALLS_PER_DAY`.

### Layout

1. **Input files** (same box in both modes): Schedule source; the schedule on the left (Excel upload, sheet and format, or the drawing upload with its reading result such as "42 rows found, cost $0" and the reading method); the Prokon report on the right (one upload shared by both modes).
2. **Review drawing schedule** (drawing mode only): coverage line, review table, **Check one beam against Prokon**, **⬇ Download schedule as Excel (Type 2 layout)**, then **▶ Run comparison (all beams)** (enabled straight away for text-layer rows; rows read by AI vision must be ticked first).
3. **Results** (identical in both modes): the tabs **Results** | **Findings to check** (drawing mode only) | **AI Assistant** are shown from the start, with "Run a comparison to see results" until a comparison has run; then the success line and metrics appear above them. The Results tab ends with an always-visible **Unmatched beams** table ("Beam mark", "Where").

### Review, coverage and findings

- **Review table:** rows read by **AI vision** must each be ticked **Reviewed** before the comparison can run, because AI can misread a value. Text-layer rows are the drawing's own text, copied exactly, so ticking them is optional (a checklist). Rows with medium/low confidence or a flag are highlighted in yellow. The page number and the reading method ("PDF text layer" or "AI vision") are shown. **Check one beam against Prokon** shows the selected span as a readable table: Left / Middle / Right with the drawing's top bars, bottom bars and stirrups next to the Prokon requirement and OK/FAIL, with the page number. Cells can be corrected and rows added or deleted.
- **Possible typos:** a bar count that cannot fit the beam width even in two layers (for example `33H25` in a 300 mm beam, probably meant `3H25`) is marked **⚠ possible typo** in the review table and listed at the top of **Findings to check**. Each `+` group of a notation (e.g. `6H32+6H25+6H25`) is checked on its own against the width from the Size column, using typical detailing values (25 mm cover, 10 mm links, clear spacing at least the bar diameter or 25 mm). It is a highlight only: the checker still uses the value as written, so OK/FAIL never changes; correct the cell during review if it is a typo.
- **Coverage, shown prominently:** "found **X of Y** Prokon beam marks on the drawing", with the lists of Prokon beams missing from the drawing and drawing beams missing from Prokon.
- **Download as Excel (converter):** the reviewed schedule as an `.xlsx` in the Type 2 layout (mark in B, top bars E-G, bottom bars H-J, stirrups L-N), with every cell copied verbatim (arrows, dashes and combined bars such as `2H13+2H13` are not changed). Page, reading method and flags are on a separate sheet that Excel mode does not read. Opening this file in Excel mode gives the same results as the drawing-mode comparison (tested).
- **Results** carry one extra line, **"AI-read drawing vs Prokon · coverage: found X of Y"**, which also says whether AI was involved in reading (with the PDF text layer it was not). The CSV/Excel exports add "Schedule source" and "Read from" columns.
- **Findings to check tab:** every span with a FAIL and every drawing beam that is not in Prokon, each shown as the same small table (drawing values vs Prokon requirement vs OK/FAIL, with the page number), so a person can check it against the drawing in seconds.

### Limits and cost

- Text-layer reading: free.
- AI vision: about $0.20-0.40 for one A1 sheet with Sonnet 5.5, $0.40-0.70 with Opus 5.5 and $0.50-0.90 with Opus 5 (estimate shown before every run). Results are cached per file and model for the browser session.
- `MAX_DRAWING_PAGES` (default 10) pages per upload; `DRAWING_MAX_OUTPUT_TOKENS` (default 32000) per AI request.
- `EXTRA_READING_RULES` (Streamlit secrets or `.env`, optional): private conventions added to the AI instructions at runtime, never shown in the app.

**AI Assistant in drawing mode:** the same assistant, access code and limits as in Excel mode. It also gets a `get_schedule_source` tool: how each row was read (method, page), which rows were uncertain or flagged, coverage, and beams only on the drawing or only in Prokon. It sees the comparison results rows and this summary, never the drawing itself.

**What is sent to Anthropic:** with text-layer reading, nothing from the drawing. With AI vision, the drawing page images and their PDF text. The Prokon report and Excel files are never sent. The app stores nothing; readings live only in the browser session.

### Known limitations

- The text-layer reader needs a "Mark" header and one table row per text line. Unusual layouts (rotated tables, merged multi-line cells) fall back to AI vision.
- AI vision uses Anthropic only. Zhipu GLM would need a GLM vision model with function calling.
- Link type codes (A1, Normal…) describe a stirrup shape, not the number of legs. When legs are not written the checker assumes 2.
- Bottom bars B3 are shown but, as in Excel mode, not used by the checker. For a blank support end the existing checker rule still copies the other end's top bars and stirrups.

## Project structure

```
.
├── app.py                    # Streamlit UI (entry point)
├── beam_checker/             # Core logic, no UI code
│   ├── parsers.py            # Bar / stirrup notation & beam-mark helpers
│   ├── prokon_pdf.py         # Extract required As / Asv from Prokon PDF
│   ├── checker.py            # Read schedule, match beams, produce results
│   ├── agent.py              # AI assistant: Claude / GLM + tools over the results
│   ├── access.py             # Access code, lockout and AI call limits
│   ├── text_layer.py         # Drawing mode: read schedule tables from the PDF text layer (no AI)
│   ├── plausibility.py       # Drawing mode: possible-typo screening of bar counts (highlight only)
│   ├── drawing_reader.py     # Drawing mode: pages, AI vision reading, checks, review table, per-beam tables
│   └── prompts.py            # All prompt text (assistant and extraction rules)
├── tests/                    # pytest unit + regression tests
├── sample_data/              # Put local test files here (git-ignored)
├── legacy/                   # Original Tkinter desktop version (reference only)
├── .streamlit/config.toml    # Streamlit settings (upload size, theme)
├── .streamlit/secrets.toml.example  # Template for the optional shared API key
├── requirements.txt          # Runtime dependencies
└── requirements-dev.txt      # + pytest
```

The core `beam_checker` package does not depend on Streamlit. You can use it from scripts too:

```python
from beam_checker import run_comparison

result = run_comparison("schedule.xlsx", "prokon.pdf", sheet_name="BEAM SCHEDULE", fmt="Format 2")
result.to_dataframe().to_excel("check.xlsx", index=False)
print(result.pdf_only, result.excel_only)
```

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/test_regression.py` runs an end-to-end check when a PDF and XLSX are present in `sample_data/`. Otherwise it is skipped. The drawing-mode tests use mocked model responses and make no API calls.

**Optional AI vision accuracy test** (calls the API and costs money): put a schedule drawing in `sample_data/`, then run

```bash
RUN_EXTRACTION_ACCURACY=1 EXTRACTION_MODEL=claude-sonnet-5-5 pytest -s tests/test_extraction_accuracy.py
```

Ground truth is what is written on the drawing: a hand-transcribed CSV (`SAMPLE_TRUTH`) or, by default, the drawing's own text layer. The Excel schedule is **not** used as ground truth, because a drawing can be older than the calculation; if an Excel file is present, drawing-vs-Excel differences are reported separately as findings, with how many of them the AI read also shows. Change `EXTRACTION_MODEL` to compare models. Other settings are listed in the test file.

## Assumptions and limitations

- Bar notation is `nHd` (e.g. `3H20+2H16`). Stirrups are `nHd-s` or `nHd/s`, with 2 legs if `n` is omitted.
- The Prokon PDF must contain text (not a scanned image). The tables *Bending Moments & Reinforcement* and *Shear Forces & Reinforcement* are read by column position.
- Beams are matched by base mark, ignoring spaces, `-`, `_` and `.`. If a span number isn't found in the PDF, the check falls back to span 1.
- This tool helps with checking. It does not replace an engineer's review.
