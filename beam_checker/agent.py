"""AI assistant for the checker results, using Claude with tool use.

Claude never does the steel arithmetic itself: every number it reports comes from
the tools below, which reuse the same parsers as the checker.
"""

import json
import math
from itertools import product

import anthropic
import requests

from .checker import RESULT_COLUMNS
from .parsers import normalize_str, parse_bar_notation, parse_stirrup_single_str

DEFAULT_MODEL = "claude-opus-5-5"
MODELS = {
    "claude-opus-5-5": "Claude Opus 5.5 (best quality)",
    "claude-opus-5": "Claude Opus 5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (about half the cost)",
}
MAX_TOOL_ROUNDS = 10
DEFAULT_MAX_OUTPUT_TOKENS = 16000

BAR_DIAS = [10, 13, 16, 20, 25, 32, 40]
LINK_DIAS = [8, 10, 12, 13, 16]

SYSTEM_PROMPT = """You are a structural engineering assistant inside a beam reinforcement checker.
The app has compared an Excel beam schedule (provided steel) against a Prokon continuous-beam
report (required steel). Each beam span has 3 rows: Left Support (top steel), Mid-Span (bottom
steel) and Right Support (top steel). Each row checks flexure (As, mm²) and shear (Asv/sv, mm²/mm).
Bar notation is nHd (e.g. 3H20 = three 20 mm high-yield bars); stirrups are nHd-s
(legs, diameter, spacing in mm).

How to work:
- Use the tools to look up results. Do not compute steel areas or Asv/sv in your head; use
  evaluate_rebar, evaluate_stirrup, suggest_bars and suggest_stirrups so every number is traceable.
- Quote beam marks, required vs provided values and ratios exactly as the tools return them.
- When suggesting a fix, prefer the smallest change to what is already provided, and mention
  practical limits you cannot check here (bar spacing, number of layers, anchorage, detailing
  rules, min/max steel). Your suggestions are for the engineer to verify, not final design.
- Be concise. Use short tables when listing several beams."""

TOOLS = [
    {
        "name": "get_summary",
        "description": "Overall results: spans checked, OK/FAIL row counts, list of beam marks with any FAIL, and beams missing from either file.",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "list_rows",
        "description": "List result rows, optionally filtered by status and/or check type. Returns at most 60 rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ALL", "FAIL", "OK"], "description": "Filter on Overall Status."},
                "check": {"type": "string", "enum": ["ANY", "FLEXURE", "SHEAR"], "description": "With status FAIL, only rows failing this check."},
            },
            "required": ["status", "check"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_beam_detail",
        "description": "All result rows for one beam. Accepts a span mark ('B101-2') or a base mark ('B101', returns every span). Matching ignores case, spaces and dashes.",
        "input_schema": {
            "type": "object",
            "properties": {"beam_mark": {"type": "string"}},
            "required": ["beam_mark"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "evaluate_rebar",
        "description": "Area of a longitudinal bar arrangement (e.g. '3H20+2H16') and whether it meets a required As in mm².",
        "input_schema": {
            "type": "object",
            "properties": {
                "notation": {"type": "string"},
                "required_as_mm2": {"type": "number"},
            },
            "required": ["notation", "required_as_mm2"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "evaluate_stirrup",
        "description": "Asv/sv of a stirrup notation (e.g. '2H10-150', legs default to 2) and whether it meets a required Asv/sv in mm²/mm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "notation": {"type": "string"},
                "required_asv_sv": {"type": "number"},
            },
            "required": ["notation", "required_asv_sv"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "suggest_bars",
        "description": "Smallest bar arrangements (one or two diameters, up to max_bars in total) whose area is at least the required As. Returns up to 8 options sorted by area.",
        "input_schema": {
            "type": "object",
            "properties": {
                "required_as_mm2": {"type": "number"},
                "max_bars": {"type": "integer", "description": "Maximum total number of bars, e.g. 6 for one layer in a narrow beam."},
            },
            "required": ["required_as_mm2", "max_bars"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "suggest_stirrups",
        "description": "Smallest stirrup arrangements (2-4 legs, spacing 75-300 mm in 25 mm steps) whose Asv/sv is at least the required value. Returns up to 8 options.",
        "input_schema": {
            "type": "object",
            "properties": {
                "required_asv_sv": {"type": "number"},
                "max_spacing_mm": {"type": "integer", "description": "Upper limit on spacing, e.g. from code max-spacing rules."},
            },
            "required": ["required_asv_sv", "max_spacing_mm"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


# ------------------------------------------------------------------ tools

def _rows_as_dicts(rows):
    return [dict(zip(RESULT_COLUMNS, r)) for r in rows]


def _get_summary(result):
    rows = result.rows
    failing = sorted({r[0] for r in rows if r[12] == "FAIL"})
    return {
        "spans_checked": result.matched_count,
        "rows_total": len(rows),
        "rows_fail": sum(r[12] == "FAIL" for r in rows),
        "rows_fail_flexure": sum(r[6] != "OK" for r in rows),
        "rows_fail_shear": sum(r[11] != "OK" for r in rows),
        "beam_spans_with_fail": failing,
        "in_pdf_missing_in_excel": result.pdf_only,
        "in_excel_missing_in_pdf": result.excel_only,
    }


def _list_rows(result, status, check):
    rows = result.rows
    if status != "ALL":
        rows = [r for r in rows if r[12] == status]
    if status == "FAIL" and check == "FLEXURE":
        rows = [r for r in rows if r[6] != "OK"]
    elif status == "FAIL" and check == "SHEAR":
        rows = [r for r in rows if r[11] != "OK"]
    return {"count": len(rows), "rows": _rows_as_dicts(rows[:60]), "truncated": len(rows) > 60}


def _get_beam_detail(result, beam_mark):
    key = normalize_str(beam_mark)
    exact = [r for r in result.rows if normalize_str(r[0]) == key]
    if not exact:
        # Base mark: 'B101' matches 'B101-1', 'B101-2', ...
        exact = [r for r in result.rows if normalize_str(r[0]).startswith(key) and normalize_str(r[0])[len(key):].isdigit()]
    if not exact:
        return {"error": f"No results for beam '{beam_mark}'. Use get_summary to see available marks."}
    return {"rows": _rows_as_dicts(exact)}


def _evaluate_rebar(notation, required_as_mm2):
    area, norm = parse_bar_notation(notation)
    return {
        "notation": norm,
        "area_mm2": area,
        "required_as_mm2": required_as_mm2,
        "ratio_pct": round(area / required_as_mm2 * 100, 1) if required_as_mm2 > 0 else None,
        "ok": area >= required_as_mm2,
    }


def _evaluate_stirrup(notation, required_asv_sv):
    asv, norm = parse_stirrup_single_str(notation)
    return {
        "notation": norm,
        "asv_sv": asv,
        "required_asv_sv": required_asv_sv,
        "ratio_pct": round(asv / required_asv_sv * 100, 1) if required_asv_sv > 0 else None,
        "ok": asv >= required_asv_sv,
    }


def _bar_area(n, d):
    return n * math.pi * d * d / 4.0


def _suggest_bars(required_as_mm2, max_bars):
    max_bars = max(2, min(int(max_bars), 12))
    options = []
    for d in BAR_DIAS:
        for n in range(2, max_bars + 1):
            a = _bar_area(n, d)
            if a >= required_as_mm2:
                options.append((a, f"{n}H{d}"))
                break
    for d1, d2 in product(BAR_DIAS, BAR_DIAS):
        if d2 >= d1:
            continue
        for n1, n2 in product(range(2, max_bars + 1), range(1, max_bars + 1)):
            if n1 + n2 > max_bars:
                continue
            a = _bar_area(n1, d1) + _bar_area(n2, d2)
            if a >= required_as_mm2:
                options.append((a, f"{n1}H{d1}+{n2}H{d2}"))
    options.sort()
    seen, out = set(), []
    for a, s in options:
        if s not in seen:
            seen.add(s)
            out.append({"notation": s, "area_mm2": round(a, 1), "ratio_pct": round(a / required_as_mm2 * 100, 1) if required_as_mm2 > 0 else None})
        if len(out) == 8:
            break
    return {"required_as_mm2": required_as_mm2, "options": out or "No arrangement within max_bars; increase max_bars or use more layers."}


def _suggest_stirrups(required_asv_sv, max_spacing_mm):
    options = []
    for legs, d, s in product((2, 3, 4), LINK_DIAS, range(75, 301, 25)):
        if s > max_spacing_mm:
            continue
        asv = legs * math.pi * d * d / 4.0 / s
        if asv >= required_asv_sv:
            options.append((round(asv, 3), legs, -s, f"{legs}H{d}-{s}"))
    options.sort()
    out = [{"notation": o[3], "asv_sv": o[0]} for o in options[:8]]
    return {"required_asv_sv": required_asv_sv, "options": out or "No arrangement within limits."}


def execute_tool(name, args, result):
    if name == "get_summary":
        return _get_summary(result)
    if name == "list_rows":
        return _list_rows(result, args["status"], args["check"])
    if name == "get_beam_detail":
        return _get_beam_detail(result, args["beam_mark"])
    if name == "evaluate_rebar":
        return _evaluate_rebar(args["notation"], float(args["required_as_mm2"]))
    if name == "evaluate_stirrup":
        return _evaluate_stirrup(args["notation"], float(args["required_asv_sv"]))
    if name == "suggest_bars":
        return _suggest_bars(float(args["required_as_mm2"]), args["max_bars"])
    if name == "suggest_stirrups":
        return _suggest_stirrups(float(args["required_asv_sv"]), args["max_spacing_mm"])
    raise ValueError(f"Unknown tool: {name}")


class ProviderError(Exception):
    """A provider call failed; the message is safe to show to the user."""


def _run_tool_json(name, args, result):
    """Run a tool and return (json_text, is_error)."""
    try:
        return json.dumps(execute_tool(name, args, result), ensure_ascii=False), False
    except Exception as e:
        return f"Error: {e}", True


# ------------------------------------------------------------------ Anthropic (Claude)

def ask(client, messages, result, model=DEFAULT_MODEL, on_tool=None, on_request=None,
        max_tokens=DEFAULT_MAX_OUTPUT_TOKENS):
    """Claude tool loop. `messages` already ends with the user's question and is extended in
    place (assistant turns and tool results) so it can be kept for follow-up questions.
    `on_tool(name, args)` is called before each tool runs. `on_request()` is called before
    each model request and may raise to stop the loop (used for call budgets).
    """
    for _ in range(MAX_TOOL_ROUNDS):
        if on_request:
            on_request()
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            return "The model declined to answer this request. Try rephrasing the question."

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                text += "\n\n_(Answer was cut off; ask a narrower question.)_"
            return text or "(No answer returned.)"

        tool_results = []
        for tu in tool_uses:
            if on_tool:
                on_tool(tu.name, tu.input)
            content, is_error = _run_tool_json(tu.name, tu.input, result)
            block = {"type": "tool_result", "tool_use_id": tu.id, "content": content}
            if is_error:
                block["is_error"] = True
            tool_results.append(block)
        messages.append({"role": "user", "content": tool_results})

    return "Stopped after too many tool calls. Try a more specific question."


def make_client(api_key):
    return anthropic.Anthropic(api_key=api_key)


# ------------------------------------------------------------------ Zhipu (GLM)
# OpenAI-compatible chat/completions endpoint with function calling, called with plain
# `requests` (same approach as the PPVC drawing checker app). GLM's tool_choice only
# supports "auto", which is what this loop needs anyway.

ZHIPU_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_TIMEOUT_S = 180
DEFAULT_ZHIPU_MODEL = "glm-5.3-flash"

ZHIPU_TOOLS = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOLS
]


def ask_zhipu(api_key, messages, result, model=DEFAULT_ZHIPU_MODEL, on_tool=None, on_request=None,
              max_tokens=DEFAULT_MAX_OUTPUT_TOKENS, post=requests.post):
    """GLM tool loop over OpenAI-style `messages` (no system message; it is added per request).
    Same contract as `ask`. `post` is injectable for tests.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for _ in range(MAX_TOOL_ROUNDS):
        if on_request:
            on_request()
        body = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            "tools": ZHIPU_TOOLS,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
        }
        try:
            resp = post(ZHIPU_ENDPOINT, headers=headers, json=body, timeout=ZHIPU_TIMEOUT_S)
        except requests.RequestException as e:
            raise ProviderError(f"Could not reach the Zhipu API: {e}") from e
        if resp.status_code == 401:
            raise ProviderError("The Zhipu API key was rejected. Check ZHIPU_API_KEY.")
        if resp.status_code == 429:
            raise ProviderError("Zhipu rate limit or balance limit reached. Wait a minute or check your account balance.")
        if resp.status_code >= 400:
            raise ProviderError(f"Zhipu API error ({resp.status_code}): {resp.text[:300]}")

        data = resp.json()
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"Unexpected Zhipu response: {str(data)[:300]}") from e

        tool_calls = message.get("tool_calls") or []
        assistant_turn = {"role": "assistant", "content": message.get("content") or ""}
        if tool_calls:
            assistant_turn["tool_calls"] = tool_calls
        messages.append(assistant_turn)

        if not tool_calls:
            text = (message.get("content") or "").strip()
            if choice.get("finish_reason") == "length":
                text += "\n\n_(Answer was cut off; ask a narrower question.)_"
            return text or "(No answer returned.)"

        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                content = "Error: tool arguments were not valid JSON."
            else:
                if on_tool:
                    on_tool(name, args)
                content, _ = _run_tool_json(name, args, result)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": content})

    return "Stopped after too many tool calls. Try a more specific question."
