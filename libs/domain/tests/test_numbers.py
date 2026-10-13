"""Numbers and weights: every locale's printing parses back to the exact Decimal."""

import contextlib
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from manifest_domain.normalisers.numbers import (
    KG_PER_UNIT,
    AmbiguityError,
    NormalisationError,
    infer_decimal_separator,
    normalise_weight,
    parse_decimal,
    to_kg,
)

Style = Literal["en", "de", "fr", "ch"]
GROUPING = {"en": ",", "de": ".", "fr": "\u202f", "ch": "'"}
DECIMAL = {"en": ".", "de": ",", "fr": ",", "ch": "."}
LB = Decimal("0.45359237")

amounts = st.decimals(min_value=0, max_value=10**9, places=4, allow_nan=False)
styles = st.sampled_from(["en", "de", "fr", "ch"])


def printed(value: Decimal, style: Style, places: int, *, grouped: bool = True) -> str:
    """``value`` the way an invoice in ``style`` prints it."""
    quantum = Decimal(1).scaleb(-places)
    text = f"{abs(value).quantize(quantum, ROUND_HALF_UP):{',' if grouped else ''}f}"
    text = text.replace(",", "\x00").replace(".", DECIMAL[style]).replace("\x00", GROUPING[style])
    return f"-{text}" if value < 0 else text


@given(amounts, styles, st.integers(0, 4), st.booleans())
def test_printing_round_trips_with_the_page_separator(
    value: Decimal, style: Style, places: int, grouped: bool
) -> None:
    text = printed(value, style, places, grouped=grouped)
    expected = value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
    separator = "," if DECIMAL[style] == "," else "."
    assert parse_decimal(text, separator) == expected


@given(amounts, styles, st.integers(0, 4))
def test_without_a_separator_it_parses_exactly_or_says_ambiguous(
    value: Decimal, style: Style, places: int
) -> None:
    text = printed(value, style, places)
    expected = value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
    try:
        assert parse_decimal(text) == expected
    except AmbiguityError:
        # Only "1,234" / "1.234" (one separator, 1-3 digits, then exactly 3) can't be told.
        assert re.fullmatch(r"[1-9]\d{0,2}[.,]\d{3}", text)


@given(st.lists(st.tuples(amounts, st.integers(0, 4)), min_size=1, max_size=8), styles)
def test_a_page_of_numbers_agrees_on_its_separator(
    values: list[tuple[Decimal, int]], style: Style
) -> None:
    texts = [printed(value, style, places) for value, places in values]
    inferred = infer_decimal_separator(texts)
    assert inferred in (None, DECIMAL[style])
    if any(places in (1, 2) for _, places in values):  # "12,5" or "12,50" gives it away
        assert inferred == DECIMAL[style]


@given(st.decimals(min_value=-(10**6), max_value=10**6, places=2, allow_nan=False))
def test_negatives_in_parentheses_or_with_a_sign(value: Decimal) -> None:
    assume(value != 0)
    text = printed(value, "en", 2)
    assert parse_decimal(text, ".") == value
    if value < 0:
        assert parse_decimal(f"({text.removeprefix('-')})", ".") == value


@given(st.text(max_size=20))
def test_anything_else_is_rejected_cleanly(text: str) -> None:
    with contextlib.suppress(NormalisationError):
        parse_decimal(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,234.56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("1\u202f234,56", "1234.56"),
        ("1\u00a0234,56", "1234.56"),
        ("1'234.50", "1234.50"),
        ("1\u2019234.50", "1234.50"),
        ("0,7141", "0.7141"),
        ("1.234.567", "1234567"),
        ("12,34,567.50", "1234567.50"),  # Indian lakh grouping
        ("0.125", "0.125"),
        ("1234.567", "1234.567"),
        ("(1,234.50)", "-1234.50"),
        ("+12", "12"),
    ],
)
def test_examples(text: str, expected: str) -> None:
    assert parse_decimal(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text", ["", "-", "1.2.3,4,5", "12a", "1,2,3.4.5", "..", "1,,2", "12,3456.7", "1.23,4"]
)
def test_malformed(text: str) -> None:
    with pytest.raises(NormalisationError):
        parse_decimal(text)


# ---------------------------------------------------------------- unit conversion

kilos = st.decimals(min_value=0, max_value=10**6, places=3, allow_nan=False)


@given(kilos)
def test_unit_conversions_are_exact(amount: Decimal) -> None:
    assert to_kg(amount, "g") * 1000 == amount
    assert to_kg(amount, "t") == amount * 1000
    assert to_kg(amount, "lb") == amount * LB
    assert to_kg(amount, "oz") * 16 == to_kg(amount, "lb")  # 16 oz to the pound, exactly


@given(kilos, st.sampled_from(sorted(KG_PER_UNIT)))
def test_unit_spellings_are_case_and_period_insensitive(amount: Decimal, unit: str) -> None:
    assert to_kg(amount, f" {unit.upper()}. ") == to_kg(amount, unit)


@given(kilos, st.sampled_from(sorted(KG_PER_UNIT)), st.sampled_from(sorted(KG_PER_UNIT)))
def test_conversion_preserves_order(amount: Decimal, unit: str, other: str) -> None:
    bigger = amount + 1
    assert to_kg(bigger, unit) > to_kg(amount, unit)
    if KG_PER_UNIT[unit] >= KG_PER_UNIT[other]:
        assert to_kg(amount, unit) >= to_kg(amount, other)


@given(st.decimals(min_value=0, max_value=50_000, places=2, allow_nan=False), styles)
def test_weights_printed_in_kg(kg: Decimal, style: Style) -> None:
    text = f"{printed(kg, style, 2)} kg"
    assert normalise_weight(text, "," if DECIMAL[style] == "," else ".") == kg


@given(st.decimals(min_value=0, max_value=50_000, places=2, allow_nan=False))
def test_weights_printed_in_pounds_come_back_within_rounding(kg: Decimal) -> None:
    """Invoices that print lb to 0.1 lose at most 0.05 lb (~0.023 kg) plus 0.005 kg."""
    printed_lb = (kg / LB).quantize(Decimal("0.1"), ROUND_HALF_UP)
    recovered = normalise_weight(f"{printed(printed_lb, 'en', 1)} lb", ".")
    assert abs(recovered - kg) <= Decimal("0.05") * LB + Decimal("0.005")


@pytest.mark.parametrize(
    ("text", "kg"),
    [
        ("5,999.5 lb", "2721.33"),
        ("1.739,19 kg", "1739.19"),
        ("23 763,01 kg", "23763.01"),
        ("12 KGS", "12.00"),
        ("1\u00a0000 g", "1.00"),
        ("2.5 MT", "2500.00"),
        ("3,5", "3.50"),  # no unit: kg
    ],
)
def test_weight_examples(text: str, kg: str) -> None:
    assert normalise_weight(text) == Decimal(kg)


@pytest.mark.parametrize("text", ["heavy", "12 stone", "kg"])
def test_bad_weights(text: str) -> None:
    with pytest.raises(NormalisationError):
        normalise_weight(text)
