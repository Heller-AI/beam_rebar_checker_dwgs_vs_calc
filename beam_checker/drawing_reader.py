"""Read beam schedule drawings (PDF or PNG/JPG pages) with Claude vision into ScheduleRecords.

No Streamlit code here. The flow is:
    pages = load_pages(files)                 # render PDF pages / open images, plus PDF text layer
    estimate_cost(pages, model)               # show before calling the API
    extraction = extract_drawing(pages, ...)  # one tool-use conversation per page
    df = extraction.table                     # the user reviews and edits this table
    records = table_to_records(df)            # then the existing comparison runs on them

If a PDF page keeps the schedule as positioned text (typical for CAD exports), it is read
directly from the text layer instead (text_layer.py): exact, free, and nothing is sent anywhere.

The model never decides PASS/FAIL: it only transcribes the schedule, every string is checked
with parsers.py, and the comparison runs only after the user has reviewed and ticked every row.
"""

import base64
import hashlib
import io
import json
import math
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd
import pypdf
import pypdfium2 as pdfium
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import text_layer
from .checker import ScheduleRecord
from .parsers import clean_suffix, is_arrow_symbol, normalize_str, parse_bar_notation, parse_stirrup_single_str
from .prompts import FLAG_DESCRIPTIONS, extraction_system_prompt

# ------------------------------------------------------------------ limits and prices

DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_OUTPUT_TOKENS = 32000
EFFORT = "medium"
MAX_ROUNDS = 6                    # model requests per page, including one nudge and one resubmission

MAX_IMAGE_EDGE = 2576             # current Sonnet/Opus vision limit, long edge in px
MAX_IMAGE_PIXELS = 3_500_000      # stay under the ~4,784-token cap so images are not downscaled again
TARGET_PX_PER_MM = 6.0            # 2.5 mm schedule text -> about 15 px high
TILE_PX = 1850                    # square tiles of about 3.4 MP
TILE_OVERLAP = 0.08
MAX_RENDER_PIXELS = 40_000_000    # memory guard when rendering very large sheets
TEXT_LAYER_MAX_CHARS = 40_000

# USD per million tokens: input, output, 5-minute cache write, cache read
# (platform.claude.com pricing page, checked 2026-10-01)
MODEL_PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 6.25, 0.50),
}
SYSTEM_AND_TOOLS_TOKENS = 3000

DRAWING_REMARKS = ("From drawing T1 / S1", "From drawing B1-B2 / S2", "From drawing T3 / S3")
SOURCE_LABEL = "AI-read drawing vs Prokon"
READ_TEXT = "PDF text layer"
READ_VISION = "AI vision"

BAR_FIELDS = ("T1", "T2", "T3", "B1", "B2", "B3")
STIRRUP_FIELDS = ("S1", "S2", "S3")
RECORD_FIELDS = ("beam_mark", "size", *BAR_FIELDS, "side_bars", "link_type", *STIRRUP_FIELDS, "remark")

TABLE_COLUMNS = [
    "Reviewed", "Review", "Page", "Read from", "Beam mark", "Size", *BAR_FIELDS, "Link type", *STIRRUP_FIELDS,
    "Side bars", "Remark", "Confidence", "Flags", "Source note", "Row ID",
]
LOCKED_COLUMNS = ["Review", "Page", "Read from", "Row ID"]  # not editable; used for highlighting


class ExtractionError(Exception):
    """A page could not be extracted; the message is safe to show to the user."""


# ------------------------------------------------------------------ pages and images

@dataclass
class Page:
    number: int          # 1-based, across all uploaded files
    source: str          # e.g. "file 1, page 2" (no file names, they may carry project names)
    image: Image.Image   # grayscale, full resolution
    text: str = ""       # PDF text layer ("" for images)
    width_mm: float = 0.0
    height_mm: float = 0.0
    tables: list = field(default_factory=list)   # schedule tables found in the text layer
    scale: float = 1.0   # image pixels per PDF point
    height_pt: float = 0.0


def count_pages(files):
    """Number of pages in uploaded files [(name, bytes)] without rendering anything."""
    total = 0
    for name, data in files:
        if name.lower().endswith(".pdf"):
            total += len(pdfium.PdfDocument(data))
        else:
            total += 1
    return total


def load_pages(files, max_pages=DEFAULT_MAX_PAGES):
    """Render PDF pages / open images. `files` is a list of (file name, bytes)."""
    n = count_pages(files)
    if n > max_pages:
        raise ValueError(f"The upload has {n} pages; the limit is {max_pages}. Upload only the schedule sheets.")
    pages = []
    for f_idx, (name, data) in enumerate(files, start=1):
        if name.lower().endswith(".pdf"):
            doc = pdfium.PdfDocument(data)
            reader = pypdf.PdfReader(io.BytesIO(data))
            for p_idx in range(len(doc)):
                pdf_page = doc[p_idx]
                w_pt, h_pt = pdf_page.get_size()
                scale = TARGET_PX_PER_MM * 25.4 / 72
                if (w_pt * scale) * (h_pt * scale) > MAX_RENDER_PIXELS:
                    scale = math.sqrt(MAX_RENDER_PIXELS / (w_pt * h_pt))
                img = pdf_page.render(scale=scale).to_pil().convert("L")
                text = reader.pages[p_idx].extract_text() or ""
                tables = text_layer.find_tables(text_layer.page_words(pdf_page))
                pages.append(Page(len(pages) + 1, f"file {f_idx}, page {p_idx + 1}", img, text,
                                  w_pt / 72 * 25.4, h_pt / 72 * 25.4, tables, scale, h_pt))
        else:
            img = Image.open(io.BytesIO(data))
            img.load()
            pages.append(Page(len(pages) + 1, f"file {f_idx}", img.convert("L")))
    return pages


def fit_image(img, max_edge=MAX_IMAGE_EDGE, max_pixels=MAX_IMAGE_PIXELS):
    """Downscale (never upscale) to the vision limits."""
    w, h = img.size
    s = min(1.0, max_edge / max(w, h), math.sqrt(max_pixels / (w * h)))
    if s >= 1.0:
        return img
    return img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)


def _tile_starts(length, tile, overlap):
    if length <= tile:
        return [0]
    n = math.ceil((length - tile) / (tile * (1 - overlap))) + 1
    step = (length - tile) / (n - 1)
    return [round(i * step) for i in range(n)]


def plan_images(page):
    """[(label, image)]: an overview of the whole page, then overlapping full-resolution tiles."""
    w, h = page.image.size
    images = [("Overview of the whole page", fit_image(page.image))]
    if w * h <= MAX_IMAGE_PIXELS and max(w, h) <= MAX_IMAGE_EDGE:
        return images  # the overview already is full resolution
    xs, ys = _tile_starts(w, TILE_PX, TILE_OVERLAP), _tile_starts(h, TILE_PX, TILE_OVERLAP)
    for r, y in enumerate(ys, start=1):
        for c, x in enumerate(xs, start=1):
            box = (x, y, min(x + TILE_PX, w), min(y + TILE_PX, h))
            label = (f"Tile row {r} of {len(ys)}, column {c} of {len(xs)} "
                     f"(x {100 * box[0] / w:.0f}-{100 * box[2] / w:.0f}%, y {100 * box[1] / h:.0f}-{100 * box[3] / h:.0f}%)")
            images.append((label, fit_image(page.image.crop(box))))
    return images


def image_tokens(img):
    w, h = img.size
    return min(4784, math.ceil(w * h / 750))


def _encode(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    if buf.tell() <= 4_500_000:
        return "image/png", buf.getvalue()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return "image/jpeg", buf.getvalue()


def page_content(page):
    """User-message content blocks for one page: labelled images, then the text layer."""
    blocks = [{"type": "text", "text": f"Drawing page {page.number} ({page.source})."}]
    for label, img in plan_images(page):
        media_type, data = _encode(img)
        blocks.append({"type": "text", "text": label})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                   "data": base64.standard_b64encode(data).decode("ascii")}})
    text = page.text.strip()
    if text:
        blocks.append({"type": "text", "text": "PDF text layer of this page (may be incomplete or out of order):\n"
                                               + text[:TEXT_LAYER_MAX_CHARS]})
    else:
        blocks.append({"type": "text", "text": "This page has no text layer; read everything from the images."})
    blocks.append({"type": "text", "text": "Extract every beam schedule row on this page."})
    return blocks


def estimate_cost(pages, model):
    """Rough cost range in USD before calling the API (None if the model's price is unknown)."""
    images = sum(len(plan_images(p)) for p in pages)
    input_tokens = sum(SYSTEM_AND_TOOLS_TOKENS + sum(image_tokens(i) for _, i in plan_images(p))
                       + len(p.text[:TEXT_LAYER_MAX_CHARS]) // 3 for p in pages)
    est = {"pages": len(pages), "images": images, "input_tokens": input_tokens, "low": None, "high": None}
    prices = MODEL_PRICES.get(model)
    if prices:
        p_in, p_out, p_write, p_read = (x / 1e6 for x in prices)
        # first request writes the cache; later requests read it; output includes thinking
        low = input_tokens * p_write + input_tokens * p_read * 1 + len(pages) * 6000 * p_out
        high = input_tokens * p_write + input_tokens * p_read * 4 + len(pages) * 20000 * p_out
        est.update(low=low, high=high)
    return est


# ------------------------------------------------------------------ tool schemas

class ExtractedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beam_mark: str = Field(min_length=1)
    size: str
    T1: str
    T2: str
    T3: str
    B1: str
    B2: str
    B3: str
    side_bars: str
    link_type: str
    S1: str
    S2: str
    S3: str
    remark: str
    confidence: Literal["high", "medium", "low"]
    flags: list[str]
    source_note: str
    row_box: list[float]

    @field_validator("row_box")
    @classmethod
    def _box(cls, v):
        if v and (len(v) != 4 or not all(0 <= x <= 100 for x in v)):
            raise ValueError("row_box must be [] or [left, top, right, bottom] in percent (0-100)")
        return v


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[ExtractedRecord]
    page_note: str


class NotationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    kind: Literal["bar", "stirrup"]


class NotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[NotationItem]


def _string(desc):
    return {"type": "string", "description": desc}


RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "beam_mark": _string("Beam mark exactly as written, including suffixes like a, -1"),
        "size": _string("Width x depth in mm as written, e.g. 200x450 or 200x225/175"),
        **{f: _string(f"Top bars {f} as written, '' if blank") for f in ("T1", "T2", "T3")},
        **{f: _string(f"Bottom bars {f} as written, '' if blank") for f in ("B1", "B2", "B3")},
        "side_bars": _string("Side bars as written, '' if none"),
        "link_type": _string("Link type code, e.g. A1"),
        **{f: _string(f"Stirrups zone {f} as full notation, e.g. 2H10-150") for f in STIRRUP_FIELDS},
        "remark": _string("Remark as written"),
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "flags": {"type": "array", "items": {"type": "string", "enum": list(FLAG_DESCRIPTIONS)}},
        "source_note": _string("Where the row is on the page"),
        "row_box": {"type": "array", "items": {"type": "number"},
                    "description": "Approximate box of this row on the page, in percent of the overview image: "
                                   "[left, top, right, bottom]. [] if unsure."},
    },
    "required": list(ExtractedRecord.model_fields),
    "additionalProperties": False,
}

EXTRACTION_TOOLS = [
    {
        "name": "validate_notation",
        "description": "Check bar strings (e.g. 3H20+2H16) and stirrup strings (e.g. 2H10-150) with the app's own "
                       "parser. Returns for each string whether it parses, and the area or Asv/sv it gives.",
        "input_schema": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "kind": {"type": "string", "enum": ["bar", "stirrup"]}},
                "required": ["text", "kind"], "additionalProperties": False}}},
            "required": ["items"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
    {
        "name": "submit_records",
        "description": "Submit every beam schedule record on this page, once, after validating the notation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "records": {"type": "array", "items": RECORD_SCHEMA},
                "page_note": _string("Anything the reviewer should know about this page, '' if nothing"),
            },
            "required": ["records", "page_note"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
]


# ------------------------------------------------------------------ notation checks

BLANKS = ("", "-", "—")


def check_notation(text, kind):
    """Validate one string with the checker's own parsers."""
    t = (text or "").strip()
    if t in BLANKS:
        return {"text": text, "ok": True, "note": "blank"}
    if is_arrow_symbol(t):
        return {"text": text, "ok": True, "note": "symbol kept as written (read by the checker as 'same as T2')"}
    if kind == "bar":
        area, norm = parse_bar_notation(t)
        ok = area > 0 and len(norm.split("+")) == len([p for p in t.split("+") if p.strip()])
        return {"text": text, "ok": ok, "area_mm2": area, "parsed_as": norm} if ok else \
            {"text": text, "ok": False, "note": "not valid bar notation (expected e.g. 3H20 or 2H16+2H13)"}
    if "+" in t:
        return {"text": text, "ok": False, "note": "two stirrup sets in one zone are not supported; report as written"}
    asv, norm = parse_stirrup_single_str(t)
    if asv <= 0:
        return {"text": text, "ok": False, "note": "not valid stirrup notation (expected e.g. 2H10-150)"}
    result = {"text": text, "ok": True, "asv_sv": asv}
    if not t[0].isdigit():
        result["note"] = "legs not stated; the checker assumes 2"
    return result


def record_problems(rec):
    """Fields of an extracted record whose notation does not parse."""
    bad = [f for f in BAR_FIELDS if not check_notation(rec[f], "bar")["ok"]]
    bad += [f for f in STIRRUP_FIELDS if not check_notation(rec[f], "stirrup")["ok"]]
    return bad


# ------------------------------------------------------------------ one page

@dataclass
class PageExtraction:
    page: int
    records: list = field(default_factory=list)
    note: str = ""
    error: str = ""
    requests: int = 0


def stream_send(client):
    """Send a request with streaming (long outputs) and return the final message."""
    def send(**kwargs):
        with client.beta.messages.stream(**kwargs) as stream:
            return stream.get_final_message()
    return send


def _tool_result(tool_use_id, payload, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)}
    if is_error:
        block["is_error"] = True
    return block


def extract_page(send, page, model, system_prompt, on_request=None, max_tokens=DEFAULT_MAX_OUTPUT_TOKENS):
    """Run the extraction conversation for one page. `send(**kwargs)` returns a Message."""
    messages = [{"role": "user", "content": page_content(page)}]
    result = PageExtraction(page.number)
    submission, nudged, bad_json_retries = None, False, 0

    for _ in range(MAX_ROUNDS):
        if on_request:
            on_request()
        result.requests += 1
        try:
            msg = send(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
                tools=EXTRACTION_TOOLS,
                messages=messages,
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except ValueError:
            # The SDK could not parse a streamed tool input at all: re-issue the same request once
            bad_json_retries += 1
            if bad_json_retries > 1:
                raise ExtractionError("The model's output could not be read twice in a row. Try again.")
            continue

        if msg.stop_reason == "refusal":
            raise ExtractionError("The model declined to process this page.")
        if msg.stop_reason == "max_tokens":
            raise ExtractionError("The page has more rows than fit in one answer. Raise DRAWING_MAX_OUTPUT_TOKENS "
                                  "or upload the schedule tables as separate, smaller images.")
        messages.append({"role": "assistant", "content": msg.content})

        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        if not tool_uses:
            if submission is not None:
                break
            if nudged:
                raise ExtractionError("The model did not submit any records for this page.")
            nudged = True
            messages.append({"role": "user", "content": "Call submit_records with the beams on this page "
                                                        "(an empty list if the page has no beam schedule)."})
            continue

        results, finished = [], False
        for tu in tool_uses:
            if tu.name == "validate_notation":
                try:
                    req = NotationRequest.model_validate(tu.input)
                except ValidationError as e:
                    results.append(_tool_result(tu.id, {"INVALID_INPUT": str(e)[:1500]}, True))
                    continue
                results.append(_tool_result(tu.id, [check_notation(i.text, i.kind) for i in req.items]))
            elif tu.name == "submit_records":
                try:
                    sub = Submission.model_validate(tu.input)
                except ValidationError as e:
                    results.append(_tool_result(tu.id, {"INVALID_INPUT": str(e)[:1500],
                                                        "hint": "Fix the fields and call submit_records again."}, True))
                    continue
                submission = sub
                problems = {r.beam_mark: record_problems(r.model_dump()) for r in sub.records}
                problems = {k: v for k, v in problems.items() if v}
                if not problems or result.requests >= MAX_ROUNDS - 1:
                    finished = True
                    results.append(_tool_result(tu.id, "Recorded."))
                else:
                    results.append(_tool_result(tu.id, {
                        "recorded": len(sub.records),
                        "fields_that_do_not_parse": problems,
                        "next": "Look at these cells again. If you misread them, call submit_records again with ALL "
                                "beams corrected. If they are written that way, reply 'done' and they will be flagged.",
                    }))
            else:
                results.append(_tool_result(tu.id, f"Unknown tool {tu.name}", True))
        if finished:
            break
        messages.append({"role": "user", "content": results})

    if submission is None:
        raise ExtractionError("No records were submitted for this page.")
    result.records = [r.model_dump() for r in submission.records]
    result.note = submission.page_note
    return result


# ------------------------------------------------------------------ whole drawing

@dataclass
class DrawingExtraction:
    table: pd.DataFrame
    page_notes: dict = field(default_factory=dict)
    page_errors: dict = field(default_factory=dict)
    requests: int = 0
    boxes: dict = field(default_factory=dict)   # Row ID -> {"page", "row", "header"} boxes in image pixels
    method: str = READ_VISION


def _flags_text(flags):
    return ", ".join(dict.fromkeys(flags))


def _is_ditto(value):
    """An arrow or ditto mark (a plain dash is just a dash, not 'same as previous')."""
    v = (value or "").strip()
    return is_arrow_symbol(v) and v not in BLANKS


def _add_checks(rows):
    """Deterministic checks after extraction: notation, symbols, conflicts, continuity, duplicates."""
    out, seen = [], set()
    for rec in rows:
        key = (normalize_str(rec["beam_mark"]),) + tuple((rec[f] or "").replace(" ", "").upper() for f in RECORD_FIELDS[1:])
        if key in seen:      # identical row seen twice (e.g. in two overlapping tiles or sheets)
            continue
        seen.add(key)
        flags = list(rec["flags"])
        if record_problems(rec):
            flags.append("notation_invalid")
        if any(_is_ditto(rec[f]) for f in BAR_FIELDS) and "ditto_assumed" not in flags:
            flags.append("ditto_unconfirmed")
        if any((rec[f] or "").strip() and not rec[f].strip()[0].isdigit() and check_notation(rec[f], "stirrup").get("asv_sv")
               for f in STIRRUP_FIELDS):
            flags.append("legs_not_stated")
        if "/" in (rec["size"] or ""):
            flags.append("tapered_size")
        rec = {**rec, "flags": flags}
        if "notation_invalid" in flags or "unreadable" in flags:
            rec["confidence"] = "low"
        out.append(rec)

    by_mark = {}
    for rec in out:
        by_mark.setdefault(normalize_str(rec["beam_mark"]), []).append(rec)
    for recs in by_mark.values():
        if len(recs) > 1:
            for rec in recs:
                rec["flags"].append("conflict")

    by_base = {}
    for rec in out:
        base, span = clean_suffix(rec["beam_mark"])
        if base != rec["beam_mark"].strip():
            by_base.setdefault(normalize_str(base), {})[span] = rec
    for spans in by_base.values():
        for n in spans:
            a, b = spans.get(n), spans.get(n + 1)
            if a and b:
                x, y = (a["T3"] or "").replace(" ", "").upper(), (b["T1"] or "").replace(" ", "").upper()
                if x and y and x not in BLANKS and y not in BLANKS and not is_arrow_symbol(x) and not is_arrow_symbol(y) and x != y:
                    a["flags"].append("continuity_mismatch")
                    b["flags"].append("continuity_mismatch")
    for rec in out:
        rec["flags"] = list(dict.fromkeys(rec["flags"]))
    return out


INFO_FLAGS = {"tapered_size"}  # shown, but do not on their own put a row on the review list


def needs_review(confidence, flags_text):
    flags = {f.strip() for f in str(flags_text or "").split(",") if f.strip()}
    return confidence != "high" or bool(flags - INFO_FLAGS)


def build_table(page_records):
    """[(page number, record dict[, meta])] -> (review table, boxes). Rows needing review come first.

    meta: {"read": READ_TEXT or READ_VISION, "box": row box in px or None, "header": header box in px or None}
    """
    items = []
    for item in page_records:
        page_no, rec, meta = item if len(item) == 3 else (*item, {})
        items.append({**rec, "_page": page_no, "_meta": {"read": READ_VISION, "box": None, "header": None, **meta}})
    rows = _add_checks(items)
    boxes, out = {}, []
    for i, r in enumerate(rows, start=1):
        row_id = f"R{i}"
        if r["_meta"]["box"]:
            boxes[row_id] = {"page": r["_page"], "row": r["_meta"]["box"], "header": r["_meta"]["header"]}
        out.append({
            "Reviewed": False, "Review": "", "Page": r["_page"], "Read from": r["_meta"]["read"],
            "Beam mark": r["beam_mark"], "Size": r["size"], **{f: r[f] for f in BAR_FIELDS},
            "Link type": r["link_type"], **{f: r[f] for f in STIRRUP_FIELDS}, "Side bars": r["side_bars"],
            "Remark": r["remark"], "Confidence": r["confidence"], "Flags": _flags_text(r["flags"]),
            "Source note": r["source_note"], "Row ID": row_id,
        })
    return refresh_review_column(pd.DataFrame(out, columns=TABLE_COLUMNS)), boxes


def records_to_table(page_records):
    """Review table only (see build_table)."""
    return build_table(page_records)[0]


def all_reviewed(table):
    """True when every row has its Reviewed tick (the comparison runs only then)."""
    return bool(len(table)) and bool(table["Reviewed"].fillna(False).astype(bool).all())


def _pt_box_to_px(page, box):
    x0, y0, x1, y1 = box
    s, h = page.scale, page.height_pt
    return (x0 * s, (h - y1) * s, x1 * s, (h - y0) * s)


def _pct_box_to_px(page, box):
    if not box or len(box) != 4:
        return None
    w, h = page.image.size
    return (box[0] / 100 * w, box[1] / 100 * h, box[2] / 100 * w, box[3] / 100 * h)


def read_text_layer(pages):
    """Read every schedule table found in the PDF text layer (no AI). None if there is none."""
    page_records, notes = [], {}
    for page in pages:
        for t_idx, table in enumerate(page.tables, start=1):
            header = _pt_box_to_px(page, table.header_box)
            for rec in table.records:
                page_records.append((page.number, rec, {"read": READ_TEXT, "box": _pt_box_to_px(page, rec["row_box"]),
                                                        "header": header}))
            msgs = list(table.notes)
            if table.unmapped_headers:
                msgs.append("columns not used: " + ", ".join(table.unmapped_headers))
            if msgs:
                notes[page.number] = f"table {t_idx}: " + "; ".join(msgs)
    if not page_records:
        return None
    table, boxes = build_table(page_records)
    return DrawingExtraction(table, notes, {}, 0, boxes, READ_TEXT)


def text_layer_summary(pages):
    """{page number: number of schedule rows found in its text layer}."""
    return {p.number: sum(len(t.records) for t in p.tables) for p in pages if p.tables}


def crop_row(page, box, header=None, pad=6, max_width=1600):
    """The drawing region of one schedule row, with the table header above it if known.

    Kept at full resolution so the text stays legible; a wide row is wrapped into stacked parts
    of at most max_width pixels (header and row stay aligned in each part).
    """
    w, h = page.image.size

    def grab(b):
        x0, y0, x1, y1 = b
        return page.image.crop((max(0, int(x0) - pad), max(0, int(y0) - pad), min(w, int(x1) + pad), min(h, int(y1) + pad)))

    row = grab(box)
    if header:
        head = grab((box[0], header[1], box[2], header[3]))   # same x-range as the row
        combined = Image.new("L", (row.width, head.height + 4 + row.height), 160)
        combined.paste(head, (0, 0))
        combined.paste(row, (0, head.height + 4))
    else:
        combined = row
    if combined.width <= max_width:
        return combined
    n = math.ceil(combined.width / max_width)
    part_w = math.ceil(combined.width / n)
    parts = [combined.crop((i * part_w, 0, min(combined.width, (i + 1) * part_w), combined.height)) for i in range(n)]
    out = Image.new("L", (part_w, n * combined.height + (n - 1) * 14), 255)
    for i, part in enumerate(parts):
        out.paste(part, (0, i * (combined.height + 14)))
    return out


def coverage(table, prokon_marks):
    """How many Prokon beam marks appear on the drawing (matched by base mark, like the checker)."""
    def base(m):
        return normalize_str(clean_suffix(_cell(m))[0])

    drawing = {base(m) for m in table["Beam mark"] if _cell(m)}
    prokon = {normalize_str(m): m for m in prokon_marks}
    found = sorted(m for k, m in prokon.items() if k in drawing)
    missing = sorted(m for k, m in prokon.items() if k not in drawing)
    extra = sorted({_cell(m) for m in table["Beam mark"] if _cell(m) and base(m) not in prokon})
    return {"found": found, "missing": missing, "drawing_only": extra, "total": len(prokon)}


def refresh_review_column(table):
    """Recompute the Review marker and put rows to check first (reading order otherwise kept)."""
    table = table.copy()
    table["Review"] = ["⚠ check" if needs_review(c, f) else "" for c, f in zip(table["Confidence"], table["Flags"])]
    order = (table["Review"] == "").astype(int)  # rows to check first, otherwise keep reading order
    return table.assign(_o=order).sort_values("_o", kind="stable").drop(columns="_o").reset_index(drop=True)


def extract_drawing(pages, send, model, extra_rules="", on_request=None, on_progress=None,
                    max_tokens=DEFAULT_MAX_OUTPUT_TOKENS):
    """Extract every page. A failed page is reported in page_errors; the others still return.

    Budget errors raised by `on_request` stop the whole run (they are not page errors).
    """
    system_prompt = extraction_system_prompt(extra_rules)
    page_records, notes, errors, requests = [], {}, {}, 0
    for i, page in enumerate(pages):
        if on_progress:
            on_progress(i / len(pages), f"Reading page {page.number} of {len(pages)}...")
        try:
            res = extract_page(send, page, model, system_prompt, on_request, max_tokens)
        except ExtractionError as e:
            errors[page.number] = str(e)
            continue
        requests += res.requests
        page_records += [(page.number, r, {"read": READ_VISION, "box": _pct_box_to_px(page, r["row_box"])})
                         for r in res.records]
        if res.note:
            notes[page.number] = res.note
    if on_progress:
        on_progress(1.0, "Done")
    table, boxes = build_table(page_records)
    return DrawingExtraction(table, notes, errors, requests, boxes, READ_VISION)


# ------------------------------------------------------------------ after review

def _cell(v):
    return "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v).strip()


def table_conflicts(table):
    """Beam marks that still appear more than once (the reviewer must keep one row per span)."""
    marks = [normalize_str(_cell(m)) for m in table["Beam mark"]]
    dup = {m for m in marks if m and marks.count(m) > 1}
    return sorted({_cell(m) for m in table["Beam mark"] if normalize_str(_cell(m)) in dup})


def table_to_records(table):
    """Reviewed table -> ScheduleRecords for run_comparison_records. B3 is not used by the checker."""
    records = []
    for _, r in table.iterrows():
        mark = _cell(r["Beam mark"])
        if not mark:
            continue
        row = {"t1": _cell(r["T1"]), "t2": _cell(r["T2"]), "t3": _cell(r["T3"]),
               "b1": _cell(r["B1"]), "b2": _cell(r["B2"]),
               "st_l": _cell(r["S1"]), "st_m": _cell(r["S2"]), "st_r": _cell(r["S3"])}
        flags = [f.strip() for f in _cell(r["Flags"]).split(",") if f.strip()]
        records.append(ScheduleRecord(mark=mark, rows=[row], size=_cell(r["Size"]), remark=_cell(r["Remark"]),
                                      source=f"page {_cell(r['Page'])}", confidence=_cell(r["Confidence"]), flags=flags))
    return records


def files_fingerprint(files, model, extra_rules=""):
    """Cache key for an extraction: file contents + model + private rules."""
    h = hashlib.sha256()
    for _, data in files:
        h.update(hashlib.sha256(data).digest())
    h.update(model.encode())
    h.update(hashlib.sha256((extra_rules or "").encode()).digest())
    return h.hexdigest()
