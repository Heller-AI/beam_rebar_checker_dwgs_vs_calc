"""End-to-end check on local sample files (skipped when sample_data/ is empty, e.g. on GitHub)."""

from pathlib import Path

import pytest

from beam_checker import run_comparison

DATA = Path(__file__).resolve().parent.parent / "sample_data"
PDFS = sorted(DATA.glob("*.pdf"))
XLSX = sorted(DATA.glob("*.xlsx"))


@pytest.mark.skipif(not (PDFS and XLSX), reason="no sample data")
def test_sample_run_produces_three_rows_per_span():
    result = run_comparison(XLSX[0], PDFS[0], fmt="Format 2")
    assert result.matched_count > 0
    assert len(result.rows) == 3 * result.matched_count
    assert {r[12] for r in result.rows} <= {"OK", "FAIL"}
