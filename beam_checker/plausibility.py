"""Plausibility screening of bar counts in a schedule row: flags likely typos such as 33H25 for 3H25.

This only highlights rows for a person to check. It never changes what the checker counts as
provided steel, and never changes OK/FAIL.

Rule: each "+" part of a bar notation (e.g. 6H32+6H25+6H25 -> 6H32, 6H25, 6H25) is one group of
bars. A group is implausible when it has more bars than would fit across the beam width in
LAYERS_PER_GROUP layers, using typical detailing values. The allowance is deliberately generous
so that only gross errors (an extra digit, a swapped number) are flagged, not tight detailing.
"""

import math
import re

SIDE_COVER_MM = 25        # cover to the links on each side
LINK_DIA_MM = 10          # assumed link diameter on each side
MIN_CLEAR_SPACING_MM = 25  # clear gap between bars: at least this, and at least the bar diameter
LAYERS_PER_GROUP = 2      # one notation group may occupy up to this many layers

BAR_FIELDS = ("T1", "T2", "T3", "B1", "B2", "B3")
_GROUP = re.compile(r"^\s*(\d*)\s*[Hh]\s*(\d+(?:\.\d+)?)\s*$")
_WIDTH = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[xX×*]")


def beam_width(size):
    """Width in mm from a size such as '300x1000' or '200x225/175'; None if it cannot be read."""
    m = _WIDTH.match(str(size or ""))
    return float(m.group(1)) if m else None


def bars_per_layer(width, dia):
    """How many bars of this diameter fit in one layer across the beam (at least 2: the corners)."""
    clear = width - 2 * (SIDE_COVER_MM + LINK_DIA_MM)
    if clear <= 0:
        return 2
    gap = max(dia, MIN_CLEAR_SPACING_MM)
    return max(2, math.floor((clear + gap) / (dia + gap)))


def group_limit(width, dia):
    return LAYERS_PER_GROUP * bars_per_layer(width, dia)


def check_notation(notation, width):
    """Implausible groups in one bar notation: [{"group", "bars", "dia", "limit"}]."""
    issues = []
    for group in str(notation or "").split("+"):
        m = _GROUP.match(group)
        if not m:
            continue                      # blanks, dashes, arrows and unparseable text are not judged here
        n = int(m.group(1)) if m.group(1) else 1
        dia = float(m.group(2))
        limit = group_limit(width, dia)
        if n > limit:
            issues.append({"group": group.strip(), "bars": n, "dia": dia, "limit": limit})
    return issues


def row_issues(row):
    """Implausible bar groups in a schedule row (a mapping with 'Size'/'size' and T1..B3)."""
    width = beam_width(row.get("Size", row.get("size", "")))
    if not width:
        return []
    out = []
    for field in BAR_FIELDS:
        for issue in check_notation(row.get(field, ""), width):
            out.append({"field": field, "width": width, **issue})
    return out


def describe(issue):
    """One readable line, e.g. 'T3 = 33H25: more than 10 bars of H25 cannot fit a 300 mm wide beam in 2 layers'."""
    dia = int(issue["dia"]) if float(issue["dia"]).is_integer() else issue["dia"]
    return (f"{issue['field']} = {issue['group']}: more than {issue['limit']} bars of H{dia} cannot fit a "
            f"{issue['width']:g} mm wide beam in {LAYERS_PER_GROUP} layers")
