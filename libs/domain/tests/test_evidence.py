"""The evidence post-check: spans must be on the page and must show the value."""

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from manifest_domain.evidence import check, collapse

PAGE = (
    "COMMERCIAL INVOICE\r\nInvoice No: RE-26-3487\r\nDate: 17.04.2026\r\n"
    "Women's cotton denim 1\u202f200 pcs\r\ntrousers\r\n"
    "Total EUR 12.345,60\r\nGross weight: 1.739,19 kg\r\nNet 120 kg\r\nPrice $4.10"
)


@pytest.mark.parametrize(
    ("value", "evidence", "kind"),
    [
        ("RE-26-3487", "Invoice No: RE-26-3487", "text"),
        ("17.04.2026", "Date: 17.04.2026", "date"),
        ("12.345,60", "Total EUR 12.345,60", "decimal"),
        ("1.739,19 kg", "Gross weight: 1.739,19 kg", "weight"),
        ("1 200", "denim 1 200 pcs", "decimal"),  # U+202F on the page, a plain space quoted
        ("EUR", "Total EUR 12.345,60", "currency"),
        ("$", "Price $4.10", "currency"),
        ("4.10", "Price $4.10", "decimal"),
        # A wrapped cell: the description's words are split by another column's text.
        ("Women's cotton denim trousers", "Women's cotton denim 1 200 pcs trousers", "text"),
        ("Invoice No: RE-26-3487", "Invoice No:\nRE-26-3487", "text"),  # line breaks collapse
        ("Gross weight", "Gross weight: 1.739,19 kg", "text"),  # punctuation around words
    ],
)
def test_verified(value: str, evidence: str, kind: str) -> None:
    assert check(PAGE, value, evidence, kind) == "verified"


@pytest.mark.parametrize(
    ("value", "evidence", "kind", "why"),
    [
        ("RE-26-3478", "Invoice No: RE-26-3478", "text", "span isn't on the page"),
        ("RE-26-3487", "invoice no: RE-26-3487", "text", "paraphrased: case differs"),
        ("RE-26-3487", "", "text", "no span"),
        ("", "Invoice No: RE-26-3487", "text", "no value"),
        ("18.04.2026", "Date: 17.04.2026", "date", "span doesn't show the value"),
        ("12", "Net 120 kg", "decimal", "part of a longer number"),
        ("345,60", "Total EUR 12.345,60", "decimal", "part of a grouped number"),
        ("12.345,6", "Total EUR 12.345,60", "decimal", "truncated number"),
        ("trousers denim", "Women's cotton denim 1 200 pcs trousers", "text", "out of order"),
    ],
)
def test_failed(value: str, evidence: str, kind: str, why: str) -> None:
    assert check(PAGE, value, evidence, kind) == "failed", why


words = st.lists(st.text(alphabet="ABCdef0123.,-", min_size=1, max_size=6), min_size=1, max_size=12)


@given(words, st.data())
def test_any_verbatim_slice_of_the_page_verifies_its_own_words(
    page_words: list[str], data: st.DataObject
) -> None:
    start = data.draw(st.integers(0, len(page_words) - 1))
    end = data.draw(st.integers(start + 1, len(page_words)))
    span = page_words[start:end]
    picked = sorted(set(data.draw(st.lists(st.integers(0, len(span) - 1), min_size=1))))
    value = " ".join(span[i] for i in picked)
    page = "\n".join(page_words)  # line breaks where the quote has spaces
    assert check(page, value, " ".join(span), "text") == "verified"


@given(st.text(max_size=40), st.text(min_size=1, max_size=20))
def test_a_span_not_on_the_page_never_verifies(page: str, span: str) -> None:
    assume(collapse(span) and collapse(span) not in collapse(page))
    assert check(page, span, span, "text") == "failed"
    assert check(page, span, span, "decimal") == "failed"
