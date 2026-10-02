"""Read beam schedule tables directly from a PDF's text layer, using word positions (no AI).

CAD-exported schedule drawings usually keep every table cell as positioned text. When they do,
reading those words is exact and free, so it is tried before any vision call:

1. Find a "Mark" header word; the beam marks are the mark-like words below it in that column.
2. The header words between the first row and the "Mark" word define the columns
   (e.g. "Top Left", "Bottom Middle", "Stirrups Right"); each is mapped to a schedule field.
3. Each mark's row is the band of words at the same height; each word goes to the nearest column.

The result uses the same record fields as the AI extraction, plus the row and header boxes
(in PDF points), so each row's position on the page is known.

The DXF reader (cad_reader) uses the same functions with cad_rules=True, which adds rules for CAD
schedules: marks with a level prefix (L5-B101-1), sub-headers under a group header ("TOP BARS" over
T1 / T2 / T3), "BEAM MK". A CAD table object with cell lines is read by its grid (find_grid_table).
"""

import bisect
import re
import statistics
from dataclasses import dataclass, field

import pypdfium2 as pdfium

MARK_RE = re.compile(r"^[A-Z]{1,6}\d{1,4}[A-Za-z]?(-\d{1,2})?$")
CAD_MARK_RE = re.compile(r"^(?:[A-Z]{1,3}\d{1,3}-)?[A-Z]{1,6}\d{1,4}[A-Za-z]?(-\d{1,2})?$")  # also a level prefix
SUB_HEADER_RE = re.compile(r"[TBS][123]")
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
    flags: tuple = ()        # cell flags carried into the record (e.g. "formatting_removed" from CAD text)
    symbol: bool = False     # text mapped from a known symbol-font glyph (e.g. a Wingdings 3 arrow): exact

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
    title: str = ""          # title row above the headers (CAD table objects only)


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


def _header_words(label):
    """Header label as plain upper-case words: punctuation and spacing ignored ('SIZE WxD)' -> SIZE WXD).
    For matching headers only; cell values are never normalised."""
    return re.sub(r"[^A-Z0-9]+", " ", label.upper()).split()


def _field_for_header(label, cad_rules=False):
    """Map a column header (e.g. 'Top Left', 'Stirrups TypeLeft', 'B2') to a record field.

    cad_rules: 'Remark' is the remark (the usual rules read it as a mark, since it contains MARK); when the
    usual rules find nothing, a sub-header word T1-T3 / B1-B3 / S1-S3 under a group header
    ('MAIN REINFORCEMENT TOP BARS T2', 'LINKS S1') gives the field, and 'BEAM MK' is the mark.
    """
    if cad_rules and any(w.startswith("REMARK") for w in _header_words(label)):
        return "remark"
    fld = _base_field_for_header(label)
    if fld is not None or not cad_rules:
        return fld
    words = _header_words(label)
    subs = [w for w in words if SUB_HEADER_RE.fullmatch(w)]
    if subs:
        return subs[-1]                       # the lowest header row is the sub-header
    if words[-2:] == ["BEAM", "MK"]:
        return "beam_mark"
    return None


def _base_field_for_header(label):
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


def _is_mark_header(w, cad_rules=False):
    if w.text.strip().upper() in ("MARK", "BEAM MARK", "MARK NO", "MARK NO."):
        return True
    return cad_rules and " ".join(_header_words(w.text)) in ("BEAM MARK", "BEAM MK", "MARK NO")


def _mark_re(cad_rules):
    return CAD_MARK_RE if cad_rules else MARK_RE


def _header_columns(header, text_h, cad_rules=False):
    """Group header words into columns: [(label, field or None, centre x)], left to right."""
    out = []
    for g in _cluster([w.cx for w in header], 2.5 * text_h):
        lo, hi = g[0] - 0.01, g[-1] + 0.01
        parts = sorted((w for w in header if lo <= w.cx <= hi), key=lambda w: (-w.cy, w.x0))
        label = " ".join(w.text for w in parts)
        out.append((label, _field_for_header(label, cad_rules), statistics.mean(g)))
    return out


EXPECTED_HEADERS = ("Mark", "Size", "Top Left / Middle / Right (or T1-T3)", "Bottom Left / Middle / Right (or B1-B3)",
                    "Stirrups Left / Middle / Right (or S1-S3)", "Side bar", "Stirrups Type", "Remark / Span Type")
_HEADER_HINTS = ("MARK", "SIZE", "TOP", "BOT", "STIRRUP", "LINK", "SIDE", "REMARK", "SPAN")


def header_report(words, limit=20, cad_rules=False):
    """When no table is found: the header-like labels that were found, so the user sees what did not match.

    Around each "Mark" word, the labels on its header rows; without a "Mark" word, texts that look like headers.
    """
    found = []
    for anchor in [w for w in words if _is_mark_header(w, cad_rules)]:
        text_h = max(anchor.y1 - anchor.y0, 1.0)
        band = [w for w in words if anchor.y0 - 1.5 * text_h <= w.cy <= anchor.y1 + 3.5 * text_h]
        found += [label for label, _, _ in _header_columns(band, text_h, cad_rules)]
    if not found:
        found = [w.text for w in words if any(h in w.text.upper() for h in _HEADER_HINTS) and len(w.text) <= 30]
    return list(dict.fromkeys(found))[:limit]


def _record(mark_text, placed, row_box, source_note):
    """One record from the words placed in each field's column: [(field, Word)] in reading order.

    "symbol_fields" (only when there are any): fields whose whole cell is one known symbol-font glyph.
    """
    cells, word_flags, symbol = {}, [], {}
    for fld, w in placed:
        cells.setdefault(fld, []).append(w.text)
        word_flags.extend(w.flags)
        symbol.setdefault(fld, []).append(w.symbol)
    rec = {f: "" for f in FIELDS}
    flags = list(dict.fromkeys(word_flags))
    for fld, texts in cells.items():
        if fld == "link_type":
            rec[fld] = "/".join(dict.fromkeys(texts))
        else:
            rec[fld] = " ".join(texts)
            if len(texts) > 1:
                flags.append("unreadable")
    rec["beam_mark"] = mark_text
    rec.update(confidence="high", flags=flags, source_note=source_note, row_box=row_box)
    symbol_fields = [fld for fld, marks in symbol.items() if marks == [True]]
    if symbol_fields:
        rec["symbol_fields"] = symbol_fields
    return rec


def find_tables(words, cad_rules=False):
    """Find schedule tables on one page. Returns [TextTable]; empty if there is none."""
    tables = []
    mark_re = _mark_re(cad_rules)
    for anchor in [w for w in words if _is_mark_header(w, cad_rules)]:
        text_h = max(anchor.y1 - anchor.y0, 1.0)
        # beam marks: mark-like words below the header, in the same column, in one contiguous run
        below = sorted((w for w in words if mark_re.match(w.text) and abs(w.cx - anchor.cx) < 6 * text_h
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
        columns, unmapped = {}, []
        for label, fld, centre in _header_columns(header, text_h, cad_rules):
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
            placed, extra = [], []
            for w in sorted(band, key=lambda w: w.x0):
                c, fld = min(centres, key=lambda cf: abs(cf[0] - w.cx))
                if abs(c - w.cx) > 0.6 * spacing + (w.x1 - w.x0) / 2:
                    extra.append(w.text)
                    continue
                placed.append((fld, w))
            if extra:
                table.notes.append(f"row {i} ({mark.text}): text outside any column ignored: {', '.join(extra)}")
            table.records.append(_record(mark.text, placed, (x_lo, mark.cy - pitch / 2, x_hi, mark.cy + pitch / 2),
                                         f"text layer, table row {i}"))
        tables.append(table)
    return tables


def find_grid_table(words, col_edges, row_edges):
    """Read a CAD table object whose cell lines are known (DXF only, cad_rules). Returns a TextTable or None.

    Each text goes to the cell its centre is in, so texts of different cells are never joined. A header text
    whose cell spans several columns ("TOP BARS" over T1 / T2 / T3; its x0..x1 is the cell width) is part of
    each of their labels; one spanning most of the columns is the table title. Rows below the headers without
    a beam mark (e.g. '#N/A' left by a data link) are skipped and listed in the notes, and the content of a
    column without a header is ignored (also noted).
    """
    xs, ys = sorted(col_edges), sorted(row_edges)          # y up: row r from the top is ys[n_rows-r-1]..ys[n_rows-r]
    n_cols, n_rows = len(xs) - 1, len(ys) - 1
    if n_cols < 2 or n_rows < 2:
        return None

    def col(w):
        i = bisect.bisect(xs, w.cx) - 1
        return i if 0 <= i < n_cols else None

    def row(w):
        i = bisect.bisect(ys, w.cy) - 1
        return n_rows - 1 - i if 0 <= i < n_rows else None

    anchor = next((w for w in sorted(words, key=lambda w: -w.cy) if _is_mark_header(w, cad_rules=True)), None)
    if anchor is None or col(anchor) is None or row(anchor) is None:
        return None
    cells = {}
    for w in words:
        r, c = row(w), col(w)
        if r is not None and c is not None:
            cells.setdefault((r, c), []).append(w)
    first = next((r for r in range(row(anchor) + 1, n_rows)
                  if any(CAD_MARK_RE.match(w.text) for w in cells.get((r, col(anchor)), []))), None)
    if first is None:
        return None
    first_top = ys[n_rows - first]

    header = [w for w in words if w.cy > first_top and col(w) is not None]
    centres = [(xs[c] + xs[c + 1]) / 2 for c in range(n_cols)]
    cover = {id(w): [c for c in range(n_cols) if w.x0 <= centres[c] <= w.x1] or [col(w)] for w in header}
    labelled = {c for cs in cover.values() for c in cs}
    titles = [w for w in header if len(cover[id(w)]) > 1 and len(cover[id(w)]) > len(labelled) / 2]
    columns, unmapped, field_of = {}, [], {}
    for c in range(n_cols):
        parts = sorted((w for w in header if w not in titles and c in cover[id(w)]), key=lambda w: (-w.cy, w.x0))
        if not parts:
            continue
        label = " ".join(w.text for w in parts)
        fld = _field_for_header(label, cad_rules=True)
        if fld is None:
            unmapped.append(label)
        else:
            columns.setdefault(fld, []).append(centres[c])
            field_of[c] = fld
    if "beam_mark" not in columns or not any(f in columns for f in ("T1", "T2", "T3", "B1", "B2")):
        return None

    mark_col = col(anchor)
    title = " ".join(w.text for w in sorted(titles, key=lambda w: (-w.cy, w.x0)))
    table = TextTable(columns, (xs[0], first_top, xs[-1], ys[-1]), unmapped_headers=unmapped, title=title)
    ignored, prev = {}, None
    for r in range(first, n_rows):
        in_row = {c: sorted(cells.get((r, c), []), key=lambda w: (-w.cy, w.x0)) for c in range(n_cols)}
        marks = in_row[mark_col]
        mark_text = " ".join(w.text for w in marks)
        if not CAD_MARK_RE.match(mark_text):
            texts = [w.text for c in range(n_cols) for w in in_row[c]]
            if texts and not any(_is_mark_header(w, cad_rules=True) for w in marks):     # not a repeated header
                where = f"after {prev}" if prev else "before the first beam"
                table.notes.append(f"row without a beam mark skipped ({where}): {', '.join(texts)}")
            continue
        for c in range(n_cols):
            if c not in labelled and in_row[c]:
                ignored[c] = ignored.get(c, 0) + len(in_row[c])
        placed = [(field_of[c], w) for c in sorted(field_of) for w in in_row[c]]
        row_box = (xs[0], ys[n_rows - r - 1], xs[-1], ys[n_rows - r])
        table.records.append(_record(mark_text, placed, row_box, f"text layer, table row {len(table.records) + 1}"))
        prev = mark_text
    for c, n in sorted(ignored.items()):
        table.notes.append(f"a column without a header was ignored ({n} cell(s))")
    return table if table.records else None


def read_pdf_tables(pdf_bytes):
    """{page index (0-based): [TextTable]} for every page of a PDF that has a schedule table."""
    doc = pdfium.PdfDocument(pdf_bytes)
    found = {}
    for i in range(len(doc)):
        tables = find_tables(page_words(doc[i]))
        if tables:
            found[i] = tables
    return found
