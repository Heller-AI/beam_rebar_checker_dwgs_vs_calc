"""End-to-end check on local sample files (skipped when sample_data/ is empty, e.g. on GitHub)."""

from pathlib import Path

import pytest

from beam_checker import run_comparison

DATA = Path(__file__).resolve().parent.parent / "sample_data"
PDFS = sorted(DATA.glob("*.pdf"))
XLSX = sorted(DATA.glob("*.xlsx"))


def _prokon_pdfs():
    """sample_data/ can also hold beam schedule drawings; keep only Prokon calculation reports."""
    import pypdf

    found = []
    for pdf in PDFS:
        text = pypdf.PdfReader(pdf).pages[0].extract_text() or ""
        if "BENDING MOMENTS" in text.upper() or "CONTINUOUS BEAM" in text.upper():
            found.append(pdf)
    return found


@pytest.mark.skipif(not (PDFS and XLSX), reason="no sample data")
def test_sample_run_produces_three_rows_per_span():
    prokon = _prokon_pdfs()
    if not prokon:
        pytest.skip("no Prokon report in sample_data")
    result = run_comparison(XLSX[0], prokon[0], fmt="Format 2")
    assert result.matched_count > 0
    assert len(result.rows) == 3 * result.matched_count
    assert {r[12] for r in result.rows} <= {"OK", "FAIL"}


def _drawing_pdfs():
    """Schedule drawings in sample_data/ (PDFs that are not Prokon reports)."""
    prokon = set(_prokon_pdfs())
    return [p for p in PDFS if p not in prokon]


@pytest.mark.skipif(not PDFS, reason="no sample data")
def test_drawing_schedule_downloaded_as_excel_gives_identical_results():
    """Drawing mode (text layer) vs the downloaded Type 2 Excel run through Excel mode."""
    import io

    from beam_checker import checker, drawing_reader as dr

    drawings, prokon = _drawing_pdfs(), _prokon_pdfs()
    if not (drawings and prokon):
        pytest.skip("needs a schedule drawing and a Prokon report in sample_data")
    pages = dr.load_pages([(drawings[0].name, drawings[0].read_bytes())])
    extraction = dr.read_text_layer(pages)
    if extraction is None:
        pytest.skip("the sample drawing has no text-layer schedule")
    table = extraction.table
    via_records = checker.run_comparison_records(dr.table_to_records(table), prokon[0], remarks=dr.DRAWING_REMARKS)
    via_excel = checker.run_comparison(io.BytesIO(dr.table_to_type2_excel(table)), prokon[0], dr.SCHEDULE_SHEET, "Format 2")

    assert via_records.matched_count == via_excel.matched_count > 0
    # identical rows; only the Remark text differs by design ("From drawing ..." vs "From Col ...")
    assert [r[:13] for r in via_records.rows] == [r[:13] for r in via_excel.rows]
    assert via_records.pdf_only == via_excel.pdf_only and via_records.excel_only == via_excel.excel_only

