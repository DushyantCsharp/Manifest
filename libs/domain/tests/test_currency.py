"""Currency mentions to ISO 4217: codes, symbols, names, codes buried in sentences."""

import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from manifest_domain.normalisers.currency import (
    ISO_4217,
    NAMES,
    SYMBOLS,
    WORD_CODES,
    normalise_currency,
)
from manifest_domain.normalisers.numbers import AmbiguityError, NormalisationError

codes = st.sampled_from(sorted(ISO_4217))
# Surrounding words that contain no 3-letter capitalised word of their own.
prefixes = st.sampled_from(["", "Currency:", "Devise / Currency", "Amounts in", "Total", "("])
suffixes = st.sampled_from(["", "only", ")", "- payable at sight", "."])


def recase(text: str, data: st.DataObject) -> str:
    flags = data.draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(c.lower() if flag else c.upper() for c, flag in zip(text, flags, strict=True))


@given(codes, st.data())
def test_a_code_in_any_case_and_padding(code: str, data: st.DataObject) -> None:
    padding = data.draw(st.sampled_from(["", " ", "  ", "\t", "\n"]))
    assert normalise_currency(f"{padding}{recase(code, data)}{padding}") == code


@given(codes, prefixes, suffixes)
def test_a_code_inside_a_phrase(code: str, prefix: str, suffix: str) -> None:
    assert normalise_currency(f"{prefix} {code} {suffix}") == code


@given(codes.filter(lambda c: c not in WORD_CODES), st.sampled_from(sorted(WORD_CODES)))
def test_capitalised_words_that_are_codes_yield_to_the_real_one(code: str, word: str) -> None:
    assert normalise_currency(f"{word} PRICES IN {code}") == code


@given(st.sampled_from(sorted(NAMES.items())), st.data())
def test_names_singular_or_plural_in_any_case(item: tuple[str, str], data: st.DataObject) -> None:
    name, code = item
    words = [w + ("S" if data.draw(st.booleans()) else "") for w in name.split()]
    text = recase(" ".join(words), data)
    assert normalise_currency(f"All amounts in {text}") == code


@given(st.sampled_from(sorted(SYMBOLS.items())))
def test_symbols(item: tuple[str, str]) -> None:
    symbol, code = item
    assert normalise_currency(f" {symbol} ") == code


@given(st.sampled_from(["USD", "CAD", "AUD", "HKD", "SGD"]))
def test_a_bare_dollar_needs_context(dollar: str) -> None:
    with pytest.raises(AmbiguityError):
        normalise_currency("$")
    assert normalise_currency("$", dollar=dollar.lower()) == dollar


@given(st.text(max_size=30))
def test_anything_else_is_a_code_or_rejected_cleanly(text: str) -> None:
    with contextlib.suppress(NormalisationError):
        assert normalise_currency(text) in ISO_4217


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("All amounts are in pounds sterling (GBP).", "GBP"),
        ("US DOLLARS", "USD"),
        ("Dollar US", "USD"),
        ("Währung / Currency EUR", "EUR"),
        ("Total USD : 887 133,24", "USD"),
        ("Renminbi", "CNY"),
    ],
)
def test_examples(text: str, code: str) -> None:
    assert normalise_currency(text) == code


@pytest.mark.parametrize("text", ["", "dollars", "XYZ", "USD or EUR", "Galleons"])
def test_not_a_currency(text: str) -> None:
    with pytest.raises(NormalisationError):
        normalise_currency(text)
