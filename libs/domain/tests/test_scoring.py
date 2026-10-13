"""The scoring recipe: signals in, a raw score out; evidence gates the calibrated result."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from manifest_domain.extraction import Evidence, ExtractedValue, Signals
from manifest_domain.scoring import AMBIGUITY_PENALTY, agreed, final, page_score

confidences = st.decimals(min_value=0, max_value=1, places=4, allow_nan=False)
likely = st.decimals(min_value="0.5", max_value=1, places=4, allow_nan=False)


def value(raw: str, evidence: Evidence = "verified", text: str | None = "X") -> ExtractedValue:
    signals = Signals(self_report=Decimal(raw), evidence=evidence, normaliser="exact")
    return ExtractedValue(
        value=text, confidence=Decimal(raw), page=1, evidence="X", signals=signals
    )


def test_page_score_applies_the_normaliser_factor() -> None:
    assert page_score(Decimal("0.9"), "exact") == Decimal("0.9")
    assert page_score(Decimal("0.9"), "ambiguous") == Decimal("0.9") * AMBIGUITY_PENALTY
    assert page_score(Decimal("0.9"), "failed") == 0
    with pytest.raises(ValueError, match="not in"):
        page_score(Decimal("1.2"), "exact")


@given(confidences, st.lists(likely, max_size=3))
def test_agreement_never_lowers_a_reading(raw: Decimal, others: list[Decimal]) -> None:
    result = agreed(raw, others)
    assert 0 <= result <= 1
    if not others:
        assert result == raw
    elif Decimal("0.0001") < raw < Decimal("0.9999"):
        assert result >= raw - Decimal("0.0001")  # storage rounds down a hair at most


def test_agreement_raises_confidence() -> None:
    assert agreed(Decimal("0.9"), [Decimal("0.9")]) > Decimal("0.98")


def test_final_calibrates_then_gates_on_evidence() -> None:
    def halve(raw: Decimal) -> Decimal:
        return raw / 2

    assert final(value("0.9"), halve) == Decimal("0.45")
    assert final(value("0.9"), None) == Decimal("0.9")  # no calibrator: raw, unrouted
    assert final(value("0.9", evidence="failed"), lambda _: Decimal("0.99")) == 0
    assert final(value("0.9", text=None), halve) == 0
