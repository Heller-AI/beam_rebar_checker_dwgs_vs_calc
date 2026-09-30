"""Parsers for rebar / stirrup notation and beam marks (e.g. '3H20+2H16', '2H10-200')."""

import math
import re

EMPTY_VALUES = ["", "nan", "none", "-", "n.a", "n/a"]
ARROW_CHARS = ["←", "→", "🡠", "🡢", "!", '"', "-", "—"]


def is_arrow_symbol(val_str):
    """True if a schedule cell only contains a 'continue from neighbour' arrow/dash."""
    if not val_str:
        return False
    return str(val_str).strip() in ARROW_CHARS


def parse_bar_notation(notation_str):
    """'3H20+2H16' -> (total area mm², normalised notation)."""
    if not notation_str or str(notation_str).strip().lower() in EMPTY_VALUES:
        return 0.0, "-"

    total_area = 0.0
    valid_parts = []
    for part in str(notation_str).strip().split("+"):
        part = part.strip()
        if "H" in part.upper():
            try:
                tokens = part.upper().split("H")
                num = int(tokens[0]) if tokens[0] != "" else 1
                dia = float(tokens[1])
                total_area += num * (math.pi * (dia ** 2) / 4.0)
                valid_parts.append(f"{num}H{int(dia) if dia.is_integer() else dia}")
            except Exception:
                pass

    return round(total_area, 1), "+".join(valid_parts) if valid_parts else "-"


def parse_stirrup_single_str(notation_str):
    """'2H10-200' or '2H10/200' -> (Asv/sv in mm²/mm, notation). Legs default to 2."""
    if not notation_str or str(notation_str).strip().lower() in EMPTY_VALUES:
        return 0.0, "-"

    clean_str = str(notation_str).strip().upper()

    try:
        if "-" in clean_str:
            bar_part, spacing_str = clean_str.split("-")[0], clean_str.split("-")[1]
        elif "/" in clean_str:
            bar_part, spacing_str = clean_str.split("/")[0], clean_str.split("/")[1]
        else:
            return 0.0, clean_str

        spacing = float(re.sub(r"[^\d.]", "", spacing_str))
        tokens = bar_part.split("H")

        legs = int(tokens[0]) if (tokens[0] != "" and tokens[0].isdigit()) else 2
        dia = float(tokens[1])

        if spacing > 0:
            asv_total = legs * math.pi * (dia ** 2) / 4.0
            return round(asv_total / spacing, 3), clean_str
    except Exception:
        pass

    return 0.0, clean_str


def parse_stirrup_list(stirrup_str_list):
    """Sum Asv/sv over several stirrup notations (e.g. multiple rows for one zone)."""
    total_asv_sv = 0.0
    valid_notations = []

    for s_str in stirrup_str_list:
        asv_sv, not_str = parse_stirrup_single_str(s_str)
        if asv_sv > 0:
            total_asv_sv += asv_sv
            valid_notations.append(not_str)

    combined_notation = "+".join(valid_notations) if valid_notations else "-"
    return round(total_asv_sv, 3), combined_notation


def normalize_str(text):
    """Loose comparison key for beam marks: drop spaces, dashes, underscores, dots; lowercase."""
    if not text:
        return ""
    return re.sub(r"[\s\-_.]", "", str(text)).lower()


def clean_suffix(mark):
    """Split base beam mark and span number, e.g. '12TRB10-2' -> ('12TRB10', 2)."""
    if not mark:
        return "", 1
    mark_str = str(mark).strip()
    match = re.match(r"^(.+?)[\-_](\d+)$", mark_str)
    if match:
        return match.group(1).strip(), int(match.group(2))
    return mark_str, 1


def is_valid_beam_mark(name_str):
    """Reject pure numbers and very short strings that are not beam marks."""
    if not name_str:
        return False
    clean = str(name_str).strip()
    return not (clean.isdigit() or len(clean) < 3)
