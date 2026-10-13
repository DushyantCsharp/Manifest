"""Report metrics, checked against numbers worked out by hand."""

from decimal import Decimal

import pytest

from manifest_evals.metrics import (
    Scored,
    at,
    choose_threshold,
    curve,
    ece,
    f1,
    percentile,
    ratio,
    reliability,
)

D = Decimal


def s(confidence: str, correct: bool) -> Scored:
    return Scored(D(confidence), correct)


def test_reliability_bins_and_ece() -> None:
    samples = [s("0.95", True), s("0.95", True), s("0.95", False), s("1", True), s("0.15", False)]
    bins = reliability(samples)
    top, bottom = bins[-1], bins[1]
    assert (top.count, top.accuracy) == (4, D("0.75"))  # 1.0 lands in the last bin
    assert top.confidence == D("0.9625")
    assert (bottom.count, bottom.accuracy, bottom.confidence) == (1, D(0), D("0.15"))
    # ECE = 4/5 * |0.9625 - 0.75| + 1/5 * |0.15 - 0|
    assert ece(samples) == D(4) / 5 * D("0.2125") + D(1) / 5 * D("0.15")
    assert ece([]) is None


def test_a_calibrated_set_has_no_error() -> None:
    samples = [s("0.75", True)] * 3 + [s("0.75", False)]
    assert ece(samples) == 0


def test_curve_threshold_and_operating_point() -> None:
    samples = [s("0.99", True)] * 8 + [s("0.9", True), s("0.9", False), s("0.5", False)]
    points = curve(samples)
    counts = [(p.threshold, p.count) for p in points]
    assert counts == [(D("0.99"), 8), (D("0.9"), 10), (D("0.5"), 11)]
    assert points[1].precision == D("0.9")
    assert choose_threshold(samples, D("0.9")) == D("0.9")  # lowest that still reaches it
    assert choose_threshold(samples, D("0.95")) == D("0.99")
    assert choose_threshold([s("0.6", False)], D("0.9")) is None
    coverage, precision = at(samples, D("0.9"))
    assert (coverage, precision) == (D(10) / 11, D("0.9"))
    assert at(samples, None) == (0, None)


def test_percentile_is_nearest_rank() -> None:
    latencies = list(range(1, 101))
    assert percentile(latencies, 95) == 95
    assert percentile([7], 95) == 7
    assert percentile([], 95) is None


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((8, 10, 10), (D("0.8"), D("0.8"), D("0.8"))),
        ((0, 4, 5), (D(0), D(0), D(0))),
        ((0, 0, 5), (None, D(0), None)),
    ],
)
def test_f1(counts: tuple[int, int, int], expected: tuple[Decimal | None, ...]) -> None:
    assert f1(*counts) == expected
    assert ratio(1, 0) is None
