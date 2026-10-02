"""Optional live accuracy test for AI vision reading of a schedule drawing.

Skipped unless RUN_EXTRACTION_ACCURACY=1. It calls the API and costs money (the app shows
the estimate; an A1 sheet is roughly $0.20-0.40 with Sonnet 5.5).

    RUN_EXTRACTION_ACCURACY=1 EXTRACTION_MODEL=claude-sonnet-5-5 pytest -s tests/test_extraction_accuracy.py

Ground truth is what is written on the drawing, never the Excel schedule (a drawing can be older
than the calculation, so Excel differences are findings, not read errors):
    1. SAMPLE_TRUTH: a hand-transcribed CSV (columns page, beam_mark, size, T1..T3, B1..B3, link_type,
       S1..S3, remark), if given; otherwise
    2. the drawing's own PDF text layer, read without AI.
If SAMPLE_EXCEL (or an .xlsx in sample_data/) is present, drawing-vs-Excel differences are reported
separately, together with how many of them the AI read also shows (discrepancy recall).

Other settings: SAMPLE_DRAWING, SAMPLE_SHEET, SAMPLE_FORMAT, EXTRACTION_PROVIDER, MIN_FIELD_ACCURACY.
Reports are printed and saved as CSV in sample_data/ (git-ignored).
"""

import os
from pathlib import Path

import pandas as pd
import pypdf
import pytest

from beam_checker import agent, checker, drawing_reader as dr
from beam_checker.parsers import is_arrow_symbol, mark_key

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "sample_data"
FIELDS = {"t1": "T1", "t2": "T2", "t3": "T3", "b1": "B1", "b2": "B2", "st_l": "S1", "st_m": "S2", "st_r": "S3"}

pytestmark = pytest.mark.skipif(os.environ.get("RUN_EXTRACTION_ACCURACY") != "1",
                                reason="live API test; set RUN_EXTRACTION_ACCURACY=1 to run")


def _is_prokon(pdf):
    text = (pypdf.PdfReader(pdf).pages[0].extract_text() or "").upper()
    return "BENDING MOMENTS" in text or "CONTINUOUS BEAM" in text


def _find_drawing():
    if os.environ.get("SAMPLE_DRAWING"):
        return Path(os.environ["SAMPLE_DRAWING"])
    cands = [p for p in sorted(DATA.glob("*")) if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
    cands += [p for p in sorted(DATA.glob("*.pdf")) if not _is_prokon(p)]
    return cands[0] if cands else None


def _api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _eff(rows):
    """Cells -> the strings the checker uses (arrows resolved to T2, layers joined, dashes blank)."""
    out = {}
    for f in FIELDS:
        vals = []
        for r in rows:
            v = r[f]
            if f in ("t1", "t3") and is_arrow_symbol(v) and v.strip() not in ("-", "—"):
                v = r["t2"]
            if v and v.strip() not in ("-", "—", "nan"):
                vals.append(v)
        out[f] = "+".join(vals).replace(" ", "").upper()
    return out


def _truth_records(pages):
    path = os.environ.get("SAMPLE_TRUTH")
    if path:
        df = pd.read_csv(path, dtype=str).fillna("").rename(columns={"beam_mark": "Beam mark"})
        df["Page"], df["Flags"], df["Size"], df["Remark"] = df.get("page", ""), "", df.get("size", ""), df.get("remark", "")
        return "hand transcription", dr.table_to_records(df)
    ex = dr.read_text_layer(pages)
    if ex is None:
        pytest.skip("no SAMPLE_TRUTH and the drawing has no text layer to use as ground truth")
    return "drawing text layer", dr.table_to_records(ex.table)


def test_ai_vision_reading_accuracy():
    provider = os.environ.get("EXTRACTION_PROVIDER", "anthropic")
    if provider != "anthropic":
        pytest.skip(f"drawing reading is not implemented for provider '{provider}' yet")
    drawing = _find_drawing()
    if not (drawing and drawing.exists()):
        pytest.skip("no drawing in sample_data")
    key = _api_key()
    if not key:
        pytest.skip("no ANTHROPIC_API_KEY in the environment or .env")

    model = os.environ.get("EXTRACTION_MODEL", agent.DEFAULT_MODEL)
    pages = dr.load_pages([(drawing.name, drawing.read_bytes())])
    truth_name, truth = _truth_records(pages)
    truth = {mark_key(r.mark): r for r in truth}

    usage, client = [], agent.make_client(key)

    def send(**kw):
        msg = dr.stream_send(client)(**kw)
        usage.append(msg.usage)
        return msg

    extraction = dr.extract_drawing(pages, send, model)
    got = {mark_key(r.mark): r for r in dr.table_to_records(extraction.table)}

    rows, hits, total = [], 0, 0
    for mark, t in sorted(truth.items()):
        g = got.get(mark)
        te, ge = _eff(t.rows), (_eff(g.rows) if g else dict.fromkeys(FIELDS, "<missing>"))
        row = {"mark": t.mark, "found": g is not None}
        for f in FIELDS:
            row[f"{f}_truth"], row[f"{f}_ai"], row[f"{f}_ok"] = te[f], ge[f], te[f] == ge[f]
            hits += te[f] == ge[f]
            total += 1
        rows.append(row)
    report = pd.DataFrame(rows)
    report.to_csv(DATA / f"accuracy_{provider}_{model}.csv", index=False, encoding="utf-8-sig")

    # drawing-vs-Excel findings and discrepancy recall (optional)
    excel = Path(os.environ["SAMPLE_EXCEL"]) if os.environ.get("SAMPLE_EXCEL") else next(iter(sorted(DATA.glob("*.xlsx"))), None)
    known = caught = 0
    if excel:
        sheet = os.environ.get("SAMPLE_SHEET") or next(
            (s for s in pd.ExcelFile(excel).sheet_names if "BEAM" in s.upper() or "SCHEDULE" in s.upper()), None)
        xl = {mark_key(r.mark): r for r in checker.read_excel_schedule(excel, sheet, os.environ.get("SAMPLE_FORMAT", "Format 2"))}
        findings = []
        for mark, t in truth.items():
            if mark not in xl:
                continue
            te, xe = _eff(t.rows), _eff(xl[mark].rows)
            ge = _eff(got[mark].rows) if mark in got else None
            for f in FIELDS:
                if te[f] != xe[f]:
                    known += 1
                    hit = ge is not None and ge[f] == te[f]
                    caught += hit
                    findings.append({"mark": t.mark, "field": FIELDS[f], "drawing": te[f], "excel": xe[f],
                                     "ai_read": ge[f] if ge else "<missing>", "caught": hit})
        pd.DataFrame(findings).to_csv(DATA / f"findings_drawing_vs_excel_{model}.csv", index=False, encoding="utf-8-sig")

    found = int(report["found"].sum())
    print(f"\n=== AI vision reading: {provider} / {model} (ground truth: {truth_name}) ===")
    print(f"Rows found: {found} of {len(truth)} | extra rows: {len(set(got) - set(truth))}")
    for f, name in FIELDS.items():
        print(f"  {name:3} accuracy: {report[f'{f}_ok'].mean():.0%}")
    print(f"Overall field accuracy: {hits / max(1, total):.1%}")
    if known:
        print(f"Drawing-vs-Excel discrepancies (findings, not errors): {known} | caught by the AI read: {caught} "
              f"({caught / known:.0%})")
    print(f"API requests: {len(usage)} | input tokens: "
          f"{sum(u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0) for u in usage)}"
          f" | output tokens: {sum(u.output_tokens for u in usage)}")

    assert not extraction.page_errors
    assert found > 0
    if os.environ.get("MIN_FIELD_ACCURACY"):
        assert hits / max(1, total) >= float(os.environ["MIN_FIELD_ACCURACY"])
