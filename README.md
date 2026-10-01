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
  - `MAX_OUTPUT_TOKENS` (default 4000) caps each response.
- Keys and the code are never shown in the app, in error messages or in the tool-call log.

These limits make casual misuse expensive, not impossible: a new browser session starts a fresh session cap. The daily cap and the spend limit in the provider console are the real ceiling, so set both.

| Where the key is | `APP_PASSWORD` set? | Who can use your key |
|---|---|---|
| Streamlit Cloud secrets | Yes | Only people who type the access code, within the limits above |
| Streamlit Cloud secrets | No | Nobody: the key is ignored and users must bring their own |
| `.env` on your PC | Yes | Only people who type the access code |
| `.env` on your PC | No | Anyone who opens the app from your PC or over the office network, with no limits. Fine for running it only on your own PC |

**On your own PC:** copy `.env.example` to `.env` (git-ignored) and fill in your keys. `.env` is never uploaded, so it has no effect on the online app.

All settings (same names in `.env` and in Streamlit secrets): `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ZHIPU_API_KEY`, `ZHIPU_MODEL`, `ACTIVE_PROVIDER` (`anthropic` or `zhipu`, the sidebar default), `APP_PASSWORD`, `MAX_AI_CALLS_PER_SESSION`, `MAX_AI_CALLS_PER_DAY`, `MAX_OUTPUT_TOKENS`.

### Models and cost

Pick the Anthropic model in the sidebar. `ANTHROPIC_MODEL` sets the starting choice; any other model ID you put there appears as a "custom" option, and an unknown or retired ID gives a clear error message instead of a crash.

| Model | API ID | Price per million tokens (input / output) | Measured cost per question |
|---|---|---|---|
| Claude Sonnet 5.5 (default) | `claude-sonnet-5-5` | $2 / $10 | about $0.02–0.03 |
| Claude Opus 5.5 (higher quality) | `claude-opus-5-5` | $4 / $20 | about $0.04–0.06 |
| Claude Opus 5 (higher quality) | `claude-opus-5` | $5 / $25 | about $0.05–0.07 |

Prices are from the [Anthropic pricing page](https://platform.claude.com/docs/en/about-claude/pricing) (checked 2026-10-01). The cost per question was measured on Sonnet 5.5 for a summary and for a "suggest fixes" question on a 49-span sample; the Opus figures apply their prices to the same token counts. Follow-up questions in a long conversation cost more, because the earlier messages are sent again (prompt caching reduces this). Zhipu pricing is set by Zhipu; see open.bigmodel.cn.

**What is sent to Anthropic:** only your question and the result rows that the assistant looks up (beam marks, bar notations, As values). The PDF and Excel files themselves are not sent. Check that this is allowed under your company's data policy.

## Project structure

```
.
├── app.py                    # Streamlit UI (entry point)
├── beam_checker/             # Core logic, no UI code
│   ├── parsers.py            # Bar / stirrup notation & beam-mark helpers
│   ├── prokon_pdf.py         # Extract required As / Asv from Prokon PDF
│   ├── checker.py            # Read schedule, match beams, produce results
│   ├── agent.py              # AI assistant: Claude / GLM + tools over the results
│   └── access.py             # Access code, lockout and AI call limits
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
