"""Printed dates to ``datetime.date``: every convention, and ambiguity is never guessed."""

import contextlib
from datetime import date

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from manifest_domain.normalisers.dates import normalise_date
from manifest_domain.normalisers.numbers import AmbiguityError, NormalisationError

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")  # fmt: skip

dates = st.dates(min_value=date(1990, 1, 1), max_value=date(2099, 12, 31))


@given(dates, st.booleans())
def test_numeric_conventions(value: date, padded: bool) -> None:
    d, m = (f"{value.day:02d}", f"{value.month:02d}") if padded else (value.day, value.month)
    assert normalise_date(value.isoformat()) == value
    assert normalise_date(f"{d}.{m}.{value.year}") == value
    assert normalise_date(f"{d}/{m}/{value.year}", day_first=True) == value
    assert normalise_date(f"{m}/{d}/{value.year}", day_first=False) == value


@given(dates, st.sampled_from(["upper", "title", "lower"]), st.booleans())
def test_month_names(value: date, case: str, abbreviated: bool) -> None:
    name = MONTHS[value.month - 1]
    name = name[:3] if abbreviated else name
    name = {"upper": name.upper(), "title": name, "lower": name.lower()}[case]
    assert normalise_date(f"{value.day:02d}-{name}-{value.year}") == value
    assert normalise_date(f"{value.day} {name} {value.year}") == value
    assert normalise_date(f"{name} {value.day}, {value.year}") == value
    assert normalise_date(f"  {name}   {value.day}  {value.year} ") == value


@given(dates)
def test_slashed_dates_that_tell_their_own_order(value: date) -> None:
    assume(value.day > 12)
    assert normalise_date(f"{value.day}/{value.month}/{value.year}") == value
    assert normalise_date(f"{value.month}/{value.day}/{value.year}") == value
    # A hint that contradicts the date itself loses to the date.
    assert normalise_date(f"{value.month}/{value.day}/{value.year}", day_first=True) == value


@given(dates)
def test_ambiguous_slashed_dates_are_never_guessed(value: date) -> None:
    assume(value.day <= 12 and value.day != value.month)
    with pytest.raises(AmbiguityError):
        normalise_date(f"{value.day}/{value.month}/{value.year}")


@given(st.integers(1, 12), st.integers(1990, 2099))
def test_day_equal_to_month_is_not_ambiguous(day: int, year: int) -> None:
    assert normalise_date(f"{day}/{day}/{year}") == date(year, day, day)


@given(st.text(max_size=25))
def test_anything_else_is_a_date_or_rejected_cleanly(text: str) -> None:
    with contextlib.suppress(NormalisationError):
        normalise_date(text, day_first=True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30-SEP-2026", date(2026, 9, 30)),
        ("Sept. 30, 2026", date(2026, 9, 30)),
        ("30 Sept 2026", date(2026, 9, 30)),
        ("09/30/2026", date(2026, 9, 30)),
        ("07.04.2026", date(2026, 4, 7)),
        ("2026-2-3", date(2026, 2, 3)),
    ],
)
def test_examples(text: str, expected: date) -> None:
    assert normalise_date(text) == expected


@pytest.mark.parametrize(
    "text", ["31.02.2026", "2026-13-01", "30 Smarch 2026", "13/13/2026", "yesterday", ""]
)
def test_not_a_date(text: str) -> None:
    with pytest.raises(NormalisationError):
        normalise_date(text)
