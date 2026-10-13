# Evals

Measures whether Manifest's confidence scores can be trusted, not just whether extraction works.

```
evals/golden/<pack_id>/        one shipment: three PDFs, their ground truth, pages.json
evals/labels/<pack_id>.json    the shipment's truth and the discrepancies it should raise
evals/labels/hs.jsonl          120 goods descriptions with their HS 2022 subheading
evals/splits.json              calibration / test split of the packs and the HS labels
evals/cassettes/               recorded model answers (per pack; hs/) and HS retrieval
evals/thresholds.yaml          the gates `make eval` enforces
evals/reports/latest/          the last report (git-ignored)
```

## The corpus (`make corpus`)

55 packs generated once and committed (`python -m manifest_evals.synth freeze`):

| packs | what | measures |
|---|---|---|
| `clean-0001..0025` | documents agree; text-layer PDFs (5 per invoice layout) | extraction accuracy, false alarms |
| `scan-0001..0015` | documents agree; image-only scans read by OCR (3 per layout) | accuracy under OCR, false alarms |
| `mism-0001..0015` | 1-2 deliberate cross-document mismatches each; every kind covered | reconciliation recall |

Each pack's `pages.json` holds every page's text exactly as the pipeline read it (native layer,
else tesseract); `golden/TOOLS.json` records the PDFium and tesseract versions. Replays use the
snapshot, so a different build can't change the requests cassettes are keyed by. The five
invoice layouts exercise normalisation: US (`1,234.56`, `09/30/2026`), German (`1.234,56`,
`30.09.2026`), exporter grid (descriptions in capitals, HS printed, total in words), letterhead
(long dates, pounds) and compact French (`1 234,56`, ISO dates).

`evals/splits.json` puts 28 packs in the calibration split and 27 in the test split,
alternating within each variant and layout, and splits the HS labels by chapter so no code
lands on both sides.

The 120 HS labels were written for this corpus and checked against the vendored HS 2022 texts
(a test checks every code exists); they are not a customs broker's rulings. The exporter
layout prints HS codes on the invoice; the classifier never sees them (description and origin
only).

`make seed` still regenerates the 40 unfrozen `syn-*` packs for ad-hoc work.

## The harness (`make eval`)

`python -m manifest_evals run` replays the corpus through the pipeline's own extractor, page
merge, reconciliation and scoring, and the HS labels through retrieval and the walk, then
reports to `evals/reports/latest/` (`report.md`, `report.json`, three charts):

- per-field accuracy (overall, per variant, per document field) and line-item precision,
  recall and F1;
- reconciliation recall on the mismatch packs, false-alarm rate on the others;
- ECE before and after calibration on the test split, with a reliability diagram, and the
  precision/coverage curve with the auto-approve threshold (the lowest calibrated confidence
  whose precision reaches `target_precision` on the calibration split);
- shipment-level auto-approve rate and precision (extraction only);
- HS accuracy at 2, 4 and 6 digits, retrieval recall@20, HS ECE and its reliability diagram;
- p95 latency, tokens and USD per page (and per HS description), from the recorded calls.

It fails when a metric breaks its gate in `thresholds.yaml`, or when a committed calibrator
(`libs/domain/src/manifest_domain/calibrators/`) differs from a refit: run `make calibrate`.

## Where the answers come from

**The committed cassettes were seeded offline, not recorded from a model.**

- Extraction: a ground-truth oracle answers each page request with the true values *as
  printed* and an evidence span from the page. On scanned pages it copies what OCR produced
  (closest match, less confident the further it is from the truth), so scanned accuracy
  reflects real OCR damage to a literal copier.
- HS: retrieval is offline BM25 over the nomenclature (`offline-lexical`); the stand-in model
  picks the labelled code when it was retrieved, and guesses otherwise.

So the `plumbing` profile applies: the numbers prove the machinery (normalisation, evidence,
reconciliation, calibration, routing, the gate) and say nothing about a model. Latency and cost
are not measured. Calibrators fitted on these answers say `provenance: oracle` and the worker
never auto-approves on them.

To measure a model: set `ANTHROPIC_API_KEY` (and `VOYAGE_API_KEY` with `make up && make migrate
&& make hs-index` for vector retrieval), run `make eval-record`, review the cassette and
calibrator diff, and confirm the `model` gates in `thresholds.yaml`. At Sonnet 5.5 list prices
(`manifest_llm.pricing`) a full recording costs roughly $10-15.
