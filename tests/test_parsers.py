import math

import pytest

from beam_checker.parsers import (
    clean_suffix,
    font_key,
    is_arrow_symbol,
    is_symbol_font,
    is_valid_beam_mark,
    map_symbol_text,
    normalize_str,
    parse_bar_notation,
    parse_stirrup_list,
    parse_stirrup_single_str,
)


def area(n, d):
    return n * math.pi * d * d / 4


def test_bar_notation_single_and_combined():
    assert parse_bar_notation("2H16") == (round(area(2, 16), 1), "2H16")
    assert parse_bar_notation("3H20 + 2H16") == (round(area(3, 20) + area(2, 16), 1), "3H20+2H16")


@pytest.mark.parametrize("empty", [None, "", "nan", "-", "N/A"])
def test_bar_notation_empty(empty):
    assert parse_bar_notation(empty) == (0.0, "-")


def test_stirrup_dash_and_slash():
    expected = round(area(2, 10) / 200, 3)
    assert parse_stirrup_single_str("2H10-200") == (expected, "2H10-200")
    assert parse_stirrup_single_str("2h10/200") == (expected, "2H10/200")


def test_stirrup_default_two_legs():
    assert parse_stirrup_single_str("H10-200")[0] == round(area(2, 10) / 200, 3)


def test_stirrup_list_sums():
    asv, notation = parse_stirrup_list(["2H10-200", "2H10-200"])
    assert asv == pytest.approx(2 * round(area(2, 10) / 200, 3))
    assert notation == "2H10-200+2H10-200"


def test_beam_mark_helpers():
    assert clean_suffix("12TRB10-2") == ("12TRB10", 2)
    assert clean_suffix("B101") == ("B101", 1)
    assert normalize_str("B 101-a") == "b101a"
    assert is_valid_beam_mark("B101")
    assert not is_valid_beam_mark("123")
    assert not is_valid_beam_mark("B1")
    assert is_arrow_symbol(" → ")
    assert not is_arrow_symbol("2H16")


def test_symbol_font_glyphs_map_only_in_their_font():
    assert font_key("ABCDEF+Wingdings3,Regular") == "WINGDINGS3" and font_key("WINGDNG3.TTF") == "WINGDNG3"
    assert is_symbol_font("Wingdings 3") and not is_symbol_font("Calibri") and not is_symbol_font("")
    assert map_symbol_text("!", "Wingdings 3") == ("←", True)
    assert map_symbol_text('"', "Wingdings3") == ("→", True)
    assert map_symbol_text("!", "Arial") == ("!", True)                 # a real exclamation mark is never mapped
    assert map_symbol_text("x", "Wingdings 3") == ("x", False)          # unknown glyph: kept, to be flagged
    assert map_symbol_text("!", "Webdings") == ("!", False)             # symbol font without a table
    assert all(is_arrow_symbol(map_symbol_text(c, "Wingdings 3")[0]) for c in "!\"")   # the checker reads them
