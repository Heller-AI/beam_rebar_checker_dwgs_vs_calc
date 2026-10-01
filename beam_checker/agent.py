"""AI assistant for the checker results, using Claude with tool use.

Claude never does the steel arithmetic itself: every number it reports comes from
the tools below, which reuse the same parsers as the checker.
"""

import json
import re
from dataclasses import dataclass, field

import anthropic
import requests

from . import fixes
from .access import BudgetExceeded
from .checker import RESULT_COLUMNS
from .parsers import normalize_str, parse_bar_notation, parse_stirrup_single_str
from .prompts import ASSISTANT_SYSTEM_PROMPT

# Model IDs verified against the Models API. Prices per million input/output tokens
# (platform.claude.com pricing page): Sonnet 5.5 $2/$10, Opus 5.5 $4/$20, Opus 5 $5/$25.
DEFAULT_MODEL = "claude-sonnet-5-5"
MODELS = {
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (default, lowest cost)",
    "claude-opus-5-5": "Claude Opus 5.5 (higher quality, about 2x the cost)",
    "claude-opus-5": "Claude Opus 5 (higher quality, about 2.5x the cost)",
}
MAX_TOOL_ROUNDS = 6               # model calls per question at most
DEFAULT_MAX_OUTPUT_TOKENS = 16000
CHAT_EFFORT = "low"               # lookups: the tables are computed in code, so little reasoning is needed

SYSTEM_PROMPT = ASSISTANT_SYSTEM_PROMPT

TOOLS = [
    {
        "name": "get_failures",
        "description": "Every FAIL in one call: one row per failing check (flexure and shear on separate rows, one "
                       "unit each) with required, provided, shortfall and provided/required (%), plus counts to quote "
                       "as facts. with_fixes=true adds the smallest bar or stirrup change per row, computed by the app "
                       "and checked against the beam width when it is known. Use this for any question about all "
                       "failures, shortfalls or fixes instead of calling other tools per row.",
        "input_schema": {
            "type": "object",
            "properties": {"with_fixes": {"type": "boolean"}},
            "required": ["with_fixes"],
            "additionalProperties": False,
        },
        "strict": True,
    },
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
        "description": "For one what-if question: the smallest bar arrangement (at most 2 layers) whose area is at "
                       "least the required As, checked against the beam width. For all FAIL rows use get_failures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "required_as_mm2": {"type": "number"},
                "beam_width_mm": {"type": "number", "description": "Beam width in mm; 0 if unknown."},
            },
            "required": ["required_as_mm2", "beam_width_mm"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "suggest_stirrups",
        "description": "For one what-if question: the smallest stirrup arrangement (2-4 legs, spacing 75 mm up to "
                       "max_spacing_mm) whose Asv/sv is at least the required value; legs limited by the beam width. "
                       "For all FAIL rows use get_failures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "required_asv_sv": {"type": "number"},
                "max_spacing_mm": {"type": "integer", "description": "Upper limit on spacing, e.g. from code max-spacing rules."},
                "beam_width_mm": {"type": "number", "description": "Beam width in mm; 0 if unknown."},
            },
            "required": ["required_asv_sv", "max_spacing_mm", "beam_width_mm"],
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


def _suggest_bars(required_as_mm2, beam_width_mm):
    width = beam_width_mm or None
    return {"required_as_mm2": required_as_mm2, "beam_width_mm": width, **fixes.suggest_bars(required_as_mm2, width)}


def _suggest_stirrups(required_asv_sv, max_spacing_mm, beam_width_mm):
    width = beam_width_mm or None
    return {"required_asv_sv": required_asv_sv, "beam_width_mm": width,
            **fixes.suggest_stirrups(required_asv_sv, width, max_spacing_mm)}


SCHEDULE_TOOL = {
    "name": "get_schedule_source",
    "description": "How the provided-steel schedule was obtained from the drawing (PDF text layer or AI vision), "
                   "coverage of Prokon beam marks, beams only on the drawing or only in Prokon, the page of each "
                   "row, and the rows that were uncertain or flagged during review.",
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    "strict": True,
}


def tools_for(schedule):
    """Claude tool list: the schedule-source tool is offered only when the schedule came from a drawing."""
    return TOOLS + [SCHEDULE_TOOL] if schedule else TOOLS


def zhipu_tools_for(schedule):
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools_for(schedule)]


def execute_tool(name, args, result, schedule=None, widths=None):
    if name == "get_failures":
        return fixes.failures_table(result, bool(args.get("with_fixes")), widths)
    if name == "get_schedule_source":
        if not schedule:
            return {"error": "The schedule came from an Excel file; there is no drawing source information."}
        return schedule
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
        return _suggest_bars(float(args["required_as_mm2"]), float(args.get("beam_width_mm") or 0))
    if name == "suggest_stirrups":
        return _suggest_stirrups(float(args["required_asv_sv"]), int(args.get("max_spacing_mm") or 300),
                                 float(args.get("beam_width_mm") or 0))
    raise ValueError(f"Unknown tool: {name}")


class ProviderError(Exception):
    """A provider call failed; the message is safe to show to the user."""


STOP_TEXT = {
    "max_tokens": "The answer was cut off: the model reached the output limit (MAX_OUTPUT_TOKENS).",
    "call_cap": "The AI call limit for this session or day was reached before the answer was finished.",
    "round_limit": f"The assistant stopped after {MAX_TOOL_ROUNDS} model calls without finishing.",
    "refusal": "The model declined to answer this request. Try rephrasing the question.",
}


@dataclass
class AskResult:
    """One answered question. `tables` holds every get_failures result computed while answering, so the
    app can show them even when the text is cut off or a limit stops the loop."""
    text: str
    stop: str = "end_turn"          # end_turn, max_tokens, refusal, call_cap or round_limit
    requests: int = 0
    tables: list = field(default_factory=list)

    @property
    def stop_message(self):
        return STOP_TEXT.get(self.stop, "")


def _keep_table(name, content, is_error, tables):
    if name == "get_failures" and not is_error:
        try:
            tables.append(json.loads(content))
        except ValueError:
            pass


# Numbers that are part of a bar/stirrup notation, beam mark or beam size are not "values" to trace
_NOTATION = re.compile(r"\d*\s*H\s*\d+(?:\.\d+)?(?:\s*[-/]\s*\d+)?", re.IGNORECASE)
_MARK = re.compile(r"\b[A-Za-z]+\d+[A-Za-z]?(?:-\d+)?\b")
_SIZE = re.compile(r"\b\d+\s*[xX×]\s*\d+(?:/\d+)?\b")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?(?![\w])")


def _numbers(text):
    return [m.group(0).replace(",", "") for m in _NUMBER.finditer(text)]


def _tool_texts(messages):
    for m in messages:
        content = m.get("content")
        if m.get("role") == "tool" and isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    yield str(block.get("content", ""))


def untraceable_numbers(answer, messages, question=""):
    """Numbers in an answer that do not match any tool result in the conversation (rounded to the
    answer's precision). Small whole numbers (0-10) are ignored, as are digits inside bar notation,
    beam marks and sizes. Used to warn, never to block."""
    known = []
    for text in [*_tool_texts(messages), question]:
        for n in _numbers(text):
            try:
                known.append(float(n))
            except ValueError:
                pass
    cleaned = _SIZE.sub(" ", _MARK.sub(" ", _NOTATION.sub(" ", answer)))
    missing = []
    for token in _numbers(cleaned):
        value = float(token)
        if value.is_integer() and 0 <= value <= 10:
            continue
        decimals = len(token.split(".")[1]) if "." in token else 0
        if not any(abs(round(k, decimals) - value) < 1e-9 or abs(k - value) < 1e-9 for k in known):
            missing.append(token)
    return list(dict.fromkeys(missing))


def _run_tool_json(name, args, result, schedule=None, widths=None):
    """Run a tool and return (json_text, is_error)."""
    try:
        return json.dumps(execute_tool(name, args, result, schedule, widths), ensure_ascii=False), False
    except Exception as e:
        return f"Error: {e}", True


# ------------------------------------------------------------------ Anthropic (Claude)

def ask(client, messages, result, model=DEFAULT_MODEL, on_tool=None, on_request=None,
        max_tokens=DEFAULT_MAX_OUTPUT_TOKENS, schedule=None, widths=None):
    """Claude tool loop. `messages` already ends with the user's question and is extended in
    place (assistant turns and tool results) so it can be kept for follow-up questions.
    `on_tool(name, args)` is called before each tool runs. `on_request()` is called before
    each model request and may raise BudgetExceeded (call caps); the loop then stops and returns
    what was computed. `schedule` is the drawing-source summary offered through
    get_schedule_source (None for Excel schedules); `widths` maps beam marks to widths in mm.
    Returns an AskResult.
    """
    out = AskResult(text="")
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            if on_request:
                on_request()
        except BudgetExceeded as e:
            out.stop, out.text = "call_cap", str(e)
            return out
        out.requests += 1
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=tools_for(schedule),
            messages=messages,
            output_config={"effort": CHAT_EFFORT},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            out.stop, out.text = "refusal", STOP_TEXT["refusal"]
            return out

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            out.text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            out.stop = "max_tokens" if response.stop_reason == "max_tokens" else "end_turn"
            if not out.text and out.stop == "end_turn":
                out.text = "(No answer text returned.)"
            return out
        if response.stop_reason == "max_tokens":         # cut off in the middle of a tool call
            out.stop = "max_tokens"
            return out

        tool_results = []
        for tu in tool_uses:
            if on_tool:
                on_tool(tu.name, tu.input)
            content, is_error = _run_tool_json(tu.name, tu.input, result, schedule, widths)
            _keep_table(tu.name, content, is_error, out.tables)
            block = {"type": "tool_result", "tool_use_id": tu.id, "content": content}
            if is_error:
                block["is_error"] = True
            tool_results.append(block)
        messages.append({"role": "user", "content": tool_results})

    out.stop = "round_limit"
    return out


def make_client(api_key):
    return anthropic.Anthropic(api_key=api_key)


def describe_anthropic_error(exc, model):
    """A short, user-facing message for an Anthropic SDK error (never includes the key)."""
    if isinstance(exc, anthropic.NotFoundError):
        return (f"The model '{model}' is unknown or has been retired. "
                "Choose another model in the sidebar, or fix ANTHROPIC_MODEL in the settings.")
    if isinstance(exc, anthropic.AuthenticationError):
        return "The Anthropic API key was rejected. Check the key, or sign in again."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"This API key is not allowed to use the model '{model}'. Choose another model."
    if isinstance(exc, anthropic.RateLimitError):
        return "Anthropic rate limit reached. Wait a minute and try again."
    if isinstance(exc, anthropic.APITimeoutError):
        return "The Anthropic API took too long to answer. Try again or ask a narrower question."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API. Check the internet connection."
    if isinstance(exc, anthropic.BadRequestError):
        return (f"The request was rejected for model '{model}'. If you set a custom model, it may not "
                f"support the features this app uses; pick one from the list. Details: {exc.message}")
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error ({exc.status_code}): {exc.message}"
    return f"Anthropic API error: {exc}"


# ------------------------------------------------------------------ Zhipu (GLM)
# OpenAI-compatible chat/completions endpoint with function calling, called with plain
# `requests`, so no extra SDK is needed. GLM's tool_choice only
# supports "auto", which is what this loop needs anyway.

ZHIPU_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_TIMEOUT_S = 180
DEFAULT_ZHIPU_MODEL = "glm-5.3-flash"

def ask_zhipu(api_key, messages, result, model=DEFAULT_ZHIPU_MODEL, on_tool=None, on_request=None,
              max_tokens=DEFAULT_MAX_OUTPUT_TOKENS, post=requests.post, schedule=None, widths=None):
    """GLM tool loop over OpenAI-style `messages` (no system message; it is added per request).
    Same contract as `ask` (returns an AskResult). `post` is injectable for tests.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    out = AskResult(text="")

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            if on_request:
                on_request()
        except BudgetExceeded as e:
            out.stop, out.text = "call_cap", str(e)
            return out
        out.requests += 1
        body = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            "tools": zhipu_tools_for(schedule),
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
            out.text = (message.get("content") or "").strip()
            out.stop = "max_tokens" if choice.get("finish_reason") == "length" else "end_turn"
            if not out.text and out.stop == "end_turn":
                out.text = "(No answer text returned.)"
            return out

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
                content, is_error = _run_tool_json(name, args, result, schedule, widths)
                _keep_table(name, content, is_error, out.tables)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": content})

    out.stop = "round_limit"
    return out
