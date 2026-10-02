"""Read beam schedule drawings (PDF or PNG/JPG pages) with Claude vision into ScheduleRecords.

No Streamlit code here. The flow is:
    pages = load_pages(files)                 # render PDF pages / open images, plus PDF text layer
    estimate_cost(pages, model)               # show before calling the API
    extraction = extract_drawing(pages, ...)  # one tool-use conversation per page
    df = extraction.table                     # the user reviews and edits this table
    records = table_to_records(df)            # then the existing comparison runs on them

If a PDF page keeps the schedule as positioned text (typical for CAD exports), it is read
directly from the text layer instead (text_layer.py): exact, free, and nothing is sent anywhere.
A CAD drawing saved as DXF is read the same way (cad_reader.py, then read_cad below).

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

from . import plausibility, text_layer
from .checker import ScheduleRecord, match_span
from .parsers import (ASSUMED_LINK, clean_suffix, is_arrow_symbol, mark_key, normalize_str, parse_bar_notation,
                      parse_stirrup_single_str)
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
READ_CAD = "CAD text"
NO_AI_METHODS = (READ_TEXT, READ_CAD)   # the drawing's own text, copied exactly
# set by the DXF reader (symbol_unknown also by the PDF text layer), never offered to the AI
CAD_ONLY_FLAGS = {"formatting_removed", "font_codes_removed", "symbol_unknown"}

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
        "flags": {"type": "array",
                  "items": {"type": "string", "enum": [f for f in FLAG_DESCRIPTIONS if f not in CAD_ONLY_FLAGS]}},
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
        result["note"] = f"legs not stated; the checker assumes {ASSUMED_LINK.legs}"
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


def extract_page(send, page, model, system_prompt, on_request=None, max_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                 on_response=None):
    """Run the extraction conversation for one page. `send(**kwargs)` returns a Message.

    `on_request()` runs before each model request (it may raise to stop); `on_response()` runs once a request
    has returned a response, so a request that failed with an error is not counted there.
    """
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
            if on_response:
                on_response()                   # the model did answer (and it is billed), only unreadably
            bad_json_retries += 1
            if bad_json_retries > 1:
                raise ExtractionError("The model's output could not be read twice in a row. Try again.")
            continue

        if on_response:
            on_response()
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
    page_records: list = field(default_factory=list)   # [(page number, record, meta)] as read, before checks
    stopped: Exception = None                   # error that stopped an AI reading early (pages read are kept)


def _flags_text(flags):
    return ", ".join(dict.fromkeys(flags))


# End columns where an arrow means "same as the middle column". An arrow there drawn with a known symbol font
# (a Wingdings 3 cell in a DXF or in a PDF's text layer) is the drawing's own symbol, read exactly: it is not
# flagged ditto_unconfirmed.
# Arrows read by AI vision, typed as ordinary text, in a middle column or in an unknown font keep the flag.
EXACT_ARROW_FIELDS = {"T1", "T3", "B1", "B3", "S1", "S3"}


def _is_ditto(value):
    """An arrow or ditto mark (a plain dash is just a dash, not 'same as previous')."""
    v = (value or "").strip()
    return is_arrow_symbol(v) and v not in BLANKS


def _add_checks(rows):
    """Deterministic checks after extraction: notation, symbols, conflicts, continuity, duplicates."""
    out, seen = [], set()
    for rec in rows:
        key = (mark_key(rec["beam_mark"]),) + tuple((rec[f] or "").replace(" ", "").upper() for f in RECORD_FIELDS[1:])
        if key in seen:      # identical row seen twice (e.g. in two overlapping tiles or sheets)
            continue
        seen.add(key)
        flags = list(rec["flags"])
        if record_problems(rec):
            flags.append("notation_invalid")
        exact = set(rec.get("symbol_fields", ())) & EXACT_ARROW_FIELDS
        if any(_is_ditto(rec[f]) for f in BAR_FIELDS if f not in exact) and "ditto_assumed" not in flags:
            flags.append("ditto_unconfirmed")
        if any((rec[f] or "").strip() and not rec[f].strip()[0].isdigit() and check_notation(rec[f], "stirrup").get("asv_sv")
               for f in STIRRUP_FIELDS):
            flags.append("legs_not_stated")
        if "/" in (rec["size"] or ""):
            flags.append("tapered_size")
        if plausibility.row_issues(rec):
            flags.append("possible_typo")
        rec = {**rec, "flags": flags}
        if "notation_invalid" in flags or "unreadable" in flags:
            rec["confidence"] = "low"
        out.append(rec)

    by_mark = {}
    for rec in out:
        by_mark.setdefault(mark_key(rec["beam_mark"]), []).append(rec)
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


# Flags that are a genuine concern and highlight the row. The others (arrows read as written, stirrup legs not
# stated, cantilever end, tapered size, CAD font codes removed...) stay visible in the Flags column but do not
# highlight on their own, so the highlight keeps its meaning on a long AI-read table. formatting_removed: a CAD
# cell lost underline / strike-through or stacked text, which can carry meaning.
CONCERN_FLAGS = {"notation_invalid", "unreadable", "possible_typo", "conflict", "continuity_mismatch",
                 "formatting_removed", "symbol_unknown"}


def _flag_set(flags_text):
    return {f.strip() for f in str(flags_text or "").split(",") if f.strip()}


def is_assumed_link(link_type):
    """The row's link type is the one whose leg count is assumed (parsers.ASSUMED_LINK), e.g. 'A1'."""
    return str(link_type or "").strip().upper() == ASSUMED_LINK.link_type.upper()


def _other_link_type(link_type):
    """A link type is written and it is not the assumed one (e.g. A2): its leg count cannot be assumed."""
    lt = str(link_type or "").strip()
    return bool(lt) and lt.lower() != "nan" and not is_assumed_link(lt)


def needs_review(confidence, flags_text, link_type=None):
    """A row needs extra care: low confidence or a concern flag (medium confidence alone does not).

    legs_not_stated is a concern only when a link type is written and is not the assumed one (ASSUMED_LINK), e.g.
    A2 without a leg count. With the assumed type (A1) or no link type the flag stays in the Flags column only;
    the review table states the A1 assumption once (assumed_legs_note).
    """
    flags = _flag_set(flags_text)
    if "legs_not_stated" in flags and _other_link_type(link_type):
        return True
    return confidence == "low" or bool(flags & CONCERN_FLAGS)


def assumed_legs_note(table):
    """One note for the review table when stirrups of the assumed link type have no leg count, else ''."""
    n = sum(1 for f, lt in zip(table["Flags"], table["Link type"])
            if "legs_not_stated" in _flag_set(f) and is_assumed_link(lt))
    if not n:
        return ""
    return (f"{n} row{'s' if n != 1 else ''}: link type {ASSUMED_LINK.link_type} has no leg count; "
            f"{ASSUMED_LINK.legs} legs assumed. Confirm against the drawing legend.")


def prokon_concerns(table, prokon_beams):
    """{Row ID: note} for rows whose span has no Prokon result, another Prokon span used, or zero requirement."""
    out = {}
    for _, r in table.iterrows():
        mark = _cell(r["Beam mark"])
        if mark:
            m = match_span(mark, prokon_beams)
            if m.note:
                out[_cell(r["Row ID"])] = m.note
    return out


def build_table(page_records):
    """[(page number, record dict[, meta])] -> (review table, boxes). Rows needing review come first.

    meta: {"read": READ_TEXT / READ_CAD / READ_VISION, "box": row box in px or None, "header": header box in px or None,
           "position": CAD position {"layout_no", "layout", "x", "y"} (optional)}
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
        elif r["_meta"].get("position"):
            boxes[row_id] = {"page": r["_page"], **r["_meta"]["position"]}
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


def tick_status(table):
    """(ticked, required) for rows that must be ticked before the comparison: rows read by AI vision.

    Text-layer rows are the drawing's own text, copied exactly, and hand-added rows were typed by the
    reviewer, so ticking those is optional.
    """
    required = table["Read from"].fillna("") == READ_VISION
    ticked = table["Reviewed"].fillna(False).astype(bool)
    return int((ticked & required).sum()), int(required.sum())


def ready_to_compare(table):
    """True when the table has rows and every AI-read row is ticked."""
    done, required = tick_status(table)
    return bool(len(table)) and done == required


CONFIRM_LABEL = "I have compared this table with the drawing"


def concern_rows(table, prokon_notes=None, conflicts=()):
    """Bool per row: a genuine concern (highlighted), a Prokon concern, or a duplicate mark.

    Duplicates come from `conflicts` (the table as it is now), not from the 'conflict' flag set at reading time,
    so a row stops being a concern once its duplicate has been deleted or renamed.
    """
    notes, dup = prokon_notes or {}, {mark_key(m) for m in conflicts}

    def live(flags):
        return ", ".join(f for f in str(flags or "").split(", ") if f.strip() != "conflict")

    return pd.Series([needs_review(_cell(c), live(_cell(f)), _cell(lt)) or _cell(i) in notes
                      or mark_key(_cell(m)) in dup
                      for c, f, lt, i, m in zip(table["Confidence"], table["Flags"], table["Link type"], table["Row ID"],
                                                table["Beam mark"])],
                     index=table.index, dtype=bool)


def tick_unflagged(table, concerns):
    """Tick every row without a concern; rows with a concern keep their tick state (each needs its own tick)."""
    out = table.copy()
    out.loc[~concerns, "Reviewed"] = True
    return out


def run_blockers(table, confirmed, has_prokon, conflicts=(), concerns=None, pages_ack=True):
    """Why the comparison cannot run yet: [reason]. Empty when it can.

    Rows read by AI vision must each be ticked, and the whole table confirmed once; text-layer rows and rows
    typed by the reviewer need neither.
    """
    reasons = []
    if not len(table):
        reasons.append("The table is empty.")
    if not has_prokon:
        reasons.append("Upload the Prokon report.")
    if conflicts:
        reasons.append(f"{len(conflicts)} beam mark(s) appear more than once (red rows): {', '.join(conflicts)}. "
                       "Keep one row per span (delete or rename the others).")
    ai = table["Read from"].fillna("") == READ_VISION
    unticked = ai & ~table["Reviewed"].fillna(False).astype(bool)
    if unticked.any():
        flagged = int((unticked & concerns).sum()) if concerns is not None else 0
        reasons.append(f"{int(unticked.sum())} of {int(ai.sum())} AI-read row(s) not ticked"
                       + (f", {flagged} of them highlighted: tick each one after checking it against the drawing"
                          if flagged else "") + ". AI vision can misread a value.")
    if ai.any() and not confirmed:
        reasons.append(f"Tick \"{CONFIRM_LABEL}\" (needed when rows were read by AI vision).")
    if not pages_ack:
        reasons.append("Confirm that you continue without the page(s) that could not be read.")
    return reasons


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


def read_cad(cad_tables):
    """Review table from the chosen DXF schedule tables (cad_reader.CadTable; no AI). None if there are no rows.

    Page is the layout number; the layout name and drawing coordinates of each row are kept in `boxes`,
    not in the table or the Excel download (layout names often carry drawing numbers).
    """
    page_records, notes = [], {}
    for t in cad_tables:
        for rec in t.table.records:
            page_records.append((t.layout_no, rec, {"read": READ_CAD, "position": t.position(rec)}))
        msgs = list(t.table.notes)
        if t.table.unmapped_headers:
            msgs.append("columns not used: " + ", ".join(t.table.unmapped_headers))
        if msgs:
            notes[t.layout_no] = "; ".join(filter(None, [notes.get(t.layout_no), f"table {t.table_no}: "
                                                         + "; ".join(msgs)]))
    if not page_records:
        return None
    table, boxes = build_table(page_records)
    return DrawingExtraction(table, notes, {}, 0, boxes, READ_CAD)


def text_layer_summary(pages):
    """{page number: number of schedule rows found in its text layer}."""
    return {p.number: sum(len(t.records) for t in p.tables) for p in pages if p.tables}


POSITIONS = (("Left", "T1", "B1", "S1"), ("Middle", "T2", "B2", "S2"), ("Right", "T3", "B3", "S3"))


def beam_detail(row, checks=None):
    """One beam span as a small readable table: Left / Middle / Right with the drawing's top bars,
    bottom bars and stirrups, and (if `checks` are given: the checker's 3 result rows for the span)
    the Prokon requirement, what the checker counted as provided, and OK/FAIL.

    The checker compares top bars at the supports and bottom bars at mid-span; "Checked" says which.
    """
    def status(text):
        return "FAIL" if str(text).startswith("FAIL") else str(text)

    out = []
    for i, (pos, t, b, st) in enumerate(POSITIONS):
        rec = {"Position": pos}
        if checks is not None:
            rec["Result"] = checks[i][12]
        rec.update({"Top bars": _cell(row[t]), "Bottom bars": _cell(row[b]), "Stirrups": _cell(row[st])})
        if checks is not None:
            c = checks[i]
            rec.update({
                "Checked": "bottom" if pos == "Middle" else "top",
                "As req → prov (mm²)": f"{c[2]} → {c[4]}", "Flexure": status(c[6]),
                "Asv/sv req → prov": f"{c[7]} → {c[9]}", "Shear": status(c[11]),
            })
        out.append(rec)
    return pd.DataFrame(out)


def typo_rows(table):
    """[(beam mark, table row, issues)] for rows whose bar counts look like typos, from the table as it is
    now (so a value corrected during review is no longer reported). Highlight only."""
    out = []
    for _, row in table.iterrows():
        mark = _cell(row["Beam mark"])
        if not mark:
            continue
        issues = plausibility.row_issues({k: _cell(row[k]) for k in ("Size", *BAR_FIELDS)})
        if issues:
            out.append((mark, row, issues))
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
    table["Review"] = [("⚠ possible typo" if "possible_typo" in str(f) else "⚠ check") if needs_review(c, f, lt) else ""
                       for c, f, lt in zip(table["Confidence"], table["Flags"], table["Link type"])]
    order = (table["Review"] == "").astype(int)  # rows to check first, otherwise keep reading order
    return table.assign(_o=order).sort_values("_o", kind="stable").drop(columns="_o").reset_index(drop=True)


STOPPED_NOTE = "not read: the reading stopped before this page"


def extract_drawing(pages, send, model, extra_rules="", on_request=None, on_progress=None,
                    max_tokens=DEFAULT_MAX_OUTPUT_TOKENS, on_response=None, stop_on=(), previous=None):
    """Extract every page. A failed page is reported in page_errors; the others still return.

    Errors raised by `on_request` or `send` stop the whole run. If they are of a type in `stop_on` (e.g. a budget
    limit or an API error), the pages already read are kept and returned, with the error in `stopped`, so a retry
    does not pay for them again; otherwise the error is raised.
    `previous`: an earlier reading of the same files; only its failed pages are read again, the others are kept.
    """
    system_prompt = extraction_system_prompt(extra_rules)
    notes, errors = {}, {}
    page_records, requests = [], 0
    if previous is not None:
        keep = {p.number for p in pages} - set(previous.page_errors)
        page_records = [r for r in previous.page_records if r[0] in keep]
        notes = {n: t for n, t in previous.page_notes.items() if n in keep}
        requests = previous.requests
        pages = [p for p in pages if p.number in previous.page_errors]
    stopped = None
    for i, page in enumerate(pages):
        if on_progress:
            on_progress(i / len(pages), f"Reading page {page.number} of {len(pages)}...")
        if stopped is not None:
            errors[page.number] = STOPPED_NOTE
            continue
        try:
            res = extract_page(send, page, model, system_prompt, on_request, max_tokens, on_response)
        except ExtractionError as e:
            errors[page.number] = str(e)
            continue
        except stop_on as e:
            stopped = e
            errors[page.number] = STOPPED_NOTE
            continue
        requests += res.requests
        page_records += [(page.number, r, {"read": READ_VISION, "box": _pct_box_to_px(page, r["row_box"])})
                         for r in res.records]
        if res.note:
            notes[page.number] = res.note
    if on_progress:
        on_progress(1.0, "Done")
    page_records.sort(key=lambda r: r[0])
    table, boxes = build_table(page_records)
    return DrawingExtraction(table, notes, errors, requests, boxes, READ_VISION, page_records, stopped)


# ------------------------------------------------------------------ after review

def _cell(v):
    return "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v).strip()


def table_conflicts(table):
    """Beam marks that still appear more than once (the reviewer must keep one row per span)."""
    marks = [mark_key(_cell(m)) for m in table["Beam mark"]]
    dup = {m for m in marks if m and marks.count(m) > 1}
    return sorted({_cell(m) for m in table["Beam mark"] if mark_key(_cell(m)) in dup})


def reading_order(table):
    """The table in the drawing's reading order (Row IDs R1, R2, ... are assigned when the drawing is read).

    The review table shows rows to check first; results and exports must not depend on that, so they
    use this order. Rows added by hand (no Row ID) keep their place after the read rows.
    """
    if "Row ID" not in table.columns or table.empty:
        return table

    def key(v):
        v = _cell(v)
        return int(v[1:]) if v[:1] == "R" and v[1:].isdigit() else 10**9

    return table.assign(_k=[key(v) for v in table["Row ID"]]).sort_values("_k", kind="stable").drop(columns="_k")


def table_to_records(table):
    """Reviewed table -> ScheduleRecords for run_comparison_records, in reading order. B3 is not used."""
    records = []
    for _, r in reading_order(table).iterrows():
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


# ------------------------------------------------------------------ converter and assistant context

SCHEDULE_SHEET = "BEAM SCHEDULE"
SOURCE_SHEET = "Source (not read)"


def table_to_type2_excel(table):
    """The reviewed schedule as an Excel file in the Type 2 layout, cell text copied verbatim.

    Sheet 1 (read by Excel mode): mark in column B, size C, top bars E-G (T1-T3), bottom bars H-J
    (B1-B3), link type K, stirrups L-N (S1-S3), side bars O, remark P. One row per span.
    Sheet 2: page, reading method, confidence, flags and review tick per row (not read by Excel mode).
    """
    from .checker import EXCEL_FORMATS

    c = EXCEL_FORMATS["Format 2"]
    cols = {"Beam mark": c["mark"], "Size": 2, "T1": c["t1"], "T2": c["t2"], "T3": c["t3"], "B1": c["b1"],
            "B2": c["b2"], "B3": 9, "Link type": 10, "S1": c["st_l"], "S2": c["st_m"], "S3": c["st_r"],
            "Side bars": 14, "Remark": 15}
    width = 16
    header = [None] * width
    for name, idx in cols.items():
        header[idx] = "MARK" if name == "Beam mark" else name.upper()
    rows = [header]
    table = reading_order(table)
    for _, r in table.iterrows():
        if not _cell(r["Beam mark"]):
            continue
        row = [None] * width
        for name, idx in cols.items():
            v = r[name]
            row[idx] = None if v is None or (isinstance(v, float) and math.isnan(v)) or v == "" else str(v)
        rows.append(row)

    source_cols = ["Beam mark", "Page", "Read from", "Confidence", "Flags", "Source note", "Reviewed"]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name=SCHEDULE_SHEET, header=False, index=False)
        table[[c for c in source_cols if c in table.columns]].to_excel(writer, sheet_name=SOURCE_SHEET, index=False)
    return buf.getvalue()


def schedule_summary(table, method, pdf_only, excel_only, n_prokon, n_found, notes=None):
    """What the AI assistant may know about a drawing schedule (no drawing content, only row metadata)."""
    rows = []
    for _, r in table.iterrows():
        mark = _cell(r["Beam mark"])
        if mark:
            rows.append({"mark": mark, "page": _cell(r["Page"]), "read_from": _cell(r["Read from"]),
                         "confidence": _cell(r["Confidence"]), "flags": _cell(r["Flags"]),
                         "needed_extra_care": needs_review(_cell(r["Confidence"]), _cell(r["Flags"]),
                                                           _cell(r["Link type"]))})
    return {
        "schedule_source": "drawing",
        "reading_method": method,
        "ai_used_for_reading": method not in NO_AI_METHODS,
        **({"page_means": "layout number in the DXF file"} if method == READ_CAD else {}),
        "note": "The drawing may be older than the calculation; differences are discrepancies to double-check.",
        "coverage": f"found {n_found} of {n_prokon} Prokon beam marks on the drawing",
        "prokon_beams_not_on_drawing": list(pdf_only),
        "drawing_beams_not_in_prokon": list(excel_only),
        "rows_needing_extra_care": [r for r in rows if r["needed_extra_care"]],
        "possible_typos": [{"mark": mark, "page": _cell(row["Page"]),
                            "issues": [plausibility.describe(i) for i in issues]}
                           for mark, row, issues in typo_rows(table)],
        "rows": [{k: r[k] for k in ("mark", "page", "read_from")} for r in rows],
        "reader_notes": notes or {},
    }


def files_fingerprint(files, model, extra_rules=""):
    """Cache key for an extraction: file contents + model + private rules."""
    h = hashlib.sha256()
    for _, data in files:
        h.update(hashlib.sha256(data).digest())
    h.update(model.encode())
    h.update(hashlib.sha256((extra_rules or "").encode()).digest())
    return h.hexdigest()
