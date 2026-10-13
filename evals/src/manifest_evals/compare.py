"""An eval report against a baseline (main's last report), as Markdown for a PR comment.

    python -m manifest_evals.compare HEAD_DIR [--base BASE_DIR] [--base-label TEXT]
                                     [--title TEXT] [--marker NAME] [--output FILE]

Each directory holds a ``report.json`` written by ``python -m manifest_evals run``. The
comment leads with the gate verdict, then every gated metric with main's value, this run's and
the change (better or worse in the direction its bound points), then, folded away, the other
metrics that moved, per-field accuracy that moved, and reconciliation findings that appeared
or went away. It starts with an HTML marker so CI can find and update its own comment.

This only reports: the gate itself is ``manifest_evals run``'s exit code.
"""

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

Report = dict[str, Any]


def load(directory: Path | None) -> Report | None:
    """``directory/report.json``, or None when there is no such report (no baseline yet)."""
    if directory is None:
        return None
    path = directory / "report.json"
    if not path.is_file():
        return None
    report: Report = json.loads(path.read_text(encoding="utf-8"))
    return report


def number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def fmt(value: Any) -> str:
    n = number(value)
    if n is None:
        return "n/a" if value is None else str(value)
    if n == n.to_integral_value() and abs(n) >= 10:
        return str(int(n))
    return f"{n:.4f}" if abs(n) < 10 else f"{n:.2f}"


def change(base: Any, head: Any, better: str | None = None) -> str:
    """``head - base``, signed, and whether that is better or worse (``better``: up | down)."""
    b, h = number(base), number(head)
    if b is None or h is None:
        return ""
    delta = h - b
    if delta == 0:
        return "="
    sign = "+" if delta > 0 else "-"
    text = sign + fmt(abs(delta))
    if better is not None:
        improved = (delta > 0) == (better == "up")
        text += " better" if improved else " **worse**"
    return text


def _direction(bound: dict[str, Any]) -> str:
    return "up" if "min" in bound else "down"


def _bound(bound: dict[str, Any]) -> str:
    parts = [f"{key} {value}" for key, value in bound.items() if key in ("min", "max")]
    if "target" in bound:
        parts.append(f"target {bound['target']}")
    return ", ".join(parts)


def _verdict(check: dict[str, Any]) -> str:
    verdict = "pass" if check["ok"] else "**FAIL**"
    if check.get("on_target") is False:
        verdict += ", below target"
    return verdict


def _findings(report: Report | None, kind: str) -> set[str]:
    if report is None:
        return set()
    rows = report.get("reconciliation", {}).get(kind, [])
    return {f"{row['pack']}: {row.get('finding') or row.get('discrepancy')}" for row in rows}


def _fields(report: Report | None) -> dict[tuple[str, str], Any]:
    if report is None:
        return {}
    return {(row["document"], row["field"]): row["accuracy"] for row in report.get("fields", [])}


def _calibrators(report: Report | None) -> str:
    if report is None:
        return ""
    found = report.get("calibrators", {}).values()
    return ", ".join(f"{c['name']}-v{c['version']} ({c.get('provenance')})" for c in found)


def comment(
    head: Report,
    base: Report | None,
    *,
    title: str = "Evals",
    base_label: str = "main",
    marker: str = "eval",
) -> str:
    gate = head.get("gate", [])
    failed = [check["metric"] for check in gate if not check["ok"]]
    below = [check["metric"] for check in gate if check.get("on_target") is False]
    head_metrics = head.get("metrics", {})
    base_metrics = base.get("metrics", {}) if base else {}

    lines = [
        f"<!-- manifest-eval:{marker} -->",
        f"### {title}: " + ("**FAIL**" if failed else "pass"),
    ]
    answered = ", ".join(f"{k} ({v})" for k, v in sorted(head.get("sources", {}).items()))
    lines += ["", f"Profile **{head.get('profile')}**; answers from {answered or 'n/a'}."]
    if failed:
        lines.append(f"Gate failures: {', '.join(f'`{m}`' for m in failed)}.")
    if below:
        lines.append(f"Below target (not gated): {', '.join(f'`{m}`' for m in below)}.")
    if base is None:
        lines.append(f"No report from {base_label} to compare with yet.")
    else:
        lines.append(f"Compared with {base_label}.")
        if base.get("profile") != head.get("profile"):
            lines.append(
                f"> Profiles differ ({base.get('profile')} on {base_label}): the answers "
                "come from different sources, so the deltas mix model and plumbing numbers."
            )
        if _calibrators(base) != _calibrators(head):
            lines.append(f"Calibrators: {_calibrators(base)} → {_calibrators(head)}.")

    lines += [
        "",
        f"| metric | {base_label} | this run | change | gate | |",
        "|---|---|---|---|---|---|",
    ]
    gated = set()
    for check in gate:
        name = check["metric"]
        gated.add(name)
        before = base_metrics.get(name)
        moved = change(before, check["value"], _direction(check["bound"])) if base else ""
        lines.append(
            f"| {name} | {fmt(before) if base else ''} | {fmt(check['value'])} | {moved} "
            f"| {_bound(check['bound'])} | {_verdict(check)} |"
        )

    others = [
        (name, base_metrics.get(name), value)
        for name, value in head_metrics.items()
        if name not in gated and (base is None or change(base_metrics.get(name), value) != "=")
    ]
    if others:
        heading = "Other metrics" if base is None else "Other metrics that moved"
        lines += ["", f"<details><summary>{heading} ({len(others)})</summary>", ""]
        lines += [f"| metric | {base_label} | this run | change |", "|---|---|---|---|"]
        for name, before, value in others:
            shown = fmt(before) if base else ""
            lines.append(f"| {name} | {shown} | {fmt(value)} | {change(before, value)} |")
        lines += ["", "</details>"]

    if base is not None:
        before_fields, after_fields = _fields(base), _fields(head)
        moved_fields = [
            (key, before_fields.get(key), value)
            for key, value in after_fields.items()
            if change(before_fields.get(key), value) != "="
        ]
        if moved_fields:
            lines += [
                "",
                f"<details><summary>Field accuracy that moved ({len(moved_fields)})</summary>",
                "",
                f"| document | field | {base_label} | this run | change |",
                "|---|---|---|---|---|",
            ]
            for (document, field), before, value in moved_fields:
                moved = change(before, value, "up")
                lines.append(f"| {document} | {field} | {fmt(before)} | {fmt(value)} | {moved} |")
            lines += ["", "</details>"]

        for kind, label in (("false", "false alarms"), ("missed", "missed discrepancies")):
            appeared = sorted(_findings(head, kind) - _findings(base, kind))
            resolved = sorted(_findings(base, kind) - _findings(head, kind))
            if appeared or resolved:
                lines += [
                    "",
                    f"<details><summary>Reconciliation {label}: {len(appeared)} new, "
                    f"{len(resolved)} gone</summary>",
                    "",
                ]
                lines += [f"- new: {item}" for item in appeared]
                lines += [f"- gone: {item}" for item in resolved]
                lines += ["", "</details>"]

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manifest_evals.compare", description=__doc__)
    parser.add_argument("head", type=Path, help="directory with this run's report.json")
    parser.add_argument("--base", type=Path, help="directory with the baseline's report.json")
    parser.add_argument("--base-label", default="main")
    parser.add_argument("--title", default="Evals")
    parser.add_argument("--marker", default="eval", help="names the comment CI updates")
    parser.add_argument("--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)

    head = load(args.head)
    if head is None:
        print(f"error: no report.json in {args.head}", file=sys.stderr)
        return 1
    text = comment(
        head, load(args.base), title=args.title, base_label=args.base_label, marker=args.marker
    )
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
