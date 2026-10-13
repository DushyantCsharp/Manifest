"""Delivery terms to Incoterms 2020 rule plus named place."""

import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from manifest_domain.normalisers.incoterms import (
    INCOTERMS_2020,
    SUPERSEDED,
    Incoterm,
    normalise_incoterm,
)
from manifest_domain.normalisers.numbers import NormalisationError

rules = st.sampled_from(sorted(INCOTERMS_2020))
places = st.from_regex(r"[A-Z][a-z]{2,10}( [A-Z][a-z]{2,10}){0,2}", fullmatch=True)
decorations = st.sampled_from(
    [
        ("", ""),
        ("Incoterms 2020: ", ""),
        ("Incoterms® 2020 ", ""),
        ("", " (Incoterms 2020)"),
        ("", " (Incoterms® 2020)"),
        ("Incoterm ", ""),
    ]
)
separators = st.sampled_from([" ", "  ", ", ", " - ", ": "])


@given(rules, places, decorations, separators, st.booleans())
def test_rule_and_place(
    rule: str, place: str, decoration: tuple[str, str], separator: str, lower: bool
) -> None:
    before, after = decoration
    code = rule.lower() if lower else rule
    assert normalise_incoterm(f"{before}{code}{separator}{place}{after}") == Incoterm(rule, place)


@given(rules, decorations)
def test_rule_without_a_place(rule: str, decoration: tuple[str, str]) -> None:
    before, after = decoration
    assert normalise_incoterm(f"{before}{rule}{after}") == Incoterm(rule, None)


@given(rules, places, st.sampled_from(["upper", "lower", "title"]))
def test_spelled_out(rule: str, place: str, case: str) -> None:
    name = INCOTERMS_2020[rule]
    name = {"upper": name.upper(), "lower": name.lower(), "title": name.title()}[case]
    assert normalise_incoterm(f"{name} {place}") == Incoterm(rule, place)


@given(st.sampled_from(sorted(SUPERSEDED.items())), places)
def test_superseded_terms_map_to_2020(item: tuple[str, str], place: str) -> None:
    old, new = item
    assert normalise_incoterm(f"{old} {place}") == Incoterm(new, place)


@given(st.from_regex(r"[A-Z]{3}", fullmatch=True), places)
def test_unknown_three_letter_codes_are_rejected(code: str, place: str) -> None:
    if code in INCOTERMS_2020 or code in SUPERSEDED:
        return
    with pytest.raises(NormalisationError):
        normalise_incoterm(f"{code} {place}")


@given(st.text(max_size=30))
def test_anything_else_is_a_rule_or_rejected_cleanly(text: str) -> None:
    with contextlib.suppress(NormalisationError):
        assert normalise_incoterm(text).code in INCOTERMS_2020


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FCA Manta (Incoterms 2020)", Incoterm("FCA", "Manta")),
        ("Lieferbedingung CIP Hamburg", None),
        ("C & F Durban", Incoterm("CFR", "Durban")),
        ("C and F Durban", Incoterm("CFR", "Durban")),
        ("Delivered at Place Unloaded Hamburg", Incoterm("DPU", "Hamburg")),
        ("Cost, Insurance and Freight Rotterdam", Incoterm("CIF", "Rotterdam")),
        ("EXW", Incoterm("EXW", None)),
    ],
)
def test_examples(text: str, expected: Incoterm | None) -> None:
    if expected is None:
        with pytest.raises(NormalisationError):
            normalise_incoterm(text)
    else:
        assert normalise_incoterm(text) == expected
