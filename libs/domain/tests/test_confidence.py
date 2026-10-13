"""Confidence aggregation stays a probability: bounded, symmetric, monotone, consistent."""

from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from manifest_domain.confidence import (
    CEILING,
    FLOOR,
    contradict,
    corroborate,
    joint,
    settle,
    stored,
    weakest,
)
from manifest_domain.extraction import ExtractedValue, Signals

TOLERANCE = Decimal("1e-20")
confidences = st.decimals(min_value=0, max_value=1, places=4, allow_nan=False)
interior = st.decimals(min_value=FLOOR, max_value=CEILING, places=4, allow_nan=False)
likely = st.decimals(min_value=Decimal("0.5"), max_value=1, places=4, allow_nan=False)


def close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE


@given(st.lists(confidences, min_size=1, max_size=6))
def test_corroboration_is_a_probability(values: list[Decimal]) -> None:
    assert 0 < corroborate(*values) < 1


@given(confidences, confidences)
def test_corroboration_is_symmetric(a: Decimal, b: Decimal) -> None:
    assert corroborate(a, b) == corroborate(b, a)


@given(interior, interior, interior)
def test_corroboration_is_associative(a: Decimal, b: Decimal, c: Decimal) -> None:
    # Within the stored range; beyond it, clamping (no reading is ever certain) takes over.
    assume(all(FLOOR < corroborate(*pair) < CEILING for pair in ((a, b), (b, c))))
    assert close(corroborate(corroborate(a, b), c), corroborate(a, b, c))
    assert close(corroborate(a, corroborate(b, c)), corroborate(a, b, c))


@given(interior)
def test_an_even_reading_is_no_evidence(a: Decimal) -> None:
    assert close(corroborate(a, Decimal("0.5")), a)
    assert close(corroborate(a), a)


@given(likely, likely)
def test_agreement_raises_confidence(a: Decimal, b: Decimal) -> None:
    assert corroborate(a, b) >= min(max(a, b), CEILING) - TOLERANCE


@given(confidences, confidences)
def test_wrong_and_right_are_mirror_images(a: Decimal, b: Decimal) -> None:
    assert close(corroborate(1 - a, 1 - b), 1 - corroborate(a, b))


@given(confidences, confidences, confidences)
def test_more_confident_readings_give_more_confidence(a: Decimal, b: Decimal, c: Decimal) -> None:
    assume(b <= c)
    assert corroborate(a, b) <= corroborate(a, c)


@given(confidences, confidences)
def test_contradiction_never_raises_confidence(a: Decimal, rival: Decimal) -> None:
    assert contradict(a, rival) <= min(max(a, FLOOR), CEILING) + TOLERANCE
    assert 0 < contradict(a, rival) < 1


@given(interior, likely, likely)
def test_a_more_confident_rival_lowers_it_more(a: Decimal, rival: Decimal, other: Decimal) -> None:
    assume(rival <= other)
    assert contradict(a, other) <= contradict(a, rival)


@given(likely)
def test_an_equally_confident_rival_leaves_a_coin_toss(a: Decimal) -> None:
    assert close(contradict(a, a), Decimal("0.5"))


@given(st.lists(confidences, max_size=8), confidences)
def test_joint_is_bounded_by_the_weakest_and_falls_with_each_value(
    values: list[Decimal], extra: Decimal
) -> None:
    assert 0 <= joint(values) <= weakest(values)
    assert joint([*values, extra]) <= joint(values)


def test_empty_and_invalid() -> None:
    assert joint([]) == 1
    assert weakest([]) == 1
    with pytest.raises(ValueError, match="nothing"):
        corroborate()
    with pytest.raises(ValueError, match="not in"):
        corroborate(Decimal("1.2"))
    with pytest.raises(ValueError, match="no readings"):
        settle([])


@given(st.decimals(min_value=0, max_value=1, allow_nan=False))
def test_stored_never_overstates(value: Decimal) -> None:
    assert stored(value) <= value
    # Not ``value - stored(value) < 0.0001``: subtraction rounds to the context's 28 digits.
    assert value < stored(value) + Decimal("0.0001")


# ---------------------------------------------------------------- settling a field


def reading(value: str | None, confidence: str) -> ExtractedValue:
    return ExtractedValue(value=value, confidence=Decimal(confidence))


@given(st.lists(confidences, min_size=1, max_size=5))
def test_unanimous_readings_corroborate(values: list[Decimal]) -> None:
    settled = settle([ExtractedValue(value="INV-1", confidence=c) for c in values])
    assert settled.value == "INV-1"
    assert settled.confidence == stored(corroborate(*values))


@given(
    st.lists(st.tuples(st.sampled_from(["A", "B", "C", None]), confidences), min_size=1),
)
def test_settling_picks_the_best_supported_value(
    readings: list[tuple[str | None, Decimal]],
) -> None:
    settled = settle([ExtractedValue(value=v, confidence=c) for v, c in readings])
    found = [(v, c) for v, c in readings if v is not None]
    if not found:
        assert settled.value is None
        return
    support = {value: corroborate(*[c for v, c in found if v == value]) for value, _ in found}
    assert support[settled.value] == max(support.values())
    assert 0 <= settled.confidence <= stored(support[settled.value])


def test_settling_examples() -> None:
    assert settle([reading("EUR", "0.9")]) == reading("EUR", "0.9")
    # Two pages agree, a third disagrees: still EUR, less sure than the agreement alone.
    settled = settle([reading("EUR", "0.9"), reading("EUR", "0.8"), reading("USD", "0.7")])
    assert settled.value == "EUR"
    assert Decimal("0.9") < settled.confidence < stored(corroborate(Decimal("0.9"), Decimal("0.8")))
    # A page that found nothing doesn't count against one that did.
    assert settle([reading(None, "0.99"), reading("EUR", "0.6")]) == reading("EUR", "0.6")


def _read(value: str, confidence: str, page: int, evidence: str = "verified") -> ExtractedValue:
    signals = Signals(self_report=Decimal(confidence), evidence=evidence, normaliser="exact")  # type: ignore[arg-type]
    return ExtractedValue(
        value=value,
        confidence=Decimal(confidence),
        page=page,
        evidence=f"No {value}",
        signals=signals,
    )


def test_settle_keeps_the_strongest_supporting_readings_provenance() -> None:
    settled = settle([_read("A", "0.8", 1), _read("A", "0.95", 2), _read("B", "0.6", 3)])
    assert (settled.value, settled.page, settled.evidence) == ("A", 2, "No A")
    assert settled.signals is not None
    assert settled.signals.self_report == Decimal("0.95")


def test_settle_prefers_readings_whose_evidence_checked_out() -> None:
    settled = settle([_read("A", "0.99", 1, "failed"), _read("B", "0.7", 2)])
    assert (settled.value, settled.page) == ("B", 2)
    only_failed = settle([_read("A", "0.99", 1, "failed")])
    assert only_failed.signals is not None
    assert only_failed.signals.evidence == "failed"  # kept, and zeroed when scored
