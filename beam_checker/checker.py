"""Compare the Excel beam schedule (provided steel) against Prokon results (required steel)."""

from dataclasses import dataclass, field

import pandas as pd

from .parsers import (
    clean_suffix,
    is_arrow_symbol,
    is_valid_beam_mark,
    normalize_str,
    parse_bar_notation,
    parse_stirrup_list,
)
from .prokon_pdf import extract_all_beams_from_pdf

# Zero-based column indices in the beam schedule sheet
EXCEL_FORMATS = {
    "Format 1": {
        "label": "Type 1 (Mark Col A | Top: C-E, Bot: F-G | Stirrups: K,L,M)",
        "mark": 0, "t1": 2, "t2": 3, "t3": 4, "b1": 5, "b2": 6, "st_l": 10, "st_m": 11, "st_r": 12,
    },
    "Format 2": {
        "label": "Type 2 (Mark Col B | Top: E-G, Bot: H-J | Stirrups: L,M,N)",
        "mark": 1, "t1": 4, "t2": 5, "t3": 6, "b1": 7, "b2": 8, "st_l": 11, "st_m": 12, "st_r": 13,
    },
}

HEADER_MARKS = ["MARK", "BEAM MARK", "PPVC BEAM SCHEDULE", "BEAM SCHEDULE", "NAN"]

RESULT_COLUMNS = [
    "Beam Mark", "Position / Location",
    "Req. As (mm²)", "Provided Bars", "Prov. As (mm²)", "Flex Ratio (%)", "Flex Status",
    "Req. Asv/sv", "Provided Stirrup", "Prov. Asv/sv", "Shear Ratio (%)", "Shear Status",
    "Overall Status", "Remark",
]

EMPTY_SPAN = {"req_t1": 0.0, "req_b2": 0.0, "req_t3": 0.0, "req_asv_l": 0.0, "req_asv_m": 0.0, "req_asv_r": 0.0}

# Cell keys of one schedule row, in the checker's own terms:
# t1/t2/t3 = top bars left/mid/right, b1/b2 = bottom bars, st_l/st_m/st_r = stirrups left/mid/right
CELL_KEYS = ("t1", "t2", "t3", "b1", "b2", "st_l", "st_m", "st_r")

# Remark column text per position (left, mid, right)
EXCEL_REMARKS = ("From Col E/L", "From Col H/M", "From Col G/N")


@dataclass
class ScheduleRecord:
    """Provided steel for one beam span, independent of where it was read from.

    `rows` holds one dict of CELL_KEYS -> cell text per schedule row (several rows = bar layers),
    exactly as written in the schedule; arrows, blanks and cantilever ends are resolved by
    check_span, not here.
    """
    mark: str
    rows: list
    size: str = ""
    remark: str = ""
    source: str = ""
    confidence: str = ""
    flags: list = field(default_factory=list)


@dataclass
class CheckResult:
    rows: list = field(default_factory=list)
    pdf_beam_names: set = field(default_factory=set)
    excel_beam_names: set = field(default_factory=set)
    pdf_matched_bases: set = field(default_factory=set)
    matched_count: int = 0

    def to_dataframe(self):
        return pd.DataFrame(self.rows, columns=RESULT_COLUMNS)

    @property
    def pdf_only(self):
        return sorted(b for b in self.pdf_beam_names if b not in self.excel_beam_names)

    @property
    def excel_only(self):
        return sorted(b for b in self.excel_beam_names if b not in self.pdf_matched_bases)


def _cell(row, idx):
    return str(row[idx]).strip() if pd.notna(row[idx]) else ""


def is_table_title(val_mark):
    """A title such as '... BEAM SCHEDULE' in the mark column (not one of the exact header words)."""
    return val_mark.upper() not in HEADER_MARKS and "SCHEDULE" in val_mark.upper()


def group_excel_spans(df, fmt):
    """Group schedule rows by beam mark; a mark cell starts a new span, blank rows continue it.

    A table title in the mark column ends the current span, like a mark does, but is not a beam:
    the rows after it are skipped until the next mark.
    """
    col_mark = EXCEL_FORMATS[fmt]["mark"]
    span_groups = []
    curr_mark, curr_rows = None, []

    for _, row in df.iterrows():
        val_mark = _cell(row, col_mark)
        if val_mark and is_table_title(val_mark):
            if curr_mark and curr_rows:
                span_groups.append((curr_mark, curr_rows))
            curr_mark, curr_rows = None, []
        elif val_mark and val_mark.upper() not in HEADER_MARKS:
            if curr_mark and curr_rows:
                span_groups.append((curr_mark, curr_rows))
            curr_mark, curr_rows = val_mark, [row]
        elif curr_mark:
            curr_rows.append(row)

    if curr_mark and curr_rows:
        span_groups.append((curr_mark, curr_rows))
    return span_groups


def excel_rows_to_record(mark, rows_group, fmt):
    """Convert one span's Excel rows into a ScheduleRecord (cells read as text, NaN -> "")."""
    c = EXCEL_FORMATS[fmt]
    return ScheduleRecord(mark=mark, rows=[{k: _cell(r, c[k]) for k in CELL_KEYS} for r in rows_group])


def read_excel_schedule(excel_file, sheet_name=None, fmt="Format 2"):
    """Read an Excel beam schedule into ScheduleRecords, one per span (beam mark)."""
    df = pd.read_excel(excel_file, sheet_name=sheet_name if sheet_name else 0, header=None)
    needed = max(v for k, v in EXCEL_FORMATS[fmt].items() if k != "label") + 1
    if df.shape[1] < needed:
        raise ValueError(
            f"The sheet has {df.shape[1]} columns but {fmt} needs at least {needed}. "
            "Check the Excel format option and the sheet."
        )
    return [excel_rows_to_record(mark, rows, fmt) for mark, rows in group_excel_spans(df, fmt)]


def _match_pdf_base(excel_base, pdf_beams):
    if excel_base in pdf_beams:
        return excel_base
    norm_base = normalize_str(excel_base)
    for p_base in pdf_beams:
        if normalize_str(p_base) == norm_base:
            return p_base
    return None


def check_span(record, p_data, remarks=EXCEL_REMARKS):
    """Return the 3 result rows (left / mid / right) for one schedule span."""
    beam_mark = record.mark

    raw_t1, raw_t3, raw_b = [], [], []
    raw_stl, raw_stm, raw_str = [], [], []

    for r in record.rows:
        v_t1, v_t2, v_t3 = r["t1"], r["t2"], r["t3"]
        v_b1, v_b2 = r["b1"], r["b2"]
        v_stl, v_stm, v_str = r["st_l"], r["st_m"], r["st_r"]

        # Arrow in a support column means "same as mid column"
        s_t1 = v_t2 if is_arrow_symbol(v_t1) else v_t1
        s_t3 = v_t2 if is_arrow_symbol(v_t3) else v_t3
        s_b2 = v_b2 if (v_b2 and v_b2 != "nan") else v_b1

        for val, target in (
            (s_t1, raw_t1), (s_t3, raw_t3), (s_b2, raw_b),
            (v_stl, raw_stl), (v_stm, raw_stm), (v_str, raw_str),
        ):
            if val and val != "nan":
                target.append(val)

    comb_t1 = "+".join(raw_t1) if raw_t1 else "-"
    comb_t3 = "+".join(raw_t3) if raw_t3 else "-"
    comb_b2 = "+".join(raw_b) if raw_b else "-"

    # Cantilever: one support blank -> use the other support's top bars
    if comb_t3 in ["-", "—"] and comb_t1 not in ["-", "—", ""]:
        comb_t3 = comb_t1
    elif comb_t1 in ["-", "—"] and comb_t3 not in ["-", "—", ""]:
        comb_t1 = comb_t3

    prov_t1, not_t1 = parse_bar_notation(comb_t1)
    prov_b2, not_b2 = parse_bar_notation(comb_b2)
    prov_t3, not_t3 = parse_bar_notation(comb_t3)

    prov_asv_l, not_stl = parse_stirrup_list(raw_stl)
    prov_asv_m, not_stm = parse_stirrup_list(raw_stm)
    prov_asv_r, not_str = parse_stirrup_list(raw_str)

    # Cantilever: missing end-zone stirrups fall back to mid, then the other end
    if not_str in ["-", "—"] or prov_asv_r == 0:
        if prov_asv_m > 0:
            prov_asv_r, not_str = prov_asv_m, not_stm
        elif prov_asv_l > 0:
            prov_asv_r, not_str = prov_asv_l, not_stl

    if not_stl in ["-", "—"] or prov_asv_l == 0:
        if prov_asv_m > 0:
            prov_asv_l, not_stl = prov_asv_m, not_stm
        elif prov_asv_r > 0:
            prov_asv_l, not_stl = prov_asv_r, not_str

    positions = [
        ("Left Support (Pos Start)", p_data["req_t1"], not_t1, prov_t1, p_data["req_asv_l"], not_stl, prov_asv_l, remarks[0]),
        ("Mid-Span (Max Bot)", p_data["req_b2"], not_b2, prov_b2, p_data["req_asv_m"], not_stm, prov_asv_m, remarks[1]),
        ("Right Support (Pos End)", p_data["req_t3"], not_t3, prov_t3, p_data["req_asv_r"], not_str, prov_asv_r, remarks[2]),
    ]

    rows = []
    for pos_name, r_as, p_bars, p_as, r_asv, p_stir, p_asv, rem in positions:
        f_ratio = f"{(p_as / r_as * 100):.1f}%" if r_as > 0 else "N/A"
        f_st = "OK" if p_as >= r_as else "FAIL (Deficit)"
        s_ratio = f"{(p_asv / r_asv * 100):.1f}%" if r_asv > 0 else "N/A"
        s_st = "OK" if p_asv >= r_asv else "FAIL (Deficit)"
        overall = "OK" if (f_st == "OK" and s_st == "OK") else "FAIL"

        rows.append([
            beam_mark, pos_name,
            f"{r_as:.1f}", p_bars, f"{p_as:.1f}", f_ratio, f_st,
            f"{r_asv:.3f}", p_stir, f"{p_asv:.3f}", s_ratio, s_st,
            overall, rem,
        ])
    return rows


def run_comparison(excel_file, pdf_file, sheet_name=None, fmt="Format 2", progress=None):
    """Full check from an Excel schedule. `progress(fraction, text)` receives 0..1 across the whole run."""
    return run_comparison_records(
        lambda: read_excel_schedule(excel_file, sheet_name, fmt), pdf_file,
        progress=progress, reading_text=f"Reading sheet: {sheet_name}...",
    )


def run_comparison_records(records, pdf_file, progress=None, remarks=EXCEL_REMARKS, reading_text="Reading schedule..."):
    """Compare ScheduleRecords (a list, or a callable returning one) against a Prokon PDF."""

    def report(frac, text):
        if progress:
            progress(frac, text)

    result = CheckResult()

    pdf_beams = extract_all_beams_from_pdf(pdf_file, progress=lambda f, t: report(0.05 + f * 0.40, t))
    if not pdf_beams:
        raise ValueError("No beam reinforcement data found in the Prokon PDF.")
    result.pdf_beam_names = set(pdf_beams)

    report(0.45, reading_text)
    if callable(records):
        records = records()
    total = len(records)

    for idx, record in enumerate(records):
        report(0.45 + (idx + 1) / total * 0.50, f"Checking beam span {idx + 1}/{total}")

        excel_base, excel_span = clean_suffix(record.mark)
        if not is_valid_beam_mark(excel_base):
            continue
        result.excel_beam_names.add(excel_base)

        matched = _match_pdf_base(excel_base, pdf_beams)
        if not matched:
            continue

        result.matched_count += 1
        result.pdf_matched_bases.add(matched)
        spans_data = pdf_beams[matched]

        # Look up the matching span number, fall back to span 1 / first available
        target_span = excel_span if excel_span in spans_data else 1
        if target_span not in spans_data and spans_data:
            target_span = next(iter(spans_data))

        p_data = spans_data.get(target_span, EMPTY_SPAN)
        result.rows.extend(check_span(record, p_data, remarks))

    report(1.0, "Done")
    return result
