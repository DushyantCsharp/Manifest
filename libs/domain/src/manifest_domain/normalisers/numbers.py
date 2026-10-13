"""Printed numbers and weights to exact Decimals, whatever the locale's separators.

``1,234.56`` (en), ``1.234,56`` (de), ``1 234,56`` (fr) and ``1'234.56`` (ch) all parse to
``Decimal("1234.56")``. A lone separator followed by exactly three digits (``1,234``) is
ambiguous; pass the page's ``decimal_separator`` (see ``infer_decimal_separator``).
"""

import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Separator = Literal[".", ","]

# space, no-break space, narrow no-break space, apostrophe, right single quote
GROUPING_CHARACTERS = frozenset(" \u00a0\u202f'\u2019")
KG_QUANTUM = Decimal("0.01")
# kg per unit; exact (the pound is defined as 0.45359237 kg).
KG_PER_UNIT: dict[str, Decimal] = {
    "kg": Decimal(1),
    "kgs": Decimal(1),
    "kilo": Decimal(1),
    "kilos": Decimal(1),
    "kilogram": Decimal(1),
    "kilograms": Decimal(1),
    "g": Decimal("0.001"),
    "gram": Decimal("0.001"),
    "grams": Decimal("0.001"),
    "t": Decimal(1000),
    "mt": Decimal(1000),
    "tonne": Decimal(1000),
    "tonnes": Decimal(1000),
    "lb": Decimal("0.45359237"),
    "lbs": Decimal("0.45359237"),
    "pound": Decimal("0.45359237"),
    "pounds": Decimal("0.45359237"),
    "oz": Decimal("0.028349523125"),
}

_NUMBER = re.compile(r"[\d.,]+")
_TRAILING_DECIMALS = re.compile(r"[.,](\d{1,2})$")
# \s covers the no-break spaces; apostrophes group Swiss thousands.
_WEIGHT = re.compile(r"\s*([-+(]?[\d.,\s'\u2019]+\)?)\s*([A-Za-z.]*)\s*")


class NormalisationError(ValueError):
    """The text doesn't hold a value of the expected kind (or it's ambiguous)."""


class AmbiguityError(NormalisationError):
    """The text reads more than one way; context (the page's conventions) must decide."""


def _compact(text: str) -> str:
    return "".join(ch for ch in text.strip() if ch not in GROUPING_CHARACTERS)


def infer_decimal_separator(texts: Iterable[str]) -> Separator | None:
    """The decimal separator a set of numbers from one document agrees on, if any tells."""
    for text in texts:
        compact = _compact(text)
        if "." in compact and "," in compact:
            return "." if compact.rindex(".") > compact.rindex(",") else ","
        if match := _TRAILING_DECIMALS.search(compact):
            return "." if compact[match.start()] == "." else ","
    return None


def parse_decimal(text: str, decimal_separator: Separator | None = None) -> Decimal:
    compact = _compact(text)
    negative = compact.startswith("-") or (compact.startswith("(") and compact.endswith(")"))
    digits = compact.strip("()+-")
    if not digits or not _NUMBER.fullmatch(digits) or not any(c.isdigit() for c in digits):
        raise NormalisationError(f"{text!r} is not a number")
    separator = decimal_separator or _guess_separator(digits, text)
    grouping = "," if separator == "." else "."
    integral, _, fraction = digits.partition(separator)
    groups = integral.split(grouping)
    if not _well_grouped(groups) or (fraction and not fraction.isdigit()):
        raise NormalisationError(f"{text!r} has misplaced separators")
    integral = "".join(groups)
    value = Decimal(f"{integral or 0}.{fraction}" if fraction else integral)
    return -value if negative else value


def _well_grouped(groups: list[str]) -> bool:
    """``1,234,567`` and Indian ``12,34,567``; not ``1,,2`` or ``12,3456``."""
    if len(groups) == 1:
        return groups[0] == "" or groups[0].isdigit()
    first, *middle, last = groups
    return (
        first.isdigit()
        and len(first) <= 3
        and all(group.isdigit() and len(group) in (2, 3) for group in middle)
        and last.isdigit()
        and len(last) == 3
    )


def _guess_separator(digits: str, text: str) -> Separator:
    if "." in digits and "," in digits:
        return "." if digits.rindex(".") > digits.rindex(",") else ","
    for separator in (".", ","):
        count = digits.count(separator)
        if count > 1:
            return "," if separator == "." else "."  # repeated: it's grouping
        if count == 1:
            integral, _, fraction = digits.partition(separator)
            grouping_shaped = len(fraction) == 3 and 0 < len(integral) <= 3 and integral != "0"
            if not grouping_shaped:
                return separator  # "12.5", "0,7141", "0.125", "1234.567"
    if "." in digits or "," in digits:
        raise AmbiguityError(f"{text!r} is ambiguous without a decimal separator")
    return "."


def to_kg(amount: Decimal, unit: str) -> Decimal:
    try:
        return amount * KG_PER_UNIT[unit.strip().lower().rstrip(".")]
    except KeyError as exc:
        raise NormalisationError(f"unknown weight unit {unit!r}") from exc


def normalise_weight(text: str, decimal_separator: Separator | None = None) -> Decimal:
    """``"5,999.5 lb"`` -> kg, rounded to 0.01 kg (half up)."""
    match = _WEIGHT.fullmatch(text)
    if match is None:
        raise NormalisationError(f"{text!r} is not a weight")
    amount = parse_decimal(match.group(1), decimal_separator)
    unit = match.group(2) or "kg"
    return to_kg(amount, unit).quantize(KG_QUANTUM, ROUND_HALF_UP)
