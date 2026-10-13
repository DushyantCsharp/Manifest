"""Printed dates to ``datetime.date``: ISO, ``30.09.2026``, ``09/30/2026``, ``30-SEP-2026``,
``30 Sep 2026``, ``September 30, 2026``.

``a/b/yyyy`` with both parts <= 12 is ambiguous; pass ``day_first`` from context or it raises.
"""

import re
from datetime import date

from manifest_domain.normalisers.numbers import AmbiguityError, NormalisationError

MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}

_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DOTTED = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_SLASHED = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_DAY_MONTH_NAME = re.compile(r"(\d{1,2})[-\s.]+([A-Za-z]+)\.?[-\s,]+(\d{4})")
_MONTH_NAME_DAY = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})")


def _build(year: int, month: int, day: int, text: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise NormalisationError(f"{text!r} is not a valid date") from exc


def _month(name: str, text: str) -> int:
    try:
        return MONTHS[name.lower()]
    except KeyError as exc:
        raise NormalisationError(f"{text!r} has an unknown month") from exc


def normalise_date(text: str, *, day_first: bool | None = None) -> date:
    cleaned = " ".join(text.strip().split())
    if match := _ISO.fullmatch(cleaned):
        year, month, day = map(int, match.groups())
        return _build(year, month, day, text)
    if match := _DOTTED.fullmatch(cleaned):
        day, month, year = map(int, match.groups())
        return _build(year, month, day, text)
    if match := _SLASHED.fullmatch(cleaned):
        first, second, year = map(int, match.groups())
        if first == second or first > 12 or (day_first and second <= 12):
            return _build(year, second, first, text)
        if second > 12 or day_first is False:
            return _build(year, first, second, text)
        raise AmbiguityError(f"{text!r} is ambiguous without day_first")
    if match := _DAY_MONTH_NAME.fullmatch(cleaned):
        day_text, month_name, year_text = match.groups()
        return _build(int(year_text), _month(month_name, text), int(day_text), text)
    if match := _MONTH_NAME_DAY.fullmatch(cleaned):
        month_name, day_text, year_text = match.groups()
        return _build(int(year_text), _month(month_name, text), int(day_text), text)
    raise NormalisationError(f"{text!r} is not a date")
