"""Load test: the system as the release images run it, under Locust, against the targets.

    uv run python scripts/loadtest.py [--users 2,4,8,16,32] [--step 120] [--concurrency 8]
    (make loadtest)

Needs Docker; starts the local stack's services and the app containers itself.

1. Cassettes: every text-layer pack's recorded model answers (``evals/cassettes``, real
   Sonnet 5.5 recordings), merged into ``.loadtest/cassettes``.
2. The app: the api, worker and monitor images (``infra/compose.yaml`` profile ``app``) with
   ``infra/compose.loadtest.yaml``: the worker replays those answers, each taking as long as
   the recorded call did, so model latency and token counts (hence cost) are real while
   nothing is called or paid for; HS classification is off (no recordings for these lines).
   A fresh ``loadtest.run`` resource attribute marks this run's metrics.
3. Locust (``tools/loadtest``): users step up, each submitting a pack and waiting for it to be
   routed before sending the next.
4. Drain: until the stage queues are empty, then one more export and scrape.
5. Report: Prometheus (pages, page latency, cost, tokens, rate limiter, queue and DLQ depth),
   Locust (API and pack latency) and the database (a cross-check of the counts), against
   ``tools/loadtest/targets.yaml``, in ``tools/loadtest/reports/<run>.md`` and ``.json``.
"""

import argparse
import base64
import csv
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".loadtest"
TOOL = ROOT / "tools" / "loadtest"
REPORTS = TOOL / "reports"
COMPOSE = [
    "docker", "compose", "-f", str(ROOT / "infra/compose.yaml"),
    "-f", str(ROOT / "infra/compose.loadtest.yaml"), "--profile", "app",
]  # fmt: skip
PROMETHEUS = "http://127.0.0.1:9090"
RABBITMQ_QUEUES = "http://127.0.0.1:15672/api/queues/%2F"
RABBITMQ_AUTH = "Basic " + base64.b64encode(b"manifest:manifest").decode()  # the local stack's
API = "http://127.0.0.1:8010"
STAGES = ("classify", "page_extract", "reconcile", "hs_classify")
ROUTED = {"needs_review", "auto_approved", "approved"}


# ---------------------------------------------------------------- running


def cassettes() -> Path:
    """Every text-layer pack's recordings in one folder (keys are request hashes)."""
    target = WORK / "cassettes"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    for folder in sorted((ROOT / "evals" / "cassettes").iterdir()):
        if folder.name.startswith(("clean-", "mism-")):
            for recording in folder.glob("*.json"):
                shutil.copy2(recording, target / recording.name)
    return target


def start_app(run: str, concurrency: int, cassette_dir: Path) -> None:
    env = {
        **os.environ,
        "LOADTEST_CASSETTES": str(cassette_dir),
        "MANIFEST_ENVIRONMENT": "loadtest",
        "OTEL_RESOURCE_ATTRIBUTES": f"loadtest.run={run}",
        "WORKER_CONCURRENCY": str(concurrency),
    }
    subprocess.run([*COMPOSE, "up", "-d", "--build", "--wait"], env=env, check=True)


def wait_healthy(timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{API}/healthz", timeout=2):  # noqa: S310 - local API
                return
        except OSError:
            time.sleep(1)
    raise SystemExit("the API did not become healthy")


def queues_empty() -> bool:
    request = urllib.request.Request(RABBITMQ_QUEUES, headers={"Authorization": RABBITMQ_AUTH})
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - local broker
        rows = {row["name"]: row for row in json.load(response)}
    return all(not rows.get(stage, {}).get("messages") for stage in STAGES)


def drain(timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not queues_empty():
        time.sleep(5)
    time.sleep(35)  # one more metric export (15 s) and scrape (15 s)


def locust(users: str, step: int, csv_prefix: Path, results: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env |= {
        "LOADTEST_USERS": users,
        "LOADTEST_STEP_SECONDS": str(step),
        "LOADTEST_RESULTS": str(results),
    }
    command = [
        "uv", "run", "--project", str(TOOL), "--locked", "locust",
        "-f", str(TOOL / "locustfile.py"), "--headless", "--host", API,
        "--csv", str(csv_prefix), "--only-summary",
    ]  # fmt: skip
    subprocess.run(command, env=env, check=True)


# ---------------------------------------------------------------- measuring


def prometheus(query: str, at: float) -> list[dict[str, Any]]:
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": at})
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - local Prometheus
        result: list[dict[str, Any]] = json.load(response)["data"]["result"]
    return result


def scalar(query: str, at: float) -> float | None:
    found = prometheus(query, at)
    if not found:
        return None
    value = float(found[0]["value"][1])
    return None if math.isnan(value) else value


def by(query: str, at: float, label: str) -> dict[str, float]:
    return {row["metric"].get(label, ""): float(row["value"][1]) for row in prometheus(query, at)}


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


@dataclass
class Step:
    users: int
    start: float
    end: float
    pages: float = 0.0
    packs: list[float] = field(default_factory=list)
    unrouted: int = 0
    page_p95: float | None = None
    max_queue: dict[str, float] = field(default_factory=dict)

    @property
    def pages_per_minute(self) -> float:
        minutes = (self.end - self.start) / 60
        return self.pages / minutes if minutes else 0.0


def page_quantile(q: float, run: str, window: str, at: float) -> float | None:
    buckets = f'page_latency_seconds_bucket{{loadtest_run="{run}", status="extracted"}}'
    return scalar(f"histogram_quantile({q}, sum by (le) (increase({buckets}[{window}])))", at)


def measure(
    run: str,
    window: tuple[float, float, float],
    users: list[int],
    step: int,
    out: Path,
) -> dict[str, Any]:
    started, ended, drained = window
    selector = f'loadtest_run="{run}"'
    extracted = f'sum(pages_processed_total{{{selector}, status="extracted"}})'

    steps = []
    for index, count in enumerate(users):
        current = Step(count, started + index * step, min(started + (index + 1) * step, ended))
        span = f"{int(current.end - current.start)}s"
        current.pages = (scalar(extracted, current.end) or 0.0) - (
            scalar(extracted, current.start) or 0.0
        )
        current.page_p95 = page_quantile(0.95, run, span, current.end)
        current.max_queue = by(
            f"max by (queue) (max_over_time(queue_depth{{{selector}}}[{span}]))",
            current.end,
            "queue",
        )
        steps.append(current)

    results = out / "packs.jsonl"
    packs = (
        [json.loads(line) for line in results.read_text().splitlines()] if results.exists() else []
    )
    for pack in packs:
        target = steps[min(int((pack["at"] - started) // step), len(steps) - 1)]
        if pack["status"] in ROUTED:
            target.packs.append(pack["seconds"])
        else:
            target.unrouted += 1

    whole = f"{int(drained - started)}s"
    model = f"gen_ai_client_operation_duration_seconds_bucket{{{selector}}}"
    dlq = f"max by (queue) (dlq_depth{{{selector}}})"
    totals = {
        "pages_extracted": scalar(extracted, drained) or 0.0,
        "pages_failed": scalar(
            f'sum(pages_processed_total{{{selector}, status="failed"}})', drained
        )
        or 0.0,
        "cost_usd": scalar(f"sum(cost_usd_total{{{selector}}})", drained) or 0.0,
        "tokens": by(f"sum by (type) (llm_tokens_total{{{selector}}})", drained, "type"),
        "admission": by(
            f"sum by (outcome) (rate_limiter_wait_seconds_count{{{selector}}})", drained, "outcome"
        ),
        "page_p50": page_quantile(0.5, run, whole, drained),
        "page_p95": page_quantile(0.95, run, whole, drained),
        "model_p95": scalar(
            f"histogram_quantile(0.95, sum by (le) (increase({model}[{whole}])))", drained
        ),
        "http_status": by(
            f"sum by (http_status_code) (http_server_duration_milliseconds_count{{{selector}}})",
            drained,
            "http_status_code",
        ),
        "dlq_start": sum(by(dlq, started, "queue").values()),
        "dlq_end": sum(by(dlq, drained, "queue").values()),
        "max_queue": by(
            f"max by (queue) (max_over_time(queue_depth{{{selector}}}[{whole}]))", drained, "queue"
        ),
    }

    api: dict[str, dict[str, Any]] = {}
    with (out / "locust_stats.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["Name"] == "Aggregated":
                continue
            api[f"{row['Type']} {row['Name']}"] = {
                "requests": int(row["Request Count"]),
                "failures": int(row["Failure Count"]),
                "p50_ms": float(row["50%"]),
                "p95_ms": float(row["95%"]),
                "p99_ms": float(row["99%"]),
            }
    return {"steps": steps, "totals": totals, "api": api, "packs": packs}


def database_counts(started: float, drained: float) -> dict[str, int]:
    """The same counts from Postgres: pages and shipments created during the run."""
    from sqlalchemy import func, select

    from manifest_db.models import Document, Page, Shipment
    from manifest_db.session import sync_session_factory

    window = (datetime.fromtimestamp(started, UTC), datetime.fromtimestamp(drained, UTC))
    counts: dict[str, int] = {}
    with sync_session_factory()() as session:
        for status, n in session.execute(
            select(Page.status, func.count())
            .join(Document, Document.id == Page.document_id)
            .where(Document.created_at.between(*window))
            .group_by(Page.status)
        ):
            counts[f"pages_{status}"] = int(n)
        for status, n in session.execute(
            select(Shipment.status, func.count())
            .where(Shipment.created_at.between(*window))
            .group_by(Shipment.status)
        ):
            counts[f"shipments_{status}"] = int(n)
    return counts


# ---------------------------------------------------------------- the report


ROUTED_SHIPMENTS = ("shipments_needs_review", "shipments_auto_approved", "shipments_approved")


def design_step(steps: list[Step], concurrency: int) -> Step:
    """The design-load step: the highest step (before the first that fell behind) whose
    page_extract backlog never exceeded one round of the worker's processes. Past it the
    worker is at capacity, and a pack's time is mostly queueing."""
    chosen = steps[0]
    for current in steps:
        if current.max_queue.get("page_extract", 0.0) > concurrency:
            break
        chosen = current
    return chosen


def verdicts(
    measured: dict[str, Any],
    targets: dict[str, dict[str, float]],
    *,
    concurrency: int,
    database: dict[str, int],
) -> list[dict[str, Any]]:
    steps: list[Step] = measured["steps"]
    totals = measured["totals"]
    api = {k: v for k, v in measured["api"].items() if not k.startswith(("PIPELINE", "PUT"))}
    requests = sum(row["requests"] for row in api.values())
    errors = sum(n for code, n in totals["http_status"].items() if code.startswith("5"))
    design = design_step(steps, concurrency)
    # Every shipment extracted in the window, including those still in flight when the users
    # stopped (they finished during the drain, so Locust never saw them routed).
    shipments = sum(database.get(name, 0) for name in ROUTED_SHIPMENTS)
    pages, spend = totals["pages_extracted"], totals["cost_usd"]
    values = {
        "api_p95_ms": max((row["p95_ms"] for row in api.values()), default=None),
        "pack_p50_seconds": statistics.median(design.packs) if design.packs else None,
        "pack_p95_seconds": percentile(design.packs, 0.95),
        "page_p95_seconds": totals["page_p95"],
        "throughput_pages_per_minute": max(s.pages_per_minute for s in steps),
        "api_error_rate": errors / requests if requests else None,
        "failed_pages": totals["pages_failed"],
        "unrouted_packs": sum(s.unrouted for s in steps),
        "dlq_growth": totals["dlq_end"] - totals["dlq_start"],
        "cost_per_page_usd": spend / pages if pages else None,
        "cost_per_pack_usd": spend / shipments if shipments else None,
    }
    checks = []
    for name, bound in targets.items():
        value = values.get(name)
        if value is None:
            ok = False
        elif "min" in bound:
            ok = value >= bound["min"]
        else:
            ok = value <= bound["max"]
        where = f"at {design.users} users" if name.startswith("pack_") else ""
        checks.append({"metric": name, "value": value, "bound": bound, "ok": ok, "where": where})
    return checks


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return f"{value:,}"


def listing(values: dict[str, float]) -> str:
    return ", ".join(f"{k} {fmt(v, 0)}" for k, v in sorted(values.items())) or "none"


def report(
    run: str,
    args: argparse.Namespace,
    measured: dict[str, Any],
    database: dict[str, int],
    checks: list[dict[str, Any]],
    window: tuple[float, float, float],
) -> str:
    started, _, drained = window
    steps: list[Step] = measured["steps"]
    totals = measured["totals"]
    tokens = totals["tokens"]
    passed = sum(c["ok"] for c in checks)
    lines = [
        f"# Load test {run}",
        "",
        f"{datetime.fromtimestamp(started, UTC):%Y-%m-%d %H:%M} UTC; {len(steps)} steps of "
        f"{args.step} s ({args.users} users), one worker container (prefork, concurrency "
        f"{args.concurrency}), two API processes, all on one Docker Desktop VM. "
        f"Targets met: **{passed}/{len(checks)}**.",
        "",
        "The worker replayed the eval corpus' real Sonnet 5.5 recordings at their recorded "
        "latency: model latency and token counts are real, nothing was billed, and cost is what "
        "those tokens cost at list prices. HS classification was off (no recordings for these "
        "lines), so every shipment routed to review (HS_UNCERTAIN) and the HS stage's cost and "
        "latency are not in these numbers.",
        "",
        "Estimated, not measured: `hs_classify` walks a shipment's lines one after another in one "
        "task. At the evals' $0.0104 and 10.2 s p95 per line and the corpus' 3.5 invoice lines "
        "per pack (up to 10), HS would add about $0.036 and roughly 20-35 s per pack: the cost "
        "target would hold, the 30 s pack p50 likely would not. Walking lines in parallel (one "
        "task per line, or concurrent walks within the task) is the fix to measure next.",
        "",
        "## Against the targets (tools/loadtest/targets.yaml)",
        "",
        "| metric | target | measured | |",
        "|---|---|---|---|",
    ]
    for check in checks:
        bound = ", ".join(f"{k} {v}" for k, v in check["bound"].items())
        verdict = "met" if check["ok"] else "**missed**"
        where = f" ({check['where']})" if check.get("where") else ""
        measured_value = fmt(check["value"], 4)
        lines.append(f"| {check['metric']}{where} | {bound} | {measured_value} | {verdict} |")
    design = design_step(steps, args.concurrency)
    capacity = max(s.pages_per_minute for s in steps)
    lines += [
        "",
        "## Steps",
        "",
        f"Pack latency targets are read at the design-load step, {design.users} users: the "
        f"highest step whose page_extract backlog stayed within one round of the worker's "
        f"{args.concurrency} processes. Beyond it the worker is at capacity (about "
        f"{capacity:.0f} pages a minute here), each user's next pack waits behind everyone "
        "else's, and pack time grows with the queue, as it must in a closed loop.",
        "",
        "| users | pages/min | packs routed | pack p50 s | pack p95 s | page p95 s "
        "| max page_extract queue | unrouted |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in steps:
        p50 = statistics.median(s.packs) if s.packs else None
        lines.append(
            f"| {s.users} | {fmt(s.pages_per_minute, 1)} | {len(s.packs)} | {fmt(p50, 1)} | "
            f"{fmt(percentile(s.packs, 0.95), 1)} | {fmt(s.page_p95, 1)} | "
            f"{fmt(s.max_queue.get('page_extract', 0.0), 0)} | {s.unrouted} |"
        )
    lines += [
        "",
        "## Totals",
        "",
        f"- Pages extracted {fmt(totals['pages_extracted'], 0)}, failed "
        f"{fmt(totals['pages_failed'], 0)} (Prometheus); database: {database}.",
        f"- Model spend ${totals['cost_usd']:.4f}: {fmt(tokens.get('input', 0.0), 0)} input and "
        f"{fmt(tokens.get('output', 0.0), 0)} output tokens.",
        f"- Page latency p50 {fmt(totals['page_p50'], 1)} s, p95 {fmt(totals['page_p95'], 1)} s; "
        f"model call p95 {fmt(totals['model_p95'], 1)} s (interpolated in the semantic "
        "conventions' 10.24-20.48 s bucket, so coarser than the page figure).",
        f"- Rate limiter admissions: {listing(totals['admission'])}.",
        f"- Max queue depth: {listing(totals['max_queue'])}.",
        f"- API responses by status: {listing(totals['http_status'])}.",
        f"- DLQ depth {fmt(totals['dlq_start'], 0)} at the start, {fmt(totals['dlq_end'], 0)} "
        "after draining.",
        "",
        "## Requests (Locust, whole run)",
        "",
        "| request | count | failures | p50 ms | p95 ms | p99 ms |",
        "|---|---|---|---|---|---|",
    ]
    for name, row in sorted(measured["api"].items()):
        lines.append(
            f"| {name} | {row['requests']} | {row['failures']} | {fmt(row['p50_ms'], 0)} | "
            f"{fmt(row['p95_ms'], 0)} | {fmt(row['p99_ms'], 0)} |"
        )
    lines += [
        "",
        f"Dashboards: Grafana > Manifest, environment `loadtest`, "
        f"{datetime.fromtimestamp(started, UTC):%H:%M}-{datetime.fromtimestamp(drained, UTC):%H:%M}"
        f' UTC. Traces: Tempo, `{{ resource.loadtest.run = "{run}" }}`.',
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--users", default="2,4,8,16,32", help="users per step")
    parser.add_argument("--step", type=int, default=120, help="seconds per step")
    parser.add_argument("--concurrency", type=int, default=8, help="worker processes")
    parser.add_argument("--keep-running", action="store_true", help="leave the app containers up")
    parser.add_argument(
        "--report",
        metavar="RUN",
        help="rebuild an earlier run's report (its Prometheus data and .loadtest/RUN must remain)",
    )
    args = parser.parse_args()

    if args.report:
        run = args.report
        earlier = json.loads((REPORTS / f"{run}.json").read_text())
        args.users, args.step = earlier["args"]["users"], earlier["args"]["step"]
        args.concurrency = earlier["args"]["concurrency"]
        started, ended, drained = earlier["window"]
        out = WORK / run
    else:
        run = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = WORK / run
        out.mkdir(parents=True)
        print(f"run {run}: preparing replay cassettes", flush=True)
        cassette_dir = cassettes()
        print("starting the stack and the app containers", flush=True)
        start_app(run, args.concurrency, cassette_dir)
        wait_healthy()
        time.sleep(20)  # the monitor's first samples, so DLQ depth has a start value
        started = time.time()
        try:
            locust(args.users, args.step, out / "locust", out / "packs.jsonl")
            ended = time.time()
            print("draining the queues", flush=True)
            drain()
            drained = time.time()
        finally:
            if not args.keep_running:
                subprocess.run([*COMPOSE, "stop", "api", "worker", "monitor"], check=False)

    users = [int(n) for n in args.users.split(",")]
    window = (started, ended, drained)
    measured = measure(run, window, users, args.step, out)
    database = database_counts(started, drained)
    targets = yaml.safe_load((TOOL / "targets.yaml").read_text())
    checks = verdicts(measured, targets, concurrency=args.concurrency, database=database)
    text = report(run, args, measured, database, checks, window)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"{run}.md").write_text(text, encoding="utf-8")
    raw = {
        "run": run,
        "args": {k: v for k, v in vars(args).items() if k != "report"},
        "window": window,
        "checks": checks,
        "totals": measured["totals"],
        "database": database,
        "steps": [vars(s) for s in measured["steps"]],
        "api": measured["api"],
    }
    (REPORTS / f"{run}.json").write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    print(text)
    return 0 if all(c["ok"] for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
