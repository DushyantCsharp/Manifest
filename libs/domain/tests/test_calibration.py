"""Isotonic calibration: monotone, bounded, order-blind, smoothed; artifacts round-trip."""

import random
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from manifest_domain import calibration
from manifest_domain.calibration import (
    Calibrator,
    FitSummary,
    Knot,
    dumps,
    fit_isotonic,
    load,
    loads,
    versions,
)
from manifest_domain.confidence import CEILING, FLOOR

scores = st.decimals(min_value=0, max_value=1, places=3, allow_nan=False)
samples = st.lists(st.tuples(scores, st.booleans()), min_size=1, max_size=60)


def calibrator(knots: tuple[Knot, ...], **changes: object) -> Calibrator:
    fields: dict[str, object] = {
        "name": "extraction",
        "version": 1,
        "provenance": "model",
        "score_version": 1,
        "schema_version": 3,
        "prompt_version": "test",
        "model": "anthropic/claude-test",
        "knots": knots,
        "fitted_on": FitSummary(split="calibration", items=1, samples=10, positives=9),
    }
    return Calibrator.model_validate(fields | changes)


@given(samples)
def test_fit_is_monotone_and_bounded(data: list[tuple[Decimal, bool]]) -> None:
    knots = fit_isotonic(data)
    assert all(a[0] < b[0] and a[1] <= b[1] for a, b in pairwise(knots))
    assert all(0 < y < 1 for _, y in knots)
    fitted = calibrator(knots)
    probe = sorted({x for x, _ in data} | {Decimal(0), Decimal(1)})
    outputs = [fitted.apply(x) for x in probe]
    assert outputs == sorted(outputs)
    assert all(FLOOR <= y <= CEILING for y in outputs)


@given(samples, st.randoms(use_true_random=False))
def test_fit_ignores_sample_order(data: list[tuple[Decimal, bool]], rng: random.Random) -> None:
    shuffled = list(data)
    rng.shuffle(shuffled)
    assert fit_isotonic(shuffled) == fit_isotonic(data)


def test_violators_pool_and_blocks_are_smoothed() -> None:
    # Right at 0.6, wrong at 0.7, right twice at 0.9: 0.6 and 0.7 pool into one block.
    data = [
        (Decimal("0.6"), True),
        (Decimal("0.7"), False),
        (Decimal("0.9"), True),
        (Decimal("0.9"), True),
    ]
    assert fit_isotonic(data) == (
        (Decimal("0.650000"), Decimal("0.500000")),  # (1 + 1) / (2 + 2)
        (Decimal("0.900000"), Decimal("0.750000")),  # (2 + 1) / (2 + 2)
    )


def test_smoothing_cannot_invert_the_order() -> None:
    # 0/1 smooths to 1/3 and 1/10 to 2/12: unpooled, the higher score would rank lower.
    data = [(Decimal("0.5"), False)] + [(Decimal("0.8"), i == 0) for i in range(10)]
    knots = fit_isotonic(data)
    assert [y for _, y in knots] == sorted(y for _, y in knots)
    assert len(knots) == 1


def test_a_perfect_record_is_not_certainty() -> None:
    [(x, y)] = fit_isotonic([(Decimal("0.97"), True)] * 40)
    assert (x, y) == (Decimal("0.970000"), (Decimal(41) / Decimal(42)).quantize(Decimal("1e-6")))


def test_apply_interpolates_and_clamps() -> None:
    fitted = calibrator(((Decimal("0.5"), Decimal("0.2")), (Decimal("0.9"), Decimal("0.8"))))
    assert fitted.apply(Decimal("0.1")) == Decimal("0.2")
    assert fitted.apply(Decimal("0.7")) == Decimal("0.5")
    assert fitted.apply(Decimal("1")) == Decimal("0.8")
    certain = calibrator(((Decimal("0.5"), Decimal(1)),))
    assert certain.apply(Decimal("0.9")) == CEILING


def test_knots_must_be_monotone() -> None:
    with pytest.raises(ValueError, match="knots"):
        calibrator(((Decimal("0.5"), Decimal("0.8")), (Decimal("0.9"), Decimal("0.2"))))
    with pytest.raises(ValueError, match="no samples"):
        fit_isotonic([])


def test_only_model_fits_are_trusted() -> None:
    knots = ((Decimal("0.5"), Decimal("0.5")),)
    assert calibrator(knots).trusted
    assert not calibrator(knots, provenance="oracle").trusted
    assert calibrator(knots).label == "extraction-v1"


def test_artifacts_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fitted = calibrator(
        fit_isotonic([(Decimal("0.9"), True), (Decimal("0.4"), False)]),
        threshold=Decimal("0.95"),
        metrics={"ece_after": Decimal("0.0123")},
    )
    assert loads(dumps(fitted)) == fitted
    assert dumps(loads(dumps(fitted))) == dumps(fitted)  # canonical
    monkeypatch.setattr(calibration, "_artifacts", lambda: tmp_path)
    assert versions("extraction") == []
    for version in (2, 1):
        (tmp_path / f"extraction-v{version}.json").write_text(dumps(fitted))
    (tmp_path / "hs-v1.json").write_text(dumps(fitted))
    assert versions("extraction") == [1, 2]
    assert load("extraction", 2) == fitted
    with pytest.raises(FileNotFoundError):
        load("extraction", 3)
