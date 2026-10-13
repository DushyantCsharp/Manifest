"""The eval harness: ``python -m manifest_evals run`` (``make eval``) and ``calibrate``.

Every pack of the corpus goes through the pipeline's own extractor (answers replayed from
cassettes), page merge, cross-document reconciliation and scoring, so the numbers describe
the code that ships. Then:

1. **Accuracy**: every value extracted or expected is compared with the pack's ground truth
   (line items paired by ``alignment``). Per field, and per variant (clean, scanned, mismatch).
2. **Line items**: precision, recall and F1 of extracted lines against true lines.
3. **Reconciliation**: recall of the discrepancies each mismatch pack's label lists, and the
   false-alarm rate: the share of packs with no injected mismatch that raised any.
4. **Calibration**: an isotonic calibrator is fitted on the calibration split (values whose
   evidence checked out) and judged on the test split: ECE before and after, a reliability
   diagram, the precision/coverage curve, and the auto-approve threshold (the lowest
   calibrated confidence whose precision reaches the target on the calibration split).
5. **Cost**: p95 latency, tokens and USD per page, from the recorded calls.
6. **HS**: 120 labelled goods descriptions through retrieval and the walk: accuracy at 2, 4
   and 6 digits, retrieval recall@20, and an ``hs`` calibrator judged like extraction's.

The report goes to ``evals/reports/latest/`` and the run fails on ``evals/thresholds.yaml``,
whose profile follows where the answers came from: ``plumbing`` while any cassette was seeded
by the ground-truth oracle (the numbers prove the machinery, not a model), ``model`` once all
were recorded from one. ``run`` also fails when the committed calibrator no longer matches a
refit; ``calibrate`` writes a new version.
"""

import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml

from manifest_db.models import EMBEDDING_DIMENSIONS
from manifest_db.session import sync_session_factory
from manifest_domain import calibration
from manifest_domain.alignment import align, line_distance
from manifest_domain.calibration import Calibrator, FitSummary, fit_isotonic
from manifest_domain.extraction import CURRENT_SCHEMA_VERSION, ExtractedValue
from manifest_domain.lifecycle import DocumentKind
from manifest_domain.reconciliation import DocumentReading, Finding, corroborated, reconcile
from manifest_domain.routing import ShipmentResults, decide, missing_required
from manifest_domain.scoring import SCORE_VERSION, final
from manifest_evals import golden, metrics, plots
from manifest_evals import hs as hs_eval
from manifest_evals.harness import EVALS_ROOT
from manifest_evals.metrics import Scored
from manifest_evals.oracle import NAME as ORACLE
from manifest_hs import index as hs_index
from manifest_hs import walk as hs_walk
from manifest_hs.offline import Bm25Retriever
from manifest_hs.retrieval import PostgresRetriever, Retriever
from manifest_llm.anthropic_provider import AnthropicProvider
from manifest_llm.cassette import CassetteProvider
from manifest_llm.pricing import cost
from manifest_llm.voyage_provider import VoyageProvider
from manifest_worker.extraction import MODEL, PROMPT_VERSION
from manifest_worker.ports import ExtractedPage

THRESHOLDS = EVALS_ROOT / "thresholds.yaml"
REPORT = EVALS_ROOT / "reports" / "latest"
Mode = Literal["replay", "record", "live"]
Expected = set[tuple[str, str, int | None]]
_LABEL_FIELDS = {"buyer.name": "buyer_name", "seller.name": "seller_name"}
_PLACES = Decimal("0.000001")


# ---------------------------------------------------------------- per pack


@dataclass
class Value:
    """One value a document states or extraction produced, judged against the truth."""

    pack: str
    split: str
    variant: str
    document: str
    field: str
    line: int | None
    expected: str | None
    extracted: ExtractedValue | None
    correct: bool
    calibrated: Decimal = Decimal(0)

    @property
    def found(self) -> bool:
        return self.extracted is not None and self.extracted.value is not None

    @property
    def verified(self) -> bool:
        signals = self.extracted.signals if self.extracted else None
        return self.found and (signals is None or signals.evidence == "verified")

    @property
    def raw(self) -> Decimal:
        """The confidence before calibration, after the evidence gate (what an uncalibrated
        pipeline would store)."""
        return final(self.extracted, None) if self.extracted is not None else Decimal(0)


@dataclass
class PackResult:
    pack: str
    split: str
    variant: str
    labelled: Expected = field(default_factory=set)
    values: list[Value] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    lines: Counter[str] = field(default_factory=Counter)  # true_positive, extracted, actual
    pages: list[ExtractedPage] = field(default_factory=list)
    missing_required: bool = False

    @property
    def found(self) -> Expected:
        return {(f.kind.value, f.field_key, f.line_no) for f in self.findings}

    def judge(
        self,
        document: str,
        kinds: dict[str, str],
        where: tuple[str, int | None],
        expected: dict[str, str | None],
        extracted: dict[str, ExtractedValue],
    ) -> None:
        names = {k for k, v in expected.items() if v is not None}
        names |= {k for k, v in extracted.items() if v.value is not None}
        prefix, line = where
        for name in sorted(names):
            want, got = expected.get(name), extracted.get(name)
            value = got.value if got is not None else None
            right = want is not None and golden.same(kinds.get(name), want, value)
            self.values.append(
                Value(
                    self.pack,
                    self.split,
                    self.variant,
                    document,
                    prefix + name,
                    line,
                    want,
                    got,
                    right,
                )
            )


def expected_findings(label: dict[str, Any]) -> Expected:
    """The (kind, field, invoice line) of each discrepancy the pack's label lists."""
    found: Expected = set()
    for discrepancy in label["discrepancies"]:
        key = _LABEL_FIELDS.get(discrepancy["field"], discrepancy["field"])
        line = discrepancy["line_no"] if key.startswith("line_items") else None
        found.add((discrepancy["kind"], key, line))
    return found


def _texts(line: dict[str, ExtractedValue]) -> dict[str, str | None]:
    return {name: value.value for name, value in line.items()}


def evaluate_pack(pack: str, split: str, provider: CassetteProvider) -> PackResult:
    label = golden.label(pack)
    result = PackResult(pack, split, label["variant"], expected_findings(label))
    documents = golden.extract_pack(pack, provider)
    readings = {
        name: DocumentReading(doc.kind or DocumentKind.OTHER, doc.merged.fields, doc.merged.lines)
        for name, doc in documents.items()
    }
    reconciliation = reconcile(list(readings.values()))
    result.findings = reconciliation.findings
    truths = golden.truths(pack)
    for name, reading in readings.items():
        result.pages += documents[name].pages
        scored = corroborated(reading, reconciliation)
        kind = DocumentKind(name)
        kinds = golden.kinds(kind)
        result.missing_required |= bool(missing_required(kind, scored.fields))
        truth = golden.expected(truths[name])
        result.judge(name, kinds, ("", None), truth.fields, dict(scored.fields))
        got = [dict(line) for line in scored.lines]
        alignment = align(truth.lines, [_texts(line) for line in got], line_distance)
        for i, j in alignment.pairs:
            result.judge(name, kinds, ("line_items.", i + 1), truth.lines[i], got[j])
            description = got[j].get("description")
            said = description.value if description is not None else None
            if golden.same("text", truth.lines[i].get("description"), said):
                result.lines["true_positive"] += 1
        for i in alignment.left_only:
            result.judge(name, kinds, ("line_items.", i + 1), truth.lines[i], {})
        for j in alignment.right_only:
            result.judge(name, kinds, ("line_items.", None), {}, got[j])
        result.lines["extracted"] += len(got)
        result.lines["actual"] += len(truth.lines)
    return result


# ---------------------------------------------------------------- the run


@dataclass
class Run:
    results: list[PackResult]
    sources: Counter[str]
    target: Decimal
    fitted: dict[str, Calibrator]
    committed: dict[str, Calibrator | None]
    hs: hs_eval.HsRun | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def calibrator(self) -> Calibrator:
        return self.fitted["extraction"]

    @property
    def profile(self) -> str:
        """``plumbing`` while any answer came from a stand-in (or retrieval was offline)."""
        stand_ins = self.sources + (self.hs.sources if self.hs else Counter())
        offline = self.hs is not None and Bm25Retriever.name in self.hs.retrieval
        return "plumbing" if offline or any(s.startswith(ORACLE) for s in stand_ins) else "model"

    def values(self) -> list[Value]:
        return [value for result in self.results for value in result.values]


def _provider(pack: str, mode: Mode, scratch: Path) -> CassetteProvider:
    if mode == "replay":
        return golden.cassette(pack)
    root = golden.CASSETTES if mode == "record" else scratch
    golden.record(pack, root=root, provider=AnthropicProvider())
    return golden.cassette(pack, root=root)


def live_retriever() -> Retriever:
    """HS retrieval against the database (``make up && make migrate && make hs-index``):
    hybrid with VOYAGE_API_KEY, full-text only without."""
    sessions = sync_session_factory()
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        return PostgresRetriever(sessions)
    voyage = VoyageProvider(api_key=key)
    gateway = hs_index.gateway_for(voyage)

    def embed(text: str) -> list[float]:
        found = gateway.embed(
            voyage.name,
            hs_index.DEFAULT_MODEL,
            [text],
            input_type="query",
            dimensions=EMBEDDING_DIMENSIONS,
        )
        return found.vectors[0]

    return PostgresRetriever(sessions, embed, hs_index.DEFAULT_MODEL)


def hs_run(mode: Mode, scratch: Path) -> hs_eval.HsRun:
    if mode == "replay":
        return hs_eval.evaluate()
    root = golden.CASSETTES if mode == "record" else scratch
    hs_eval.record(live_retriever(), root)
    return hs_eval.evaluate(hs_eval.cassettes(root=root), hs_eval.retrievals(root=root))


def _found(results: Iterable[PackResult], split: str) -> list[Value]:
    return [v for r in results if r.split == split for v in r.values if v.found]


def _draft(name: str, knots: Any, provenance: str, prompt: str, summary: FitSummary) -> Calibrator:
    latest = calibration.versions(name)
    return Calibrator(
        name=name,
        version=latest[-1] if latest else 1,
        provenance=provenance,
        score_version=SCORE_VERSION,
        schema_version=CURRENT_SCHEMA_VERSION,
        prompt_version=prompt,
        model=f"{golden.PROVIDER}/{MODEL}",
        knots=knots,
        fitted_on=summary,
    )


def _judged(
    draft: Calibrator,
    calibration_split: Sequence[tuple[Decimal, bool]],
    test: Sequence[tuple[Decimal, bool]],
    target: Decimal,
) -> Calibrator:
    """``draft`` with its threshold (chosen on the calibration split) and test-split ECE."""
    threshold = metrics.choose_threshold(
        [Scored(draft.apply(raw), right) for raw, right in calibration_split], target
    )
    summary = {
        "ece_before_test": metrics.ece([Scored(raw, right) for raw, right in test]),
        "ece_after_test": metrics.ece([Scored(draft.apply(raw), right) for raw, right in test]),
        "test_samples": Decimal(len(test)),
    }
    quantized = {k: v.quantize(_PLACES) for k, v in summary.items() if v is not None}
    update = {"threshold": threshold, "target_precision": target, "metrics": quantized}
    return draft.model_copy(update=update)


def fit_hs(run: hs_eval.HsRun, target: Decimal, provenance: str) -> Calibrator:
    """The HS calibrator: walks with a code, raw confidence against being right at 6 digits."""
    scored = {
        split: [(o.walk.raw, o.correct()) for o in run.outcomes if o.split == split and o.walk.code]
        for split in ("calibration", "test")
    }
    train = scored["calibration"]
    summary = FitSummary(
        split="calibration",
        items=sum(1 for o in run.outcomes if o.split == "calibration"),
        samples=len(train),
        positives=sum(1 for _, right in train if right),
    )
    draft = _draft("hs", fit_isotonic(train), provenance, hs_walk.PROMPT_VERSION, summary)
    return _judged(draft, train, scored["test"], target)


def fit(results: Sequence[PackResult], sources: Counter[str], target: Decimal) -> Calibrator:
    """The extraction calibrator: fitted on the calibration split's values whose evidence
    checked out, its threshold chosen there, judged on the test split on what the pipeline
    stores (calibrated, and 0 without verified evidence)."""
    train = [v for r in results if r.split == "calibration" for v in r.values if v.verified]
    provenance = "oracle" if any(s.startswith(ORACLE) for s in sources) else "model"
    summary = FitSummary(
        split="calibration",
        items=sum(1 for r in results if r.split == "calibration"),
        samples=len(train),
        positives=sum(1 for v in train if v.correct),
    )
    knots = fit_isotonic((v.raw, v.correct) for v in train)
    draft = _draft("extraction", knots, provenance, PROMPT_VERSION, summary)

    def stored(values: Sequence[Value]) -> list[Scored]:
        return [Scored(final(v.extracted, draft.apply), v.correct) for v in values]

    threshold = metrics.choose_threshold(stored(_found(results, "calibration")), target)
    test = _found(results, "test")
    judged = {
        "ece_before_test": metrics.ece([Scored(v.raw, v.correct) for v in test]),
        "ece_after_test": metrics.ece(stored(test)),
        "test_samples": Decimal(len(test)),
    }
    quantized = {k: v.quantize(_PLACES) for k, v in judged.items() if v is not None}
    update = {"threshold": threshold, "target_precision": target, "metrics": quantized}
    return draft.model_copy(update=update)


def _committed(name: str) -> Calibrator | None:
    versions = calibration.versions(name)
    return calibration.load(name, versions[-1]) if versions else None


def evaluate(packs: Sequence[str] | None = None, mode: Mode = "replay") -> Run:
    """Replay (or record) the corpus and the HS labels, fit the calibrators, measure."""
    target = Decimal(str(yaml.safe_load(THRESHOLDS.read_text())["target_precision"]))
    split_of = {p: s for s, ids in golden.splits()["packs"].items() for p in ids}
    sources: Counter[str] = Counter()
    results = []
    with tempfile.TemporaryDirectory() as scratch:
        for pack in packs or golden.golden_packs():
            provider = _provider(pack, mode, Path(scratch))
            results.append(evaluate_pack(pack, split_of[pack], provider))
            sources += provider.sources
        hs = None if packs else hs_run(mode, Path(scratch))
    extraction = fit(results, sources, target)
    for value in (v for r in results for v in r.values):
        if value.extracted is not None:
            value.calibrated = final(value.extracted, extraction.apply)
    fitted = {"extraction": extraction}
    if hs is not None:
        stand_in = any(s.startswith(ORACLE) for s in hs.sources)
        offline = Bm25Retriever.name in hs.retrieval
        fitted["hs"] = fit_hs(hs, target, "oracle" if stand_in or offline else "model")
    committed = {name: _committed(name) for name in fitted}
    run = Run(results, sources, target, fitted, committed, hs)
    measure(run)
    return run


def _accuracy(values: Sequence[Value]) -> Decimal | None:
    return metrics.ratio(sum(1 for v in values if v.correct), len(values))


def measure(run: Run) -> None:
    m: dict[str, Any] = {}
    every = run.values()
    m["field_accuracy"] = _accuracy(every)
    for variant in ("clean", "scanned", "mismatch"):
        m[f"field_accuracy_{variant}"] = _accuracy([v for v in every if v.variant == variant])
    lines = sum((r.lines for r in run.results), Counter())
    precision, recall, f1 = metrics.f1(lines["true_positive"], lines["extracted"], lines["actual"])
    m |= {"line_item_precision": precision, "line_item_recall": recall, "line_item_f1": f1}

    labelled = sum(len(r.labelled) for r in run.results)
    caught = sum(len(r.labelled & r.found) for r in run.results)
    unmutated = [r for r in run.results if not r.labelled]
    m["reconciliation_recall"] = metrics.ratio(caught, labelled)
    alarmed = sum(1 for r in unmutated if r.findings)
    m["false_alarm_rate"] = metrics.ratio(alarmed, len(unmutated))
    for variant in ("clean", "scanned"):
        packs = [r for r in unmutated if r.variant == variant]
        m[f"false_alarm_rate_{variant}"] = metrics.ratio(
            sum(1 for r in packs if r.findings), len(packs)
        )
    m["false_findings"] = sum(len(r.found - r.labelled) for r in run.results)

    test = [v for v in every if v.split == "test" and v.found]
    before = [Scored(v.raw, v.correct) for v in test]
    after = [Scored(v.calibrated, v.correct) for v in test]
    m["ece_before"], m["ece"] = metrics.ece(before), metrics.ece(after)
    threshold = run.calibrator.threshold
    coverage, precision_at = metrics.at(after, threshold)
    m["auto_approve_threshold"] = threshold
    m["coverage_at_threshold"] = coverage
    m["precision_at_threshold"] = precision_at
    m |= _shipments(run, threshold)

    pages = [page for r in run.results for page in r.pages]
    m["pages"] = len(pages)
    m["p95_latency_ms"] = metrics.percentile([p.latency_ms for p in pages], 95)
    tokens = sum(p.tokens_in + p.tokens_out for p in pages)
    m["tokens_per_page"] = Decimal(tokens) / len(pages) if pages else None
    costs = [cost(p.model, p.tokens_in, p.tokens_out) for p in pages]
    known = [c for c in costs if c is not None]
    priced = bool(pages) and len(known) == len(costs)
    m["usd_per_page"] = sum(known, Decimal(0)) / len(known) if priced else None
    if run.hs is not None:
        m |= _hs_metrics(run.hs, run.fitted["hs"])
    run.metrics = m
    run.details = {
        "fields": _per_field(every),
        "reconciliation": _reconciliation_details(run.results),
        "bins_before": metrics.reliability(before),
        "bins_after": metrics.reliability(after),
        "curve": metrics.curve(after),
    }


def _hs_metrics(hs: hs_eval.HsRun, calibrator: Calibrator) -> dict[str, Any]:
    outcomes = hs.outcomes
    m: dict[str, Any] = {"hs_descriptions": len(outcomes)}
    m["hs_recall_at_20"] = metrics.ratio(sum(o.recalled for o in outcomes), len(outcomes))
    for digits in (2, 4, 6):
        right = sum(o.correct(digits) for o in outcomes)
        m[f"hs_accuracy_{digits}"] = metrics.ratio(right, len(outcomes))
    test = [o for o in outcomes if o.split == "test" and o.walk.code]
    after = [Scored(calibrator.apply(o.walk.raw), o.correct()) for o in test]
    m["hs_ece_before"] = metrics.ece([Scored(o.walk.raw, o.correct()) for o in test])
    m["hs_ece"] = metrics.ece(after)
    coverage, precision = metrics.at(after, calibrator.threshold)
    m["hs_threshold"] = calibrator.threshold
    m["hs_coverage_at_threshold"] = coverage
    m["hs_precision_at_threshold"] = precision
    m["hs_p95_latency_ms"] = metrics.percentile([o.walk.latency_ms for o in outcomes], 95)
    costs = [cost(o.walk.model, o.walk.input_tokens, o.walk.output_tokens) for o in outcomes]
    known = [c for c in costs if c is not None]
    priced = bool(costs) and len(known) == len(costs)
    m["hs_usd_per_description"] = sum(known, Decimal(0)) / len(known) if priced else None
    return m


def hs_bins(run: Run) -> tuple[list[metrics.Bin], list[metrics.Bin]]:
    if run.hs is None:
        return [], []
    calibrator = run.fitted["hs"]
    test = [o for o in run.hs.outcomes if o.split == "test" and o.walk.code]
    before = metrics.reliability([Scored(o.walk.raw, o.correct()) for o in test])
    after = metrics.reliability([Scored(calibrator.apply(o.walk.raw), o.correct()) for o in test])
    return before, after


def _shipments(run: Run, threshold: Decimal | None) -> dict[str, Any]:
    """Shipment-level routing on the test split, extraction only (HS is judged separately)."""
    approved = right = total = 0
    for result in (r for r in run.results if r.split == "test"):
        total += 1
        decision = decide(
            ShipmentResults(
                partial=False,
                discrepancies=[f.kind for f in result.findings],
                missing_required=result.missing_required,
                confidences=[v.calibrated for v in result.values if v.found],
                hs_confidences=(),
                extraction_threshold=threshold,
                hs_threshold=Decimal(0),
            )
        )
        if decision.auto_approve:
            approved += 1
            right += all(v.correct for v in result.values) and not result.labelled
    return {
        "shipment_auto_approve_rate": metrics.ratio(approved, total),
        "shipment_precision": metrics.ratio(right, approved),
    }


def _per_field(values: Sequence[Value]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Value]] = defaultdict(list)
    for value in values:
        groups[(value.document, value.field)].append(value)
    return [
        {"document": d, "field": f, "values": len(vs), "accuracy": _accuracy(vs)}
        for (d, f), vs in sorted(groups.items())
    ]


def _reconciliation_details(results: Sequence[PackResult]) -> dict[str, Any]:
    missed = [(r.pack, item) for r in results for item in sorted(r.labelled - r.found, key=str)]
    false = [(r.pack, item) for r in results for item in sorted(r.found - r.labelled, key=str)]
    return {
        "missed": [{"pack": pack, "discrepancy": list(item)} for pack, item in missed],
        "false": [{"pack": pack, "finding": list(item)} for pack, item in false],
    }


# ---------------------------------------------------------------- gate, report, calibrators


def gate(run: Run) -> list[dict[str, Any]]:
    bounds = yaml.safe_load(THRESHOLDS.read_text())["profiles"][run.profile]
    checks = []
    for name, bound in bounds.items():
        value = run.metrics.get(name)
        if value is None:
            ok = False
        elif "min" in bound:
            ok = Decimal(str(value)) >= Decimal(str(bound["min"]))
        else:
            ok = Decimal(str(value)) <= Decimal(str(bound["max"]))
        checks.append({"metric": name, "value": value, "bound": bound, "ok": ok})
    return checks


def drift(run: Run) -> str | None:
    """Why a committed calibrator isn't what a refit gives (None: they all are)."""
    for name, fitted in run.fitted.items():
        committed = run.committed.get(name)
        if committed is None:
            return f"no {name} calibrator is committed: run `make calibrate`"
        if calibration.dumps(committed) != calibration.dumps(fitted):
            return f"{committed.label} no longer matches a refit: run `make calibrate`"
    return None


def write_calibrators(run: Run) -> list[Path]:
    """Commit each refit as a new version (an identical refit stays as it is)."""
    paths = []
    for name, fitted in run.fitted.items():
        committed = run.committed.get(name)
        calibrator = fitted
        if committed is not None and calibration.dumps(committed) != calibration.dumps(fitted):
            calibrator = fitted.model_copy(update={"version": committed.version + 1})
        path = calibration.artifact_path(calibrator.name, calibrator.version)
        path.write_text(calibration.dumps(calibrator), encoding="utf-8")
        run.fitted[name] = run.committed[name] = calibrator
        paths.append(path)
    return paths


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return {k: _plain(getattr(value, k)) for k in value.__dataclass_fields__}
    return value


def fmt(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{value:.4f}" if abs(value) < 10 else f"{value:.2f}"
    return "n/a" if value is None else str(value)


def write_report(run: Run, checks: list[dict[str, Any]], directory: Path = REPORT) -> Path:
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    details = run.details
    plots.reliability(
        directory / "reliability-extraction.png",
        "Extraction confidence, test split",
        [
            ("raw scores", details["bins_before"], run.metrics["ece_before"]),
            ("calibrated", details["bins_after"], run.metrics["ece"]),
        ],
    )
    if run.hs is not None:
        before, after = hs_bins(run)
        plots.reliability(
            directory / "reliability-hs.png",
            "HS confidence, test split",
            [
                ("raw walk confidence", before, run.metrics["hs_ece_before"]),
                ("calibrated", after, run.metrics["hs_ece"]),
            ],
        )
    plots.precision_coverage(
        directory / "precision-coverage.png",
        details["curve"],
        threshold=run.metrics["auto_approve_threshold"],
        target=run.target,
        title="Auto-approve: precision vs coverage (test split)",
    )
    report = {
        "profile": run.profile,
        "sources": dict(run.sources),
        "calibrators": {name: c.model_dump(mode="json") for name, c in run.fitted.items()},
        "metrics": run.metrics,
        "gate": checks,
        "fields": details["fields"],
        "reconciliation": details["reconciliation"],
        "reliability": {"before": details["bins_before"], "after": details["bins_after"]},
    }
    (directory / "report.json").write_text(json.dumps(_plain(report), indent=2) + "\n")
    (directory / "report.md").write_text(_markdown(run, checks))
    return directory


def _markdown(run: Run, checks: list[dict[str, Any]]) -> str:
    answered = ", ".join(f"{k} ({v})" for k, v in sorted(run.sources.items()))
    lines = ["# Eval report", "", f"Profile **{run.profile}**; answers from {answered}."]
    if run.profile == "plumbing":
        lines += [
            "",
            "> These answers were seeded offline by the ground-truth oracle. They prove the",
            "> pipeline, scoring and calibration machinery; they say nothing about a model.",
            "> Latency and cost are not measured (the oracle is instant and its token counts",
            "> are estimates). Record a real model with `make eval-record`.",
        ]
    lines += ["", "| metric | value | gate | |", "|---|---|---|---|"]
    gated = {check["metric"]: check for check in checks}
    for name, value in run.metrics.items():
        check = gated.get(name)
        bound = "" if check is None else ", ".join(f"{k} {v}" for k, v in check["bound"].items())
        verdict = "" if check is None else ("pass" if check["ok"] else "**FAIL**")
        lines.append(f"| {name} | {fmt(value)} | {bound} | {verdict} |")
    lines += [
        "",
        "![reliability](reliability-extraction.png)",
        "",
        "![precision/coverage](precision-coverage.png)",
        "",
        "![HS reliability](reliability-hs.png)",
        "",
        "## Per-field accuracy",
        "",
        "| document | field | values | accuracy |",
        "|---|---|---|---|",
    ]
    for row in run.details["fields"]:
        accuracy = fmt(row["accuracy"])
        lines.append(f"| {row['document']} | {row['field']} | {row['values']} | {accuracy} |")
    recon = run.details["reconciliation"]
    lines += [
        "",
        "## Reconciliation",
        "",
        f"Missed: {len(recon['missed'])}; false findings: {len(recon['false'])}.",
        "",
    ]
    lines += [f"- missed {m['pack']}: {m['discrepancy']}" for m in recon["missed"]]
    lines += [f"- false {f['pack']}: {f['finding']}" for f in recon["false"]]
    return "\n".join(lines) + "\n"


def main(command: str, mode: Mode, packs: Sequence[str], report: Path) -> int:
    run = evaluate(packs or None, mode)
    if command == "calibrate":
        for path in write_calibrators(run):
            print(f"wrote {path}")
    checks = gate(run)
    write_report(run, checks, report)
    for check in checks:
        status = "pass" if check["ok"] else "FAIL"
        print(f"{status:4} {check['metric']:<28} {fmt(check['value']):>10}  {check['bound']}")
    problem = None if packs else drift(run)
    if problem:
        print(f"FAIL {problem}", file=sys.stderr)
    print(f"profile {run.profile}; report in {report}")
    return 1 if problem or not all(check["ok"] for check in checks) else 0
