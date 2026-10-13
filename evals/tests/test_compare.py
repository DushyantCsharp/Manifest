"""The PR comment's deltas against main's last report (manifest_evals.compare)."""

import json
from pathlib import Path
from typing import Any

from manifest_evals import compare


def report(**metrics: Any) -> dict[str, Any]:
    gate = [
        {"metric": "field_accuracy", "bound": {"min": 0.95, "target": 0.96}},
        {"metric": "false_alarm_rate", "bound": {"max": 0.45, "target": 0.10}},
    ]
    for check in gate:
        value = metrics[check["metric"]]
        bound = check["bound"]
        check["value"] = value
        check["ok"] = value >= bound["min"] if "min" in bound else value <= bound["max"]
        target = bound["target"]
        check["on_target"] = value >= target if "min" in bound else value <= target
    return {
        "profile": "model",
        "sources": {"anthropic": 165},
        "calibrators": {"extraction": {"name": "extraction", "version": 2, "provenance": "model"}},
        "metrics": {**metrics, "pages": 165},
        "gate": gate,
        "fields": [
            {"document": "commercial_invoice", "field": "seller_name", "accuracy": "0.8364"}
        ],
        "reconciliation": {
            "missed": [],
            "false": [{"pack": "clean-0004", "finding": ["value_mismatch", "seller_name", None]}],
        },
    }


def test_changes_say_which_way_is_better() -> None:
    assert compare.change("0.9", "0.95", "up") == "+0.0500 better"
    assert compare.change("0.9", "0.85", "up") == "-0.0500 **worse**"
    assert compare.change("0.30", "0.25", "down") == "-0.0500 better"
    assert compare.change(11799, 12000) == "+201"
    assert compare.change("0.5", "0.5", "up") == "="
    assert compare.change(None, "0.5") == ""


def test_without_a_baseline_it_reports_the_run_alone() -> None:
    head = report(field_accuracy=0.967, false_alarm_rate=0.375)
    text = compare.comment(head, None, title="Replay evals", marker="replay")
    assert text.startswith("<!-- manifest-eval:replay -->\n### Replay evals: pass")
    assert "No report from main to compare with yet." in text
    assert "Below target (not gated): `false_alarm_rate`." in text
    assert "| false_alarm_rate |  | 0.3750 |  | max 0.45, target 0.1 | pass, below target |" in text


def test_against_main_it_shows_what_moved() -> None:
    base = report(field_accuracy=0.967, false_alarm_rate=0.375)
    head = report(field_accuracy=0.94, false_alarm_rate=0.20)
    head["fields"][0]["accuracy"] = "0.9818"
    head["reconciliation"]["false"] = [
        {"pack": "scan-0002", "finding": ["value_mismatch", "buyer_name", None]}
    ]
    head["calibrators"]["extraction"]["version"] = 3
    text = compare.comment(head, base, base_label="main @ abc1234")
    assert "### Evals: **FAIL**" in text
    assert "Gate failures: `field_accuracy`." in text
    assert (
        "| field_accuracy | 0.9670 | 0.9400 | -0.0270 **worse** | min 0.95, target 0.96 |" in text
    )
    assert "| false_alarm_rate | 0.3750 | 0.2000 | -0.1750 better |" in text
    assert "Calibrators: extraction-v2 (model) → extraction-v3 (model)." in text
    assert "| commercial_invoice | seller_name | 0.8364 | 0.9818 | +0.1454 better |" in text
    assert "false alarms: 1 new, 1 gone" in text
    assert "- gone: clean-0004: ['value_mismatch', 'seller_name', None]" in text
    assert "Other metrics" not in text  # pages didn't move


def test_a_profile_change_is_called_out() -> None:
    base = report(field_accuracy=1.0, false_alarm_rate=0.0)
    base["profile"] = "plumbing"
    head = report(field_accuracy=0.967, false_alarm_rate=0.375)
    assert "Profiles differ (plumbing on main)" in compare.comment(head, base)


def test_the_cli_reads_report_directories(tmp_path: Path) -> None:
    (tmp_path / "head").mkdir()
    head = report(field_accuracy=0.967, false_alarm_rate=0.375)
    (tmp_path / "head" / "report.json").write_text(json.dumps(head))
    output = tmp_path / "comment.md"
    arguments = [str(tmp_path / "head"), "--base", str(tmp_path / "missing")]
    assert compare.main([*arguments, "--output", str(output)]) == 0
    assert "No report from main" in output.read_text()
    assert compare.main([str(tmp_path / "missing")]) == 1
