"""Country names and codes normalise to ISO 3166-1 alpha-2; nothing is guessed."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from manifest_domain.normalisers.countries import (
    _BY_NAME,
    ISO_3166_ALPHA2,
    NAMES,
    _key,
    _rows,
    normalise_country,
)
from manifest_domain.normalisers.numbers import NormalisationError

ROWS = _rows()


def test_every_assigned_code_once() -> None:
    assert len(ISO_3166_ALPHA2) == len(ROWS) == 249
    names = [name for row in ROWS for name in row[1:]]
    assert len({_key(name) for name in names}) == len(names) == len(_BY_NAME)  # no clashes


@given(st.sampled_from(ROWS))
def test_codes_and_every_listed_name_normalise_to_the_code(row: list[str]) -> None:
    code, *names = row
    assert normalise_country(code) == code
    assert normalise_country(code.lower()) == code
    for name in names:
        assert normalise_country(name) == code
        assert normalise_country(name.upper()) == code
        assert normalise_country(f"  Made in {name} ") == code


@pytest.mark.parametrize(
    ("printed", "code"),
    [
        ("COTE D'IVOIRE", "CI"),
        ("C\u00f4te d\u2019Ivoire", "CI"),  # typographic apostrophe, as often printed
        ("the Netherlands", "NL"),
        ("SOUTH KOREA", "KR"),
        ("Korea, Republic of", "KR"),
        ("U.S.A.", "US"),
        ("UK", "GB"),
        ("Turkey", "TR"),
        ("Viet Nam", "VN"),
        ("Taiwan", "TW"),
    ],
)
def test_common_spellings(printed: str, code: str) -> None:
    assert normalise_country(printed) == code


@pytest.mark.parametrize("printed", ["", "  ", "Korea", "Narnia", "ZZ", "XK", "Europe"])
def test_unknown_or_ambiguous_is_rejected(printed: str) -> None:
    with pytest.raises(NormalisationError):
        normalise_country(printed)


def test_short_names() -> None:
    assert NAMES["CN"] == "China"
    assert NAMES["GB"].startswith("United Kingdom")
