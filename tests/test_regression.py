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
