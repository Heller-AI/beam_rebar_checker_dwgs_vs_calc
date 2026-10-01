"""Read beam schedule tables from a CAD drawing saved as DXF (no AI, nothing sent anywhere).

A DXF keeps every schedule cell as typed text with its position, so it is read like the PDF
text layer: each text entity becomes a positioned word and text_layer.find_tables() rebuilds
the rows and columns. This module only collects the words:

- TEXT, MTEXT (formatting codes stripped, the cell flagged), attribute text of block references,
  text inside blocks, and CAD table objects (ACAD_TABLE, through their graphical block);
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

from ezdxf import recover
from ezdxf.protocols import virtual_entities
from ezdxf.fonts import fonts
from ezdxf.tools.text_size import get_font_name

from . import text_layer

FORMATTING_FLAG = "formatting_removed"   # underline / overline / strike-through or stacked text removed: review
FONT_FLAG = "font_codes_removed"          # only font, height, colour... codes removed: shown, not highlighted

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

    @property
    def score(self):
        """How well the headers matched: mapped columns first, then rows."""
        return (len(self.table.columns), len(self.table.records))

    def label(self):
        turned = f" · rotated {self.rotation:g}°" if self.rotation else ""
        return (f"Layout '{self.layout_name}' · table {self.table_no} · {len(self.table.records)} rows · "
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


def _text_width(entity, text, height, width_factor=1.0):
    """Text width in drawing units. The DXF does not store it: it is measured with the style's font (or a
    stand-in font when that font is not installed), falling back to an estimate from the text height."""
    try:
        return _font(get_font_name(entity), round(height, 6), round(width_factor or 1.0, 6)).text_width(text)
    except Exception:
        return 0.8 * height * (width_factor or 1.0) * len(text)


# TEXT special codes: %%u / %%o / %%k toggle underline, overline, strike-through (formatting);
# %%c, %%d, %%p are the diameter, degree and plus/minus symbols (content, shown as the symbol).
_TEXT_FORMAT_RE = re.compile(r"%%[uUoOkK]")
# MTEXT inline codes: \S stacks text (fractions, tolerances) and \L \O \K (and lower case) toggle underline,
# overline and strike-through, which can carry meaning; the others (\f font, \H height, \W width, \C colour,
# \Q oblique, \T tracking, \A alignment, \p paragraph, \~ hard space) and grouping braces only change the look.
_MTEXT_CODE_RE = re.compile(r"\\(.)|([{}])")
_MTEXT_REVIEW_CODES = set("SLlOoKk")


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
    flags = (FORMATTING_FLAG,) if _TEXT_FORMAT_RE.search(raw) else ()
    word = text_layer.Word(text, x0, y0, x1, y0 + h, flags)
    rotation_by_word[id(word)] = rot
    return [word]


def _words_from_mtext(e, rotation_by_word):
    """MTEXT -> one word per line, formatting codes stripped (and the cell flagged)."""
    raw = e.text
    lines = [ln.strip() for ln in e.plain_text(split=True, fast=False)]
    if not any(lines):
        return []
    h = e.dxf.get("char_height", 1.0) or 1.0
    rot = e.get_rotation() % 360
    att = e.dxf.get("attachment_point", 1)
    pitch = h * 5 / 3 * (e.dxf.get("line_spacing_factor", 1.0) or 1.0)
    total = h + pitch * (len(lines) - 1)
    ins = e.dxf.get("insert")
    u, v = _rotate(ins.x, ins.y, -rot)
    top = {1: v, 2: v, 3: v, 4: v + total / 2, 5: v + total / 2, 6: v + total / 2}.get(att, v + total)
    flags = _mtext_flags(raw)
    words = []
    for i, line in enumerate(lines):
        if not line:
            continue
        w = _text_width(e, line, h)
        x0 = {2: u - w / 2, 5: u - w / 2, 8: u - w / 2, 3: u - w, 6: u - w, 9: u - w}.get(att, u)
        y1 = top - i * pitch
        word = text_layer.Word(line, x0, y1 - h, x0 + w, y1, flags)
        rotation_by_word[id(word)] = rot
        words.append(word)
    return words


class _Budget:
    """Entity-count and time limits while walking the drawing."""

    def __init__(self, limits, started):
        self.limits, self.deadline, self.count = limits, started + limits.timeout_s, 0

    def tick(self):
        self.count += 1
        if self.count > self.limits.max_entities:
            raise CadReadError(f"The DXF has more than {self.limits.max_entities:,} entities, the limit for this app. "
                               + TOO_BIG_HINT.format(setting="MAX_DXF_ENTITIES"))
        if self.count % 500 == 0 and time.monotonic() > self.deadline:
            raise CadReadError(f"Reading the DXF took longer than {self.limits.timeout_s} s, the limit for this app. "
                               + TOO_BIG_HINT.format(setting="DXF_TIMEOUT_SECONDS"))


def _collect(entities, budget, stats, rotation_by_word, depth=0):
    """Positioned words from a layout or block, expanding block references and table objects."""
    words = []
    for e in entities:
        budget.tick()
        kind = e.dxftype()
        try:
            if kind in ("TEXT", "ATTRIB"):
                if kind == "ATTRIB" and e.is_invisible:
                    continue
                words += _words_from_text(e, rotation_by_word)
            elif kind == "MTEXT":
                words += _words_from_mtext(e, rotation_by_word)
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
                    words += _collect(e.virtual_entities(), budget, stats, rotation_by_word, depth + 1)
                else:
                    stats["too_deep"] += 1
            elif kind == "ACAD_TABLE":
                stats["tables"] += 1
                words += _collect(virtual_entities(e), budget, stats, rotation_by_word, depth + 1)
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


def _load(data, limits):
    """Parse the DXF in a worker thread so a slow file cannot hold the app past the time limit.

    Python cannot stop the thread; on timeout it finishes in the background and its result is dropped.
    """
    box = {}

    def work():
        try:
            box["doc"] = recover.read(io.BytesIO(data))
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
        rotation_by_word = {}
        words = _collect(doc.layouts.get(name), budget, stats, rotation_by_word)
        any_words = any_words or bool(words)
        table_no = 0
        for rot, group in _by_rotation(words, rotation_by_word):
            groups.append(group)
            for table in text_layer.find_tables(group):
                table_no += 1
                for i, rec in enumerate(table.records, start=1):
                    rec["source_note"] = f"CAD text, layout {layout_no}, table row {i}"
                reading.tables.append(CadTable(layout_no, name, table_no, rot, table))

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
    if auditor.has_errors or auditor.has_fixes:
        w.append("The DXF had structural errors; they were repaired on reading. Check the table against the drawing.")
    if not reading.tables:
        if not any_words and stats["lines"]:
            w.append("No text was found, only lines. The schedule text may have been exploded into lines "
                     "(e.g. after a PDF import); such text cannot be read.")
        reading.found_headers = list(dict.fromkeys(h for g in groups for h in text_layer.header_report(g)))[:20]
    return reading
