"""Read beam schedule tables from a CAD drawing saved as DXF (no AI, nothing sent anywhere).

A DXF keeps every schedule cell as typed text with its position, so it is read like the PDF
text layer: each text entity becomes a positioned word and text_layer rebuilds the rows and
columns (with its CAD rules: level-prefixed marks, group headers over T1 / T2 / T3). This module
collects the words:

- TEXT, MTEXT (formatting codes stripped, the cell flagged), attribute text of block references,
  text inside blocks, and CAD table objects (ACAD_TABLE, through their graphical block);
- a table object whose block has cell lines is read by that grid (text_layer.find_grid_table): each
  cell's position comes from the MTEXT attachment point and width, so neighbouring cells are never
  joined; without cell lines its text is read by position like any other text;
- text in a symbol font (Wingdings 3 arrows) is mapped with the shared table in parsers; an unknown
  symbol is kept as written and the cell flagged;
- model space and every paper-space layout, each read on its own;
- rotated text: words are grouped by rotation and turned back to horizontal before reading.

DWG is a closed format and is not read: the user saves the drawing as DXF first.
External references, data links and embedded objects are never followed; the reader warns instead.
"""

import io
import math
import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache

import ezdxf
from ezdxf import recover
from ezdxf.filemanagement import dxf_stream_info
from ezdxf.protocols import virtual_entities
from ezdxf.fonts import fonts
from ezdxf.tools.text_size import get_font_name

from . import text_layer
from .parsers import font_key, is_symbol_font, map_symbol_text

FORMATTING_FLAG = "formatting_removed"   # underline / overline / strike-through or stacked text removed: review
FONT_FLAG = "font_codes_removed"          # only font, height, colour... codes removed: shown, not highlighted
SYMBOL_FLAG = text_layer.SYMBOL_FLAG       # symbol-font text with no known meaning, kept as written: review

DEFAULT_MAX_MB = 30
DEFAULT_MAX_ENTITIES = 300_000
DEFAULT_TIMEOUT_S = 20
MAX_BLOCK_DEPTH = 8

DWG_MESSAGE = ("DWG cannot be read here. In your CAD software use Save As DXF (or export only the schedule sheet), "
               "then upload the DXF.")
TOO_BIG_HINT = ("Export only the schedule sheet to DXF, or run the app locally with a higher limit "
                "({setting}, see the README).")


class CadReadError(Exception):
    """The DXF could not be read; the message is safe to show to the user."""


@dataclass(frozen=True)
class CadLimits:
    max_mb: int = DEFAULT_MAX_MB
    max_entities: int = DEFAULT_MAX_ENTITIES
    timeout_s: int = DEFAULT_TIMEOUT_S


@dataclass
class CadTable:
    layout_no: int           # 1-based, in tab order (Model first)
    layout_name: str
    table_no: int            # 1-based within the layout
    rotation: float          # text rotation of the table, degrees
    table: text_layer.TextTable
    kind: str = ""           # "single span" / "continuous span" from the table title, else ""

    @property
    def score(self):
        """How well the headers matched: mapped columns first, then rows."""
        return (len(self.table.columns), len(self.table.records))

    def label(self):
        turned = f" · rotated {self.rotation:g}°" if self.rotation else ""
        kind = f" ({self.kind})" if self.kind else ""
        return (f"Layout '{self.layout_name}' · table {self.table_no}{kind} · {len(self.table.records)} rows · "
                f"{len(self.table.columns)} columns matched{turned}")

    def position(self, rec):
        """Where a record's row is in the drawing: layout and drawing coordinates of the row centre."""
        x0, y0, x1, y1 = rec["row_box"]
        x, y = _rotate((x0 + x1) / 2, (y0 + y1) / 2, self.rotation)
        return {"layout_no": self.layout_no, "layout": self.layout_name, "x": round(x, 1), "y": round(y, 1)}


@dataclass
class CadReading:
    tables: list = field(default_factory=list)          # [CadTable]
    warnings: list = field(default_factory=list)        # shown to the user
    found_headers: list = field(default_factory=list)   # header labels found when no table matched
    layouts: list = field(default_factory=list)         # layout names in tab order
    entities_read: int = 0      # drawing objects walked (block and table content included, empty table cells not)
    table_cells: int = 0        # non-empty cells of table objects read by their grid

    def best(self):
        return max(self.tables, key=lambda t: t.score, default=None)


def is_dwg(data):
    return data[:4] == b"AC10"


def _rotate(x, y, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


@lru_cache(maxsize=256)
def _font(name, height, width_factor):
    return fonts.make_font(name, height, width_factor)


def _style_font(entity):
    try:
        return get_font_name(entity)
    except Exception:
        return ""


def _text_width(entity, text, height, width_factor=1.0):
    """Text width in drawing units. The DXF does not store it: it is measured with the style's font (or a
    stand-in font when that font is not installed), falling back to an estimate from the text height."""
    try:
        return _font(_style_font(entity), round(height, 6), round(width_factor or 1.0, 6)).text_width(text)
    except Exception:
        return 0.8 * height * (width_factor or 1.0) * len(text)


def _symbol_text(text, font_names):
    """(text, flags) for text drawn in font_names: a known symbol-font glyph becomes its arrow; text in an
    unknown symbol font (or mixed with other fonts in one cell) is kept as written and flagged."""
    if not any(is_symbol_font(f) for f in font_names):
        return text, ()
    if len({font_key(f) for f in font_names}) > 1:
        return text, (SYMBOL_FLAG,)
    mapped, known = map_symbol_text(text, font_names[0])
    return (mapped, ()) if known else (text, (SYMBOL_FLAG,))


# TEXT special codes: %%u / %%o / %%k toggle underline, overline, strike-through (formatting);
# %%c, %%d, %%p are the diameter, degree and plus/minus symbols (content, shown as the symbol).
_TEXT_FORMAT_RE = re.compile(r"%%[uUoOkK]")
# MTEXT inline codes: \S stacks text (fractions, tolerances) and \L \O \K (and lower case) toggle underline,
# overline and strike-through, which can carry meaning; the others (\f font, \H height, \W width, \C colour,
# \Q oblique, \T tracking, \A alignment, \p paragraph, \~ hard space) and grouping braces only change the look.
_MTEXT_CODE_RE = re.compile(r"\\(.)|([{}])")
_MTEXT_REVIEW_CODES = set("SLlOoKk")
_MTEXT_FONT_RE = re.compile(r"\\[fF]([^|;\\]*)")


def _mtext_flags(raw):
    codes = [m.group(1) or m.group(2) for m in _MTEXT_CODE_RE.finditer(raw)]
    codes = [c for c in codes if c not in ("P", "\\")]       # paragraph break and an escaped backslash
    if any(c in _MTEXT_REVIEW_CODES for c in codes):
        return (FORMATTING_FLAG,)
    return (FONT_FLAG,) if codes else ()


def _words_from_text(e, rotation_by_word):
    """TEXT / ATTRIB -> one word, in the frame where the text reads horizontally."""
    raw = e.dxf.get("text", "")
    text = e.plain_text().strip()
    if not text:
        return []
    shown, symbol_flags = _symbol_text(text, [_style_font(e)])
    mapped, text = shown != text, shown
    h = e.dxf.get("height", 1.0) or 1.0
    wf = e.dxf.get("width", 1.0) or 1.0
    rot = e.dxf.get("rotation", 0.0) % 360
    halign, valign = e.dxf.get("halign", 0), e.dxf.get("valign", 0)
    w = _text_width(e, text, h, wf)
    ins = e.dxf.get("insert")
    align = e.dxf.get("align_point") if (halign or valign) and e.dxf.hasattr("align_point") else None
    if halign in (3, 5) and align is not None:          # ALIGNED / FIT: text fills insert -> align point
        u0, v = _rotate(ins.x, ins.y, -rot)
        u1, _ = _rotate(align.x, align.y, -rot)
        x0, x1 = min(u0, u1), max(u0, u1)
    else:
        p = align if align is not None else ins
        u, v = _rotate(p.x, p.y, -rot)
        x0 = {1: u - w / 2, 2: u - w, 4: u - w / 2}.get(halign, u)
        x1 = x0 + w
        if halign == 4:                                 # MIDDLE: centred both ways
            valign = 2
    y0 = {2: v - h / 2, 3: v - h}.get(valign, v)
    flags = ((FORMATTING_FLAG,) if _TEXT_FORMAT_RE.search(raw) else ()) + symbol_flags
    word = text_layer.Word(text, x0, y0, x1, y0 + h, flags, symbol=mapped)
    rotation_by_word[id(word)] = rot
    return [word]


def _words_from_mtext(e, rotation_by_word, cell_width=False):
    """MTEXT -> one word per line, formatting codes stripped (and the cell flagged).

    The attachment point says which point of the text box the insert point is (1-3 top, 4-6 middle, 7-9 bottom;
    left, centre, right). cell_width: when the MTEXT has a width (a table cell), the word spans that width
    instead of the measured text, so a header cell spanning several columns covers all of them.
    """
    raw = e.text
    lines = [ln.strip() for ln in e.plain_text(split=True, fast=False)]
    if not any(lines):
        return []
    inline_fonts = _MTEXT_FONT_RE.findall(raw)
    font_names = list(dict.fromkeys(inline_fonts)) or [_style_font(e)]
    h = e.dxf.get("char_height", 1.0) or 1.0
    rot = e.get_rotation() % 360
    att = e.dxf.get("attachment_point", 1)
    pitch = h * 5 / 3 * (e.dxf.get("line_spacing_factor", 1.0) or 1.0)
    total = h + pitch * (len(lines) - 1)
    ins = e.dxf.get("insert")
    u, v = _rotate(ins.x, ins.y, -rot)
    top = {1: v, 2: v, 3: v, 4: v + total / 2, 5: v + total / 2, 6: v + total / 2}.get(att, v + total)
    box_w = (e.dxf.get("width", 0.0) or 0.0) if cell_width else 0.0
    side = (att - 1) % 3                                 # 0 left, 1 centre, 2 right
    flags = _mtext_flags(raw)
    words = []
    for i, line in enumerate(lines):
        if not line:
            continue
        shown, symbol_flags = _symbol_text(line, font_names)
        mapped, line = shown != line, shown
        w = box_w or _text_width(e, line, h)
        x0 = (u, u - w / 2, u - w)[side]
        y1 = top - i * pitch
        word = text_layer.Word(line, x0, y1 - h, x0 + w, y1, flags + symbol_flags, symbol=mapped)
        rotation_by_word[id(word)] = rot
        words.append(word)
    return words


def _grid_edges(entities):
    """(x of the vertical lines, y of the horizontal lines) of a table block, or None without cell lines."""
    xs, ys = [], []
    for e in entities:
        if e.dxftype() != "LINE":
            continue
        s, t = e.dxf.start, e.dxf.end
        length = abs(s.x - t.x) + abs(s.y - t.y)
        if length == 0:
            continue
        if abs(s.x - t.x) <= 1e-6 * length:
            xs.append(s.x)
        elif abs(s.y - t.y) <= 1e-6 * length:
            ys.append(s.y)

    def distinct(values):
        values = sorted(values)
        tol = 1e-5 * (values[-1] - values[0]) if values else 0
        out = []
        for v in values:
            if not out or v - out[-1] > tol:
                out.append(v)
        return out

    xs, ys = distinct(xs), distinct(ys)
    return (xs, ys) if len(xs) >= 3 and len(ys) >= 3 else None


class _Budget:
    """Entity-count and time limits while walking the drawing."""

    def __init__(self, limits, started):
        self.limits, self.deadline, self.count = limits, started + limits.timeout_s, 0

    def tick(self):
        self.count += 1
        if self.count > self.limits.max_entities:
            raise CadReadError(f"The DXF has more than {self.limits.max_entities:,} entities (drawing objects, "
                               "including block and table content; empty table cells are not counted), the limit "
                               "for this app. " + TOO_BIG_HINT.format(setting="MAX_DXF_ENTITIES"))
        if self.count % 500 == 0 and time.monotonic() > self.deadline:
            raise CadReadError(f"Reading the DXF took longer than {self.limits.timeout_s} s, the limit for this app. "
                               + TOO_BIG_HINT.format(setting="DXF_TIMEOUT_SECONDS"))


def _collect(entities, budget, stats, rotation_by_word, grids, depth=0, cell_width=False):
    """Positioned words from a layout or block, expanding block references and table objects.

    A table object with cell lines is not returned as words: (its words, column x, row y) goes to grids.
    """
    words = []
    for e in entities:
        kind = e.dxftype()
        if kind == "MTEXT" and not e.plain_text().strip():
            continue                    # empty (a table object keeps one per empty cell): neither read nor counted
        budget.tick()
        try:
            if kind in ("TEXT", "ATTRIB"):
                if kind == "ATTRIB" and e.is_invisible:
                    continue
                words += _words_from_text(e, rotation_by_word)
            elif kind == "MTEXT":
                words += _words_from_mtext(e, rotation_by_word, cell_width)
            elif kind == "INSERT":
                block = e.block()
                if block is None:
                    continue
                if block.block_record.is_xref:
                    stats["xref_inserts"] += 1
                    continue
                for a in e.attribs:
                    budget.tick()
                    if not a.is_invisible:
                        words += _words_from_text(a, rotation_by_word)
                if depth < MAX_BLOCK_DEPTH:
                    words += _collect(e.virtual_entities(), budget, stats, rotation_by_word, grids, depth + 1)
                else:
                    stats["too_deep"] += 1
            elif kind == "ACAD_TABLE":
                stats["tables"] += 1
                parts = list(virtual_entities(e))
                edges = _grid_edges(parts)
                cell_rot = {}
                cell_words = _collect(parts, budget, stats, cell_rot, grids, depth + 1, cell_width=bool(edges))
                if edges and not any(cell_rot.values()):      # horizontal table with cell lines: read by its grid
                    grids.append((cell_words, *edges))
                else:                                          # no cell lines, or turned: read by position
                    words += cell_words
                    rotation_by_word.update(cell_rot)
            elif kind == "OLE2FRAME":
                stats["ole"] += 1
            elif kind in ("IMAGE", "PDFUNDERLAY", "DWFUNDERLAY", "DGNUNDERLAY"):
                stats["underlays"] += 1
            elif kind in ("LINE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ARC"):
                stats["lines"] += 1
        except CadReadError:
            raise
        except Exception:
            stats["skipped"] += 1        # one damaged entity must not stop the whole reading
    return words


def _by_rotation(words, rotation_by_word):
    """{rotation in whole degrees: words}; each group is already in the frame where its text reads horizontally."""
    by_rot = {}
    for w in words:
        by_rot.setdefault(round(rotation_by_word[id(w)]) % 360, []).append(w)
    return sorted(by_rot.items(), key=lambda kv: -len(kv[1]))


def _table_kind(title):
    """'single span' / 'continuous span' from a table title such as 'SINGLE SPAN BEAM SCHEDULE', else ''."""
    t = " ".join(title.upper().split())
    if "SINGLE SPAN" in t:
        return "single span"
    if "CONTINUOUS" in t:
        return "continuous span"
    return ""


def _read_doc(data):
    """(document, auditor or None). The plain reader first (about twice as fast on large files); the
    recovering reader only for a binary or damaged DXF the plain reader cannot load."""
    if not data.startswith(b"AutoCAD Binary DXF"):
        try:
            info = dxf_stream_info(io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="ignore"))
            stream = io.TextIOWrapper(io.BytesIO(data), encoding=info.encoding, errors="surrogateescape")
            return ezdxf.read(stream), None
        except Exception:
            pass
    return recover.read(io.BytesIO(data))


def _load(data, limits):
    """Parse the DXF in a worker thread so a slow file cannot hold the app past the time limit.

    Python cannot stop the thread; on timeout it finishes in the background and its result is dropped.
    """
    box = {}

    def work():
        try:
            box["doc"] = _read_doc(data)
        except Exception as e:      # noqa: BLE001 - reported to the user below
            box["error"] = e

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(limits.timeout_s)
    if worker.is_alive():
        raise CadReadError(f"Opening the DXF took longer than {limits.timeout_s} s, the limit for this app. "
                           + TOO_BIG_HINT.format(setting="DXF_TIMEOUT_SECONDS"))
    if "error" in box:
        raise CadReadError("This file could not be read as a DXF (it may be damaged or not a DXF). "
                           "Save it again as DXF from the CAD software and upload it again.")
    return box["doc"]


def read_dxf(data, limits=CadLimits()):
    """Read every schedule table in a DXF file (bytes). Raises CadReadError with a user-safe message."""
    if is_dwg(data):
        raise CadReadError(DWG_MESSAGE)
    size_mb = len(data) / 1e6
    if size_mb > limits.max_mb:
        raise CadReadError(f"The DXF is {size_mb:.0f} MB; the limit for this app is {limits.max_mb} MB. "
                           + TOO_BIG_HINT.format(setting="MAX_DXF_MB"))
    started = time.monotonic()
    doc, auditor = _load(data, limits)
    budget = _Budget(limits, started)
    reading = CadReading()
    stats = dict.fromkeys(("xref_inserts", "too_deep", "tables", "ole", "underlays", "lines", "skipped"), 0)
    groups, any_words = [], False
    for layout_no, name in enumerate(doc.layouts.names_in_taborder(), start=1):
        reading.layouts.append(name)
        rotation_by_word, grids = {}, []
        words = _collect(doc.layouts.get(name), budget, stats, rotation_by_word, grids)
        found = []
        for cell_words, xs, ys in grids:
            table = text_layer.find_grid_table(cell_words, xs, ys)
            if table is not None:
                reading.table_cells += len(cell_words)
                found.append((0.0, table))
            else:                                   # no schedule header in the grid: read its text by position
                rotation_by_word.update((id(w), 0.0) for w in cell_words)
                words += cell_words
        any_words = any_words or bool(words) or bool(found)
        for rot, group in _by_rotation(words, rotation_by_word):
            groups.append(group)
            found += [(rot, table) for table in text_layer.find_tables(group, cad_rules=True)]
        for table_no, (rot, table) in enumerate(found, start=1):
            kind = _table_kind(table.title)
            for i, rec in enumerate(table.records, start=1):
                where = f"{kind} table, row {i}" if kind else f"table row {i}"
                rec["source_note"] = f"CAD text, layout {layout_no}, {where}"
            reading.tables.append(CadTable(layout_no, name, table_no, rot, table, kind))
    reading.entities_read = budget.count

    w = reading.warnings
    n_xref = sum(1 for b in doc.blocks if b.block_record.is_xref)
    if n_xref or stats["xref_inserts"]:
        w.append(f"The file has {n_xref or stats['xref_inserts']} external reference(s) (xrefs). They are not "
                 "followed: a schedule inside an xref is not read. Bind the xref in the CAD software first.")
    if len(doc.objects.query("DATALINK")):
        w.append("The file has data links (e.g. a table linked to a spreadsheet). Linked data is not followed; "
                 "only the text stored in the drawing is read.")
    if stats["ole"]:
        w.append(f"The file has {stats['ole']} embedded object(s) (e.g. a pasted Excel table). Their content "
                 "cannot be read; use Excel mode for an Excel schedule.")
    if stats["underlays"]:
        w.append(f"The file has {stats['underlays']} attached image(s) or PDF/DWF/DGN underlay(s); "
                 "text inside them cannot be read.")
    if stats["too_deep"]:
        w.append(f"{stats['too_deep']} block(s) nested more than {MAX_BLOCK_DEPTH} levels deep were not read.")
    if stats["skipped"]:
        w.append(f"{stats['skipped']} damaged entit(y/ies) were skipped.")
    if auditor is not None and (auditor.has_errors or auditor.has_fixes):
        w.append("The DXF had structural errors; they were repaired on reading. Check the table against the drawing.")
    if not reading.tables:
        if not any_words and stats["lines"]:
            w.append("No text was found, only lines. The schedule text may have been exploded into lines "
                     "(e.g. after a PDF import); such text cannot be read.")
        reading.found_headers = list(dict.fromkeys(
            h for g in groups for h in text_layer.header_report(g, cad_rules=True)))[:20]
    return reading
