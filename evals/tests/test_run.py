"""The eval harness end to end on the committed corpus (replayed, so no infrastructure)."""

from decimal import Decimal
from pathlib import Path

import pytest

from manifest_domain import calibration
from manifest_evals import run as harness
from manifest_evals.oracle import NAME as ORACLE


@pytest.fixture(scope="module")
def evaluated() -> harness.Run:
    return harness.evaluate()


def test_the_oracle_corpus_runs_under_the_plumbing_profile(evaluated: harness.Run) -> None:
    assert evaluated.profile == "plumbing"
    assert set(evaluated.sources) == {ORACLE, f"{ORACLE} (OCR)"}
    assert sum(evaluated.sources.values()) == 165  # 55 packs x 3 single-page documents
    m = evaluated.metrics
    assert m["field_accuracy_clean"] == m["field_accuracy_mismatch"] == 1
    assert 0 < m["field_accuracy_scanned"] < 1
    assert m["reconciliation_recall"] == 1
    assert m["false_alarm_rate_clean"] == 0
    assert m["ece"] < m["ece_before"]  # calibration helps on the held-out split
    assert m["pages"] == 165


def test_the_committed_calibrator_is_this_refit_and_never_trusted(evaluated: harness.Run) -> None:
    assert harness.drift(evaluated) is None, "run `make calibrate` and commit the calibrator"
    fitted = evaluated.calibrator
    assert (fitted.provenance, fitted.trusted) == ("oracle", False)
    assert fitted.threshold is not None
    assert fitted.fitted_on.split == "calibration"
    assert fitted.fitted_on.items == 28
    assert calibration.load(fitted.name, fitted.version) == fitted


def test_the_gate_reads_thresholds_yaml(
    evaluated: harness.Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert all(check["ok"] for check in harness.gate(evaluated))
    strict = tmp_path / "thresholds.yaml"
    strict.write_text(
        "target_precision: 0.99\nprofiles:\n  plumbing:\n"
        "    field_accuracy_scanned: {min: 0.999}\n"
        "    ece: {max: 1}\n"
        "    no_such_metric: {min: 0}\n"
    )
    monkeypatch.setattr(harness, "THRESHOLDS", strict)
    verdicts = {check["metric"]: check["ok"] for check in harness.gate(evaluated)}
    assert verdicts == {"field_accuracy_scanned": False, "ece": True, "no_such_metric": False}


def test_a_stale_calibrator_is_drift(evaluated: harness.Run) -> None:
    current = evaluated.calibrator
    stale = current.model_copy(update={"threshold": Decimal("0.42")})
    args = (evaluated.results, evaluated.sources, evaluated.target, {"extraction": current})
    run = harness.Run(*args, {"extraction": stale})
    assert "no longer matches a refit" in (harness.drift(run) or "")
    missing = harness.Run(*args, {"extraction": None})
    assert "no extraction calibrator" in (harness.drift(missing) or "")


def test_hs_is_measured_and_calibrated(evaluated: harness.Run) -> None:
    m = evaluated.metrics
    assert m["hs_descriptions"] == 120
    assert m["hs_accuracy_2"] >= m["hs_accuracy_4"] >= m["hs_accuracy_6"]
    assert m["hs_accuracy_6"] <= m["hs_recall_at_20"]  # the walk only chooses retrieved codes
    assert m["hs_ece"] < m["hs_ece_before"]
    hs = evaluated.fitted["hs"]
    assert (hs.name, hs.trusted) == ("hs", False)  # offline retrieval and a stand-in model


def test_the_report(evaluated: harness.Run, tmp_path: Path) -> None:
    directory = harness.write_report(evaluated, harness.gate(evaluated), tmp_path / "report")
    for chart in ("reliability-extraction.png", "reliability-hs.png", "precision-coverage.png"):
        assert (directory / chart).read_bytes().startswith(b"\x89PNG")
    markdown = (directory / "report.md").read_text()
    assert "ground-truth oracle" in markdown
    assert "| reconciliation_recall | 1" in markdown
    assert '"profile": "plumbing"' in (directory / "report.json").read_text()


def test_labelled_discrepancies_use_extraction_field_names() -> None:
    label = {
        "discrepancies": [
            {"kind": "value_mismatch", "field": "buyer.name", "line_no": None},
            {"kind": "missing_field", "field": "line_items.country_of_origin", "line_no": 2},
            {"kind": "value_mismatch", "field": "invoice_number", "line_no": 3},
        ]
    }
    assert harness.expected_findings(label) == {
        ("value_mismatch", "buyer_name", None),
        ("missing_field", "line_items.country_of_origin", 2),
        ("value_mismatch", "invoice_number", None),
    }
