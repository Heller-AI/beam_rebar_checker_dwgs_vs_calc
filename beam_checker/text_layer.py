"""Read beam schedule tables directly from a PDF's text layer, using word positions (no AI).

CAD-exported schedule drawings usually keep every table cell as positioned text. When they do,
reading those words is exact and free, so it is tried before any vision call:

1. Find a "Mark" header word; the beam marks are the mark-like words below it in that column.
2. The header words between the first row and the "Mark" word define the columns
   (e.g. "Top Left", "Bottom Middle", "Stirrups Right"); each is mapped to a schedule field.
3. Each mark's row is the band of words at the same height; each word goes to the nearest column.

The result uses the same record fields as the AI extraction, plus the row and header boxes
(in PDF points) so the app can show a crop of the drawing next to each finding.
"""

import re
import statistics
from dataclasses import dataclass, field

import pypdfium2 as pdfium

MARK_RE = re.compile(r"^[A-Z]{1,6}\d{1,4}[A-Za-z]?(-\d{1,2})?$")
SIDE_WORDS = {"LEFT": 1, "MID": 2, "MIDDLE": 2, "CENTRE": 2, "CENTER": 2, "RIGHT": 3}
FIELDS = ("beam_mark", "size", "T1", "T2", "T3", "B1", "B2", "B3", "side_bars", "link_type",
          "S1", "S2", "S3", "remark")


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2


@dataclass
class TextTable:
    columns: dict            # field -> list of column centre x (link_type can have several)
    header_box: tuple        # (x0, y0, x1, y1) in PDF points, y up
    records: list = field(default_factory=list)   # dicts with FIELDS + "row_box"
    unmapped_headers: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def page_words(pdf_page):
    """Every text object on the page with its bounding box (PDF points, origin bottom-left)."""
    tp = pdf_page.get_textpage()
    words = []
    for obj in pdf_page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_TEXT,)):
        x0, y0, x1, y1 = obj.get_bounds()
        text = tp.get_text_bounded(x0, y0, x1, y1).strip()
        if text:
            words.append(Word(text, x0, y0, x1, y1))
    return words


def _field_for_header(label):
    """Map a column header (e.g. 'Top Left', 'Stirrups TypeLeft', 'B2') to a record field."""
    u = re.sub(r"[^A-Z0-9 ]", " ", label.upper())
    compact = u.replace(" ", "")
    side = next((n for w, n in SIDE_WORDS.items() if w in compact), None)
    m = re.fullmatch(r"([TBS])([123])", compact)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    if "MARK" in compact:
        return "beam_mark"
    if "SIZE" in compact:
        return "size"
    if "SIDE" in compact:
        return "side_bars"
    if "REMARK" in compact or "SPANTYPE" in compact or "NOTE" in compact:
        return "remark"
    if "STIRRUP" in compact or "LINK" in compact:
        if "TYPE" in compact:
            return "link_type"
        return f"S{side}" if side else None
    if "TOP" in compact and side:
        return f"T{side}"
    if ("BOT" in compact) and side:
        return f"B{side}"
    return None


def _cluster(values, tol):
    """Group sorted 1-D values whose neighbours are closer than tol."""
    groups = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


def find_tables(words):
    """Find schedule tables on one page. Returns [TextTable]; empty if there is none."""
    tables = []
    for anchor in [w for w in words if w.text.strip().upper() in ("MARK", "BEAM MARK", "MARK NO", "MARK NO.")]:
        text_h = max(anchor.y1 - anchor.y0, 1.0)
        # beam marks: mark-like words below the header, in the same column, in one contiguous run
        below = sorted((w for w in words if MARK_RE.match(w.text) and abs(w.cx - anchor.cx) < 6 * text_h
                        and w.cy < anchor.y0), key=lambda w: -w.cy)
        if len(below) < 2:
            continue
        pitches = [a.cy - b.cy for a, b in zip(below, below[1:])]
        pitch = statistics.median(pitches)
        marks = [below[0]]
        for prev, w in zip(below, below[1:]):
            if prev.cy - w.cy > 2.5 * pitch:
                break
            marks.append(w)
        if below[0].cy < anchor.y0 - 4 * pitch:   # first row too far below the header: not this table's
            continue

        # header: words between the first row and just above the "Mark" word
        top_limit = anchor.y1 + 2.5 * text_h
        first_row_top = marks[0].cy + pitch / 2
        header = [w for w in words if first_row_top <= w.y0 and w.y1 <= top_limit + text_h]
        groups = _cluster([w.cx for w in header], 2.5 * text_h)
        columns, unmapped = {}, []
        for g in groups:
            lo, hi = g[0] - 0.01, g[-1] + 0.01
            parts = sorted((w for w in header if lo <= w.cx <= hi), key=lambda w: (-w.cy, w.x0))
            label = " ".join(w.text for w in parts)
            fld = _field_for_header(label)
            centre = statistics.mean(g)
            if fld is None:
                unmapped.append(label)
            else:
                columns.setdefault(fld, []).append(centre)
        if "beam_mark" not in columns or not any(f in columns for f in ("T1", "T2", "T3", "B1", "B2")):
            continue

        centres = sorted((c, f) for f, cs in columns.items() for c in cs)
        spacing = min((b[0] - a[0] for a, b in zip(centres, centres[1:])), default=50)
        x_lo = min(w.x0 for w in header + marks) - spacing
        x_hi = max(w.x1 for w in header) + spacing
        table = TextTable(columns, (x_lo, first_row_top, x_hi, top_limit + text_h), unmapped_headers=unmapped)

        for i, mark in enumerate(marks, start=1):
            band = [w for w in words if abs(w.cy - mark.cy) < 0.45 * pitch and x_lo <= w.cx <= x_hi]
            cells, extra = {}, []
            for w in sorted(band, key=lambda w: w.x0):
                c, fld = min(centres, key=lambda cf: abs(cf[0] - w.cx))
                if abs(c - w.cx) > 0.6 * spacing + (w.x1 - w.x0) / 2:
                    extra.append(w.text)
                    continue
                cells.setdefault(fld, []).append(w.text)
            rec = {f: "" for f in FIELDS}
            flags = []
            for fld, texts in cells.items():
                if fld == "link_type":
                    rec[fld] = "/".join(dict.fromkeys(texts))
                else:
                    rec[fld] = " ".join(texts)
                    if len(texts) > 1:
                        flags.append("unreadable")
            rec["beam_mark"] = mark.text
            if extra:
                table.notes.append(f"row {i} ({mark.text}): text outside any column ignored: {', '.join(extra)}")
            rec.update(confidence="high", flags=flags, source_note=f"text layer, table row {i}",
                       row_box=(x_lo, mark.cy - pitch / 2, x_hi, mark.cy + pitch / 2))
            table.records.append(rec)
        tables.append(table)
    return tables


def read_pdf_tables(pdf_bytes):
    """{page index (0-based): [TextTable]} for every page of a PDF that has a schedule table."""
    doc = pdfium.PdfDocument(pdf_bytes)
    found = {}
    for i in range(len(doc)):
        tables = find_tables(page_words(doc[i]))
        if tables:
            found[i] = tables
    return found
