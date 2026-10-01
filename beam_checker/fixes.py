"""Failure tables and fix suggestions for the AI assistant, computed in code.

The assistant only presents these tables: every required/provided/shortfall number and every
suggested arrangement comes from here, not from the model's arithmetic. Suggestions are a starting
point for an engineer; bar spacing rules, anchorage, laps and detailing are not checked.
"""

import math
import re
from itertools import product

from .parsers import normalize_str
from .plausibility import bars_per_layer

BAR_DIAS = (13, 16, 20, 25, 32, 40)   # main bars; H10 is not suggested as main reinforcement
LINK_DIAS = (8, 10, 12, 13, 16)
LINK_SPACINGS = tuple(range(75, 301, 25))
MAX_LAYERS = 2                 # a suggestion may use at most this many layers of bars
LEG_BAR_DIA = 16               # legs must engage longitudinal bars: legs <= bars of this size per layer
MAX_BARS_NO_WIDTH = 12         # search bound when the beam width is unknown

FIT_OK = "fits the width"
FIT_UNKNOWN = "width unknown, fit not checked"
FIT_NO = "does not fit, needs engineer"
NO_OPTION = "no valid option found, needs engineer"

FLEX_UNIT = "mm²"
SHEAR_UNIT = "Asv/sv (mm²/mm)"
POSITIONS = {"Left Support (Pos Start)": "Left", "Mid-Span (Max Bot)": "Middle", "Right Support (Pos End)": "Right"}


def _num(text):
    try:
        return float(str(text).replace("%", ""))
    except ValueError:
        return 0.0


def _bar_area(n, d):
    return n * math.pi * d * d / 4.0


def _pct(provided, required):
    return round(provided / required * 100, 1) if required > 0 else None


# ------------------------------------------------------------------ failures

def failure_rows(result):
    """One row per failing check (flexure and shear separately, each with one unit)."""
    rows = []
    for r in result.rows:
        position = POSITIONS.get(r[1], r[1])
        if r[6] != "OK":
            req, prov = _num(r[2]), _num(r[4])
            rows.append({"beam": r[0], "position": position, "check": "Flexure", "unit": FLEX_UNIT,
                         "provided_as": r[3], "required": round(req, 1), "provided": round(prov, 1),
                         "shortfall": round(req - prov, 1), "provided_over_required_pct": _pct(prov, req)})
        if r[11] != "OK":
            req, prov = _num(r[7]), _num(r[9])
            rows.append({"beam": r[0], "position": position, "check": "Shear", "unit": SHEAR_UNIT,
                         "provided_as": r[8], "required": round(req, 3), "provided": round(prov, 3),
                         "shortfall": round(req - prov, 3), "provided_over_required_pct": _pct(prov, req)})
    return rows


def failure_counts(result):
    """Counts computed in code, plus one sentence the assistant can quote as a fact."""
    flex = [r for r in result.rows if r[6] != "OK"]
    shear = [r for r in result.rows if r[11] != "OK"]
    both = [r for r in result.rows if r[6] != "OK" and r[11] != "OK"]
    any_fail = [r for r in result.rows if r[12] == "FAIL"]
    spans = sorted({r[0] for r in any_fail})
    counts = {"position_rows_with_fail": len(any_fail), "rows_fail_flexure": len(flex), "rows_fail_shear": len(shear),
              "rows_fail_both": len(both), "spans_with_fail": len(spans), "position_rows_checked": len(result.rows)}
    counts["fact"] = (f"{len(any_fail)} of {len(result.rows)} position rows fail in {len(spans)} span(s): "
                      f"{len(flex)} fail flexure, {len(shear)} fail shear, {len(both)} fail both.")
    return counts


# ------------------------------------------------------------------ suggestions

def _largest_dia(notation):
    dias = [float(m) for m in re.findall(r"[Hh]\s*(\d+(?:\.\d+)?)", str(notation or ""))]
    return max(dias) if dias else 0


def suggest_bars(required, width=None, current=""):
    """Smallest bar arrangement with area >= required, in at most MAX_LAYERS layers.

    Each '+' group is one layer of one diameter; with a known width each layer must fit it. To keep
    suggestions buildable, a second layer uses the same diameter as the first or one size smaller,
    with no more bars than the first; ties prefer fewer layers, then fewer bars. When the current
    bars are given, no diameter smaller than the largest one already provided is suggested (a fix
    adds bars or steps up a size; it does not downsize).
    Returns {"suggestion", "area", "fit"}; suggestion is None when no option exists.
    """
    min_dia = _largest_dia(current)
    dias = [d for d in BAR_DIAS if d >= min_dia] or [BAR_DIAS[-1]]

    def per_layer(d):
        return bars_per_layer(width, d) if width else MAX_BARS_NO_WIDTH

    def options(limit_by_width):
        out = []
        for d in dias:
            top = per_layer(d) if limit_by_width else MAX_BARS_NO_WIDTH
            for n in range(2, top + 1):
                out.append((_bar_area(n, d), n, f"{n}H{d}"))
        if MAX_LAYERS >= 2:
            for i, d1 in enumerate(dias):
                for d2 in {d1, dias[i - 1] if i else d1}:          # same size or one size smaller
                    t1 = per_layer(d1) if limit_by_width else MAX_BARS_NO_WIDTH
                    t2 = per_layer(d2) if limit_by_width else MAX_BARS_NO_WIDTH
                    for n1, n2 in product(range(2, t1 + 1), range(2, t2 + 1)):
                        if n2 <= n1:
                            out.append((_bar_area(n1, d1) + _bar_area(n2, d2), n1 + n2, f"{n1}H{d1}+{n2}H{d2}"))
        return out

    def best(opts):
        ok = [o for o in opts if o[0] >= required]
        # smallest area; ties (within 0.5 %) go to one layer, then to fewer bars
        if not ok:
            return None
        smallest = min(o[0] for o in ok)
        near = [o for o in ok if o[0] <= smallest * 1.005]
        return min(near, key=lambda o: ("+" in o[2], o[1], o[0]))

    if width:
        pick = best(options(True))
        if pick:
            return {"suggestion": pick[2], "area": round(pick[0], 1), "fit": FIT_OK}
        pick = best(options(False))
        if pick:
            return {"suggestion": pick[2], "area": round(pick[0], 1), "fit": FIT_NO}
        return {"suggestion": None, "area": None, "fit": NO_OPTION}
    pick = best(options(False))
    if pick:
        return {"suggestion": pick[2], "area": round(pick[0], 1), "fit": FIT_UNKNOWN}
    return {"suggestion": None, "area": None, "fit": NO_OPTION}


def suggest_stirrups(required, width=None, max_spacing=300):
    """Smallest stirrup arrangement with Asv/sv >= required (2-4 legs, spacing 75 mm to max_spacing).

    With a known width the number of legs is limited to the bars that fit in one layer.
    """
    max_legs = min(4, bars_per_layer(width, LEG_BAR_DIA)) if width else 4

    def best(leg_limit):
        ok = []
        spacings = [x for x in LINK_SPACINGS if x <= max_spacing]
        for legs, d, s in product(range(2, leg_limit + 1), LINK_DIAS, spacings):
            asv = legs * math.pi * d * d / 4.0 / s
            if asv >= required:
                ok.append((round(asv, 6), legs, -s, f"{legs}H{d}-{s}", asv))
        return min(ok) if ok else None

    pick = best(max_legs)
    if pick:
        return {"suggestion": pick[3], "asv_sv": round(pick[4], 3), "fit": FIT_OK if width else FIT_UNKNOWN}
    if width and max_legs < 4:
        pick = best(4)
        if pick:
            return {"suggestion": pick[3], "asv_sv": round(pick[4], 3), "fit": FIT_NO}
    return {"suggestion": None, "asv_sv": None, "fit": NO_OPTION}


def width_for(mark, widths):
    """Beam width for a span mark from {mark: width} (exact or normalised match), else None."""
    if not widths:
        return None
    if mark in widths:
        return widths[mark]
    key = normalize_str(mark)
    return next((w for m, w in widths.items() if normalize_str(m) == key), None)


def failures_table(result, with_fixes=False, widths=None):
    """The assistant's main table: counts as facts, and one row per failing check (optionally with fixes)."""
    rows = failure_rows(result)
    if with_fixes:
        for row in rows:
            width = width_for(row["beam"], widths)
            if row["check"] == "Flexure":
                s = suggest_bars(row["required"], width, row["provided_as"])
                row.update(suggested_change=s["suggestion"] or "-", suggested_provides=s["area"],
                           suggested_over_required_pct=_pct(s["area"], row["required"]) if s["area"] else None,
                           fit=s["fit"], beam_width_mm=width)
            else:
                s = suggest_stirrups(row["required"], width)
                row.update(suggested_change=s["suggestion"] or "-", suggested_provides=s["asv_sv"],
                           suggested_over_required_pct=_pct(s["asv_sv"], row["required"]) if s["asv_sv"] else None,
                           fit=s["fit"], beam_width_mm=width)
    out = {"counts": failure_counts(result), "rows": rows}
    if with_fixes:
        out["note"] = ("Suggestions are the smallest arrangement that meets the requirement, limited to "
                       f"{MAX_LAYERS} layers that fit the beam width (cover, links, minimum clear gap) when the width "
                       "is known. An engineer must check spacing, anchorage, laps and detailing.")
    return out
