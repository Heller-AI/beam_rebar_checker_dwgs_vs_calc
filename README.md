# Beam Rebar Checker (Prokon vs Beam Schedule)

A web app that checks the reinforcement in an **Excel beam schedule** against the **required steel from a Prokon continuous-beam PDF report**.

For every beam span it reports 3 position rows (left support, mid-span, right support). For each row it compares:

| Check | Required (from Prokon) | Provided (from schedule) |
|---|---|---|
| Flexure | As top at start/end, max As bottom | Top bars (left/right), bottom bars |
| Shear | Asv/sv at start, max, end | Stirrups (left/mid/right zones) |

Any position where provided < required is flagged **FAIL**.

## Features

- Upload the Excel schedule and Prokon PDF in the browser. Nothing to install for end users.
- Picks the sheet automatically (the first one named *BEAM* or *SCHEDULE*).
- Two schedule layouts:
  - **Type 1**: mark in col A, top bars C–E, bottom F–G, stirrups K, L, M
  - **Type 2**: mark in col B, top bars E–G, bottom H–J, stirrups L, M, N
- Handles arrow symbols (→ ← "), cantilevers, multi-row bar layers and `-2` span suffixes.
- Filter by beam mark or status. FAIL rows are highlighted.
- Download the filtered results as CSV or Excel.
- Lists beams that are in the PDF but not the schedule, and the other way round.
- **AI Assistant** tab (optional, uses Claude): ask questions about the results, e.g. *"Why does EDB31-2 fail?"* or *"Suggest the smallest bar change to fix each FAIL"*. See [AI Assistant](#ai-assistant).

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

`.env` settings (see `.env.example`): `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ZHIPU_API_KEY`, `ZHIPU_MODEL`, `ACTIVE_PROVIDER` (`anthropic` or `zhipu`, the sidebar default) and `APP_PASSWORD`. The same names work in Streamlit Cloud secrets. One `APP_PASSWORD` protects both keys.


1. Create a key at <https://console.anthropic.com> → *API Keys*, and add credit under *Billing*.
2. **On your own PC:** copy `.env.example` to `.env` (git-ignored) and paste the key after `ANTHROPIC_API_KEY=`. Refresh the app. If colleagues open your app over the office network, also set `APP_PASSWORD=` so they need a code.
3. **On Streamlit Cloud:** `.env` is not uploaded, so by default users must enter **their own** key. To share yours with selected people, paste both `ANTHROPIC_API_KEY` and `APP_PASSWORD` into *App settings → Secrets* (see `.streamlit/secrets.toml.example`) and give the code only to people you approve. **Without `APP_PASSWORD`, a key in secrets is never used.** Also set a monthly spend limit in the Anthropic console.

| Where the key is | APP_PASSWORD set? | Who can use your key |
|---|---|---|
| `.env` (your PC) | No | Anyone opening the app from your PC or office network |
| `.env` or Cloud secrets | Yes | Only people who type the access code |
| Cloud secrets | No | Nobody (key ignored, users must bring their own) |

Cost is roughly a few US cents per question with Claude Opus 5.5, and about half that with Sonnet 5.5 (you can pick the model in the sidebar). Longer conversations cost more per question because the history is resent.

**What is sent to Anthropic:** only your question and the result rows that the assistant looks up (beam marks, bar notations, As values). The PDF and Excel files themselves are not sent. Check that this is allowed under your company's data policy.

## Project structure

```
.
├── app.py                    # Streamlit UI (entry point)
├── beam_checker/             # Core logic, no UI code
│   ├── parsers.py            # Bar / stirrup notation & beam-mark helpers
│   ├── prokon_pdf.py         # Extract required As / Asv from Prokon PDF
│   ├── checker.py            # Read schedule, match beams, produce results
│   └── agent.py              # AI assistant: Claude / GLM + tools over the results
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

result = run_comparison("schedule.xlsx", "prokon.pdf", sheet_name="CIS_BEAM SCHEDULE", fmt="Format 2")
result.to_dataframe().to_excel("check.xlsx", index=False)
print(result.pdf_only, result.excel_only)
```

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/test_regression.py` runs an end-to-end check when a PDF and XLSX are present in `sample_data/`. Otherwise it is skipped.

## Assumptions and limitations

- Bar notation is `nHd` (e.g. `3H20+2H16`). Stirrups are `nHd-s` or `nHd/s`, with 2 legs if `n` is omitted.
- The Prokon PDF must contain text (not a scanned image). The tables *Bending Moments & Reinforcement* and *Shear Forces & Reinforcement* are read by column position.
- Beams are matched by base mark, ignoring spaces, `-`, `_` and `.`. If a span number isn't found in the PDF, the check falls back to span 1.
- This tool helps with checking. It does not replace an engineer's review.
