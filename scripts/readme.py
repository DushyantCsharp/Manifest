"""Regenerate README.md's measured sections from the latest reports (`make readme`).

    uv run python scripts/readme.py            rewrite the generated blocks in README.md
    uv run python scripts/readme.py --check    exit 1 if README.md doesn't match the reports

Blocks between ``<!-- generated:NAME -->`` and ``<!-- /generated:NAME -->``:

    targets   targets vs measured: the eval gate (evals/reports/latest/report.json against
              evals/thresholds.yaml, `make eval`) and the latest load test
              (tools/loadtest/reports/<run>.json against tools/loadtest/targets.yaml)
    cost      tokens and USD per unit of work, at the price sheet's date
    tenx      measured capacity with and without HS, and what 10x the design load needs

Each block names the reports it came from: the commit they measured and when.
"""

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from manifest_llm.pricing import CHECKED, PRICES

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
EVAL_REPORT = ROOT / "evals" / "reports" / "latest" / "report.json"
LOADTEST_REPORTS = ROOT / "tools" / "loadtest" / "reports"
LOADTEST_TARGETS = ROOT / "tools" / "loadtest" / "targets.yaml"
ROUTED_SHIPMENTS = ("shipments_needs_review", "shipments_auto_approved", "shipments_approved")

EVAL_LABELS = {
    "field_accuracy": "Field accuracy, all packs",
    "field_accuracy_clean": "Field accuracy, clean PDFs",
    "field_accuracy_scanned": "Field accuracy, scans (OCR)",
    "line_item_f1": "Line-item F1",
    "reconciliation_recall": "Cross-document mismatches caught",
    "false_alarm_rate": "Packs with a false mismatch",
    "ece": "Calibration error (ECE), extraction",
    "coverage_at_threshold": "Fields auto-approvable at the threshold",
    "precision_at_threshold": "Precision of those fields",
    "shipment_precision": "Auto-approved shipments that were right",
    "p95_latency_ms": "Model call per page, p95",
    "usd_per_page": "Cost per page",
    "hs_recall_at_20": "HS: right code among 20 retrieved",
    "hs_accuracy_2": "HS accuracy, chapter (2 digits)",
    "hs_accuracy_4": "HS accuracy, heading (4 digits)",
    "hs_accuracy_6": "HS accuracy, subheading (6 digits)",
    "hs_ece": "Calibration error (ECE), HS",
    "hs_p95_latency_ms": "HS walk per line, p95",
    "hs_usd_per_description": "Cost per HS line",
}
LOAD_LABELS = {
    "api_p95_ms": "API p95, every endpoint",
    "pack_p50_seconds": "Pack: first finalize to routed, p50",
    "pack_p95_seconds": "Pack: first finalize to routed, p95",
    "page_p95_seconds": "Page extraction attempt, p95",
    "throughput_pages_per_minute": "Throughput, one worker host",
    "api_error_rate": "API 5xx rate",
    "failed_pages": "Failed pages",
    "unrouted_packs": "Packs not routed",
    "dlq_growth": "Dead-letter growth",
    "cost_per_page_usd": "Cost per page (extraction)",
    "cost_per_pack_usd": "Cost per pack (everything)",
}
PERCENT = {
    "field_accuracy", "field_accuracy_clean", "field_accuracy_scanned", "line_item_f1",
    "reconciliation_recall", "false_alarm_rate", "coverage_at_threshold",
    "precision_at_threshold", "shipment_precision", "hs_recall_at_20", "hs_accuracy_2",
    "hs_accuracy_4", "hs_accuracy_6", "api_error_rate",
}  # fmt: skip


def value(metric: str, raw: Any) -> str:
    if raw is None:
        return "n/a"
    number = float(raw)
    if metric in PERCENT:
        return f"{number * 100:.1f}%"
    if "usd" in metric:
        return f"${number:.4f}"
    if metric.endswith("_ms"):
        return f"{number / 1000:.1f} s" if number >= 1000 else f"{number:.0f} ms"
    if metric.endswith("_seconds"):
        return f"{number:.1f} s"
    if metric in ("ece", "hs_ece"):
        return f"{number:.3f}"
    if "per_minute" in metric:
        return f"{number:.1f} pages/min"
    return f"{number:g}"


def bound(metric: str, limit: Any, sense: str) -> str:
    return f"{'≥' if sense == 'min' else '≤'} {value(metric, limit)}"


def stamp(report: dict[str, Any]) -> str:
    commit = report.get("commit")
    if not commit:
        return "commit not recorded"
    dirty = " + uncommitted changes" if report.get("dirty") else ""
    when = str(report.get("generated_at") or "")[:10]
    return f"commit `{commit[:7]}`{dirty}, {when}"


def load_tests() -> list[dict[str, Any]]:
    """Every load-test report, newest first."""
    reports = [json.loads(p.read_text()) for p in LOADTEST_REPORTS.glob("*.json")]
    return sorted(reports, key=lambda r: str(r["run"]), reverse=True)


def with_hs(report: dict[str, Any]) -> bool:
    return bool(report["totals"].get("cost_by_stage", {}).get("hs_classify"))


def shipments(report: dict[str, Any]) -> int:
    database = report.get("database", {})
    return int(sum(database.get(k, 0) for k in ROUTED_SHIPMENTS))


def capacity(report: dict[str, Any]) -> float:
    return float(max(s["pages"] / ((s["end"] - s["start"]) / 60) for s in report["steps"]))


def targets_block(evals: dict[str, Any], load: dict[str, Any]) -> str:
    thresholds = yaml.safe_load((ROOT / "evals" / "thresholds.yaml").read_text())
    profile = thresholds["profiles"][evals["profile"]]
    lines = [
        f"_Generated by `make readme` from the eval report ({stamp(evals)}, profile "
        f"`{evals['profile']}`) and load test `{load['run']}` ({stamp(load)})._",
        "",
        "**Accuracy and calibration** (evals: real Sonnet 5.5 answers, replayed; "
        "`evals/thresholds.yaml`)",
        "",
        "| Metric | Target | CI gate | Measured | |",
        "|---|---|---|---|---|",
    ]
    for check in evals["gate"]:
        metric = check["metric"]
        limits = profile.get(metric, {})
        sense = "min" if "min" in limits else "max"
        gate = bound(metric, limits.get(sense), sense)
        target = bound(metric, limits["target"], sense) if "target" in limits else gate
        if not check["ok"]:
            verdict = "**fails gate**"
        elif check.get("on_target") is False:
            verdict = "below target"
        else:
            verdict = "met"
        label = EVAL_LABELS.get(metric, metric)
        lines.append(
            f"| {label} | {target} | {gate} | {value(metric, check['value'])} | {verdict} |"
        )
    load_targets = yaml.safe_load(LOADTEST_TARGETS.read_text())
    hs = "HS on" if with_hs(load) else "HS off"
    lines += [
        "",
        f"**Throughput, latency, cost** (load test `{load['run']}`, {hs}, "
        "`tools/loadtest/targets.yaml`)",
        "",
        "| Metric | Target | Measured | |",
        "|---|---|---|---|",
    ]
    for check in load["checks"]:
        metric = check["metric"]
        limits = load_targets.get(metric, check["bound"])
        sense = "min" if "min" in limits else "max"
        where = f" ({check['where']})" if check.get("where") else ""
        label = LOAD_LABELS.get(metric, metric) + where
        target = bound(metric, limits[sense], sense)
        verdict = "met" if check["ok"] else "**missed**"
        lines.append(f"| {label} | {target} | {value(metric, check['value'])} | {verdict} |")
    return "\n".join(lines)


def cost_block(evals: dict[str, Any], load: dict[str, Any]) -> str:
    m = evals["metrics"]
    totals = load["totals"]
    packs = shipments(load)
    by_stage = totals.get("tokens_by_stage", {})
    pack_in = sum(v for k, v in by_stage.items() if k.endswith("/input"))
    pack_out = sum(v for k, v in by_stage.items() if k.endswith("/output"))
    hs_lines = load.get("database", {}).get("hs_suggestions", 0) / packs if packs else 0
    rows: list[tuple[str, str, Any, Any, Any, str]] = [
        (
            "Page extracted",
            "Sonnet 5.5, effort low",
            m.get("input_tokens_per_page"),
            m.get("output_tokens_per_page"),
            m.get("usd_per_page"),
            f"evals, {m.get('pages')} pages",
        ),
        (
            "HS line classified (chapter, heading, subheading)",
            "Sonnet 5.5, effort medium",
            m.get("hs_input_tokens_per_description"),
            m.get("hs_output_tokens_per_description"),
            m.get("hs_usd_per_description"),
            f"evals, {m.get('hs_descriptions')} lines",
        ),
    ]
    if packs and pack_in:
        rows.append(
            (
                f"Shipment pack (3 pages, {hs_lines:.1f} HS lines on average)",
                "both",
                pack_in / packs,
                pack_out / packs,
                totals["cost_usd"] / packs,
                f"load test `{load['run']}`, {packs} packs",
            )
        )
    lines = [
        f"_Generated by `make readme` from the eval report ({stamp(evals)}) and load test "
        f"`{load['run']}` ({stamp(load)}); prices as of {CHECKED.isoformat()} "
        "(`manifest_llm.pricing`)._",
        "",
        "| Unit of work | Model | Input tokens | Output tokens | USD | Measured on |",
        "|---|---|---|---|---|---|",
    ]
    for unit, model, tokens_in, tokens_out, usd, source in rows:
        tin = f"{float(tokens_in):,.0f}" if tokens_in is not None else "n/a"
        tout = f"{float(tokens_out):,.0f}" if tokens_out is not None else "n/a"
        cost = f"${float(usd):.4f}" if usd is not None else "n/a"
        lines.append(f"| {unit} | {model} | {tin} | {tout} | {cost} | {source} |")
    lines += [
        "",
        f"Price sheet as of {CHECKED.isoformat()} (USD per million tokens; standard tier, no "
        "caching or batch discounts):",
        "",
        "| Model | Input | Output |",
        "|---|---|---|",
    ]
    for model in ("claude-sonnet-5-5", "voyage-3.5"):
        price = PRICES[model]
        output = f"${price.output}" if price.output else "n/a"
        lines.append(f"| {model} | ${price.input} | {output} |")
    lines += [
        "",
        "Output tokens include the model's thinking. The HS query embedding (voyage-3.5, about "
        "ten tokens per line) costs under $0.000001.",
    ]
    return "\n".join(lines)


def tenx_block(reports: list[dict[str, Any]]) -> str:
    hs_on = next((r for r in reports if with_hs(r)), None)
    hs_off = next((r for r in reports if not with_hs(r)), None)
    targets = yaml.safe_load(LOADTEST_TARGETS.read_text())
    design = float(targets["throughput_pages_per_minute"]["min"])
    lines = [
        "_Generated by `make readme` from the latest load tests with and without HS._",
        "",
        "| Load test | Capacity | Pack p50 at 2 users | Worker processes |",
        "|---|---|---|---|",
    ]
    for label, report in (("HS on", hs_on), ("HS off", hs_off)):
        if report is None:
            continue
        first = sorted(report["steps"][0]["packs"])
        p50 = first[len(first) // 2] if first else None
        lines.append(
            f"| {label}: `{report['run']}` ({stamp(report)}) | "
            f"{capacity(report):.0f} pages/min | {value('pack_p50_seconds', p50)} | "
            f"{report['args']['concurrency']} |"
        )
    if hs_on is not None and shipments(hs_on):
        per_process = capacity(hs_on) / hs_on["args"]["concurrency"]
        tenx = design * 10
        packs_per_minute = tenx / 3
        packs = shipments(hs_on)
        tokens = sum(hs_on["totals"].get("tokens", {}).values()) / packs
        spend = Decimal(str(hs_on["totals"]["cost_usd"])) / packs
        lines += [
            "",
            f"At 10x the design load ({tenx:.0f} pages, {packs_per_minute:.0f} packs a minute), "
            "extrapolated from the HS-on run:",
            "",
            f"- **Worker processes:** {per_process:.1f} pages a minute each, so about "
            f"{tenx / per_process:.0f} processes with the HS stage as it is.",
            f"- **Model tokens:** {tokens:,.0f} per pack, so about "
            f"{tokens * packs_per_minute / 1_000_000:.1f}M a minute against the provider's "
            "rate limits.",
            f"- **Spend:** ${spend:.3f} per pack, so about "
            f"${float(spend) * packs_per_minute * 60 * 24:,.0f} a day at list prices.",
        ]
    return "\n".join(lines)


def render(text: str, blocks: dict[str, str]) -> str:
    for name, body in blocks.items():
        pattern = re.compile(
            rf"(<!-- generated:{name} -->\n)(?:.*?\n)?(<!-- /generated:{name} -->)", re.DOTALL
        )
        if not pattern.search(text):
            raise SystemExit(f"README.md has no generated:{name} block")
        text = pattern.sub(
            lambda match, body=body: match.group(1) + body + "\n" + match.group(2), text
        )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="fail if README.md is out of date")
    args = parser.parse_args()
    if not EVAL_REPORT.exists():
        raise SystemExit("no eval report: run `make eval` first")
    evals = json.loads(EVAL_REPORT.read_text())
    reports = load_tests()
    if not reports:
        raise SystemExit("no load-test report: run `make loadtest` first")
    # The headline tables describe the whole pipeline, so prefer the newest run with HS on.
    headline = next((r for r in reports if with_hs(r)), reports[0])
    blocks = {
        "targets": targets_block(evals, headline),
        "cost": cost_block(evals, headline),
        "tenx": tenx_block(reports),
    }
    current = README.read_text()
    updated = render(current, blocks)
    if args.check:
        if updated != current:
            print("README.md is out of date: run `make readme`", file=sys.stderr)
            return 1
        return 0
    README.write_text(updated)
    print("README.md: regenerated " + ", ".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
