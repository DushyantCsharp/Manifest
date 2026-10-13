# Manifest

Takes a shipment's document pack (commercial invoice, packing list, bill of lading), extracts
typed fields and line items, reconciles them across documents, and suggests a 6-digit HS 2022
code per line item with retrieved justification. Low-confidence or inconsistent results route
to a human review queue; high-confidence results auto-approve.

**The point is calibrated confidence.** Extraction is table stakes; the product is knowing which
extractions to trust and measuring that. When choosing between designs, prefer the one that
keeps confidence measurable by `evals/`.

## Layout

| Path | Import package | Role |
|---|---|---|
| `libs/domain` | `manifest_domain` | Pydantic v2 models, versioned extraction schemas (`schemas/v1`..`v3`), normalisers, evidence check, reconciliation, scoring, calibration (fitted calibrators in `calibrators/`), routing |
| `libs/db` | `manifest_db` | SQLAlchemy 2.0 async models (`models.py`), Alembic migrations, vendored HS 2022 data |
| `libs/llm` | `manifest_llm` | LLM and embedding providers (Anthropic, Voyage), Redis token bucket, structured output, cassettes, prices |
| `libs/hs` | `manifest_hs` | HS 2022 tree, hybrid retrieval (pgvector + full-text, fused), the chapter → heading → subheading walk, index build |
| `libs/telemetry` | `manifest_telemetry` | OpenTelemetry: providers and OTLP export (`configure`), library instrumentation, `gen_ai` spans, every metric's definition (`metrics`), the pipeline stage in context |
| `services/api` | `manifest_api` | FastAPI: REST under `/v1`, Strawberry GraphQL at `/graphql`; production adapters in `adapters/` |
| `services/worker` | `manifest_worker` | Celery app; one module per pipeline stage in `tasks/`; data backfills in `backfills/` |
| `evals` | `manifest_evals` | Frozen corpus (`golden/`), labels (`labels/`, incl. `hs.jsonl`), cassettes, splits, `thresholds.yaml`, harness (`run.py`), synthetic generator (`synth/`) |
| `contracts/` | — | **Generated, committed:** `openapi.json` (OpenAPI 3.1) and `schema.graphql` |
| `clients/python` | `manifest_client` | **Generated** REST SDK (openapi-python-client) |
| `clients/graphql` | `manifest_review_client` | **Generated** GraphQL client (ariadne-codegen); operations in `queries/` |
| `web/review` | — | Reviewer UI; for now only **generated** REST types in `src/generated/` |
| `tools/codegen` | — | Pinned client generators, locked outside the workspace |
| `scripts/contracts.py` | — | Export contracts, regenerate clients, drift check (`make contracts[-check]`) |
| `infra` | — | docker compose for the local stack (profile `app`: the api, worker and monitor images); Grafana dashboards (`grafana/dashboards`), Prometheus alert rules and their tests (`prometheus/rules`, `prometheus/tests`) |
| `tools/loadtest`, `scripts/loadtest.py` | — | Locust load test (own lock), targets (`targets.yaml`) and reports (`reports/`) |
| `services/*/Dockerfile` | — | Release images (`manifest-api`, `manifest-worker`); context is the repo root, `.dockerignore` is an allowlist |
| `.github/workflows` | — | `ci.yml` (checks), `eval.yml` (replay + live evals, PR comments), `release.yml` (images on `v*` tags) |

## Rules

### Dependencies — enforced by `lint-imports` (contracts in root `pyproject.toml`)
- Services depend on libs. **Libs never import services** (or `evals`).
- `manifest_api` and `manifest_worker` never import each other; they meet at the broker and the
  database.
- `manifest_domain` imports no other `manifest_*` package and no infrastructure.
- **LLM calls go only through `libs/llm`.** No other package imports a provider SDK. The token
  bucket, retries, structured-output validation and cost/latency telemetry live there; a call
  that bypasses it breaks backpressure and is invisible to evals.

### Money and quantities
- All money is `Decimal` **plus** an ISO 4217 currency code. Never a bare amount, never `float`.
- No `float` for quantities, weights or prices — `Decimal`, with weights normalised to kg.
  `libs/domain/tests/test_no_float.py` fails if any domain model has a float field.
- Dates are `datetime.date` (ISO 8601). Datetimes are timezone-aware UTC (ruff `DTZ` enforces).

### Extraction schemas
- **Every extraction row carries `schema_version`.**
- A released `schemas/vN` is frozen. Changing what is extracted means a new version, an Alembic
  expand/contract migration, and a backfill that reprocesses only affected documents.
- Re-extraction inserts a new `extractions` row and sets `superseded_at` on the old one; one
  current extraction per document is enforced by a partial unique index.
- v2 (`line_items.country_of_origin`) deploys in three steps: ship 0003 + 0004 with v2-writing
  code, run `make backfill-origin` until it completes, then ship 0005 (it refuses to apply while
  any current v1 document still lacks origins). A missing origin on a v2 line stays NULL and is
  flagged by reconciliation, never filled with a sentinel.
- v3 (provenance: every value's page, verbatim evidence and confidence signals) deploys the same
  way: ship 0007 with v3-writing code, run `make backfill-v3` until it completes, then ship
  0008 (it refuses while a current pre-v3 extraction remains on a document that didn't fail).
- `schemas/vN` freezes what is extracted (fields, normalisation kinds). How the model is asked
  (system prompt, field descriptions) is the extractor's, versioned as `PROMPT_VERSION`:
  rewording a prompt changes cassette keys and needs a recalibration, not a schema version.

### API contracts
- `contracts/` and every generated client are build outputs: never hand-edit them. Change the
  app (or `clients/graphql/queries/`), run `make contracts`, commit everything it touched.
  `make contracts-check` (part of `make check`) fails on any drift.
- Every REST error is RFC 9457 `application/problem+json`. Handlers and ports raise the
  exceptions in `manifest_api.errors`; `manifest_api.problems` maps them to problem types.
  Never build an error response by hand.
- Every POST takes a required `Idempotency-Key` and runs through `run_idempotent`.
- Lists use keyset cursors from `manifest_api.pagination` over the queries in
  `manifest_db.keyset`; never offset pagination.
- Handlers and resolvers call only the Protocols in `manifest_api.ports`. Production adapters
  live in `manifest_api.adapters` (Postgres via asyncpg, presigned S3 multipart uploads, Redis
  idempotency keys, pending-pages admission, the broker) and are built by
  `create_app(wired=True)`, which `uvicorn manifest_api.main:app` serves. The review port has
  no adapter yet; an unwired port answers 501. Tests inject in-memory fakes
  (`services/api/tests/conftest.py`) or the real adapters on testcontainers.
- The API hands work to the worker only by publishing `manifest.classify` (exchange `manifest`,
  routing key `classify`) through Celery's protocol, after the document is committed as
  `queued`; it declares nothing (the worker owns the topology). A worker test checks the names.
- GraphQL mutations return expected failures as `userErrors`, not GraphQL errors.

### Pipeline (`services/worker`)
- Four stage queues, `classify` → chord(`page_extract` × pages) → `reconcile` → `hs_classify`,
  each a quorum queue (delivery limit 5) dead-lettering to `<stage>.dlq`
  (`manifest_worker/topology.py`). Tasks use `PipelineTask`; never retry by hand.
- Failures are sorted in `manifest_worker/errors.py`: transient → autoretry with jittered
  backoff (max 4), then parked in the DLQ; content (invalid output, unreadable PDF) → no retry,
  review; rejected (credentials, unwired adapter) or poison → parked. Rate limits re-queue
  with a countdown (`defer`) and never spend a retry.
- `page_extract` is a chord header: it must always return, even when its page fails, or
  reconcile never runs. Every write is an upsert keyed by (page, schema version) or
  (document, schema version), so reruns and redrives overwrite instead of duplicating.
- LLM calls go through `manifest_llm.LlmGateway` (shared Redis RPM/TPM buckets per
  provider/model). PDFium calls go through the lock in `manifest_worker/text.py`.
- Page extraction (`manifest_worker/extraction.py`): structured outputs (`output_config`
  JSON schema; current models reject forced tool calls) fill the v3 model per document type.
  For every field the model copies the value *as printed*, a verbatim evidence span and a
  confidence; code normalises values (`manifest_domain.normalisers`), checks the span against
  the page text (`manifest_domain.evidence`: no verified evidence, confidence 0) and stamps the
  page. Context resolves ambiguity (`1,234`, `04/07/2026`) at a confidence penalty; an
  unnormalisable value becomes missing with confidence 0. Wired when `ANTHROPIC_API_KEY` is
  set, or replays `MANIFEST_LLM_CASSETTES` (`MANIFEST_LLM_CASSETTE_MODE=replay|record|new`).
- Per shipment (`manifest_worker/shipments.py`): `reconcile` always hands over to
  `reconcile_shipment` (same queue), which waits until every document is done, then records
  discrepancies (upserted on their natural key, so resolutions survive) and rescores values with
  cross-document agreement and the pinned calibrator. A fingerprint of the current extractions
  makes repeats and stale runs no-ops. `hs_classify(shipment, fingerprint)` classifies the
  invoice lines (packing list if there is no invoice), then routes: review, or auto-approve
  only when every value and HS suggestion clears a **trusted** calibrator's threshold and
  nothing is open. Locks go documents (by id) → extractions → review task → shipment.
- HS (`manifest_hs`, adapter `manifest_worker/hs.py`): hybrid retrieval returns 20 subheadings;
  the model walks chapter → heading → subheading among them, citing what it relied on (a
  citation of anything not shown zeroes that step); "none" leaves the line unclassified
  (HS_UNCERTAIN). Wired with a model; vector retrieval needs `VOYAGE_API_KEY` and
  `make hs-index`, else retrieval is full-text only.

- RabbitMQ 4 refuses Celery's transient control queues: remote control is off, and workers
  run `--without-gossip --without-mingle`. Inspect queues in the management UI, dead letters
  with `make dlq`.
- Prefork needs `fork`: on macOS billiard spawns its children, which can't run tasks, so run
  the worker in its container there (`make app`) or with `--pool threads`.
- For load tests: `MANIFEST_LLM_REPLAY_LATENCY=true` makes cassette replays take their
  recorded time, `MANIFEST_HS_RETRIEVALS` replays HS retrieval from recordings (so walk
  prompts, and their recorded answers, match), and `MANIFEST_HS_ENABLED=false` skips the walk
  (lines route to review as HS_UNCERTAIN).
- `hs_classify` walks a shipment's lines one after another in one task: about 10 s per line
  with a live model, so a 3-line pack spends ~30 s there (see the load-test reports).

### Telemetry (`libs/telemetry`)
- One `configure(service)` per process, before anything creates an engine or client: the API
  in its lifespan, each worker child at `worker_process_init` (never the prefork parent: its
  children would share one `service.instance.id`), the monitor in `main`. OTLP over HTTP to
  `OTEL_EXPORTER_OTLP_ENDPOINT`; `OTEL_SDK_DISABLED=true` turns it all off.
- `instrument()` traces SQLAlchemy, Celery, botocore, redis and httpx; `instrument_fastapi`
  the API. SQLAlchemy is patched at `sqlalchemy.create_engine`, so engines are made through the
  module attribute (`manifest_db.session`); the instrumentation predates SQLAlchemy 2.1 and is
  enabled with `skip_dep_check` (the trace test proves it works).
- Trace context rides Celery's message headers, so one trace runs from the finalize request
  through classify, every page_extract of the chord, each model call and reconcile
  (`services/worker/tests/test_tracing.py` asserts it). Publish through Celery, never raw kombu.
- Every model call is a `gen_ai.*` span in `LlmGateway` (operation, provider, model, tokens,
  finish reason; error type on failure). Prompts and answers are personal data: captured only
  with `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`.
- Every metric is defined in `manifest_telemetry.metrics` (the dashboards and alerts read those
  names); add new ones there. Tokens and cost carry the pipeline stage, which `PipelineTask`
  sets for each task. Cost is list price (`manifest_llm.pricing`), a float only in the metric.
- System state (queue and DLQ depth, review queue age, auto-approve ratio) comes from exactly one
  `make monitor` process; queries take `max by (queue)`, never the sum.
- Alerts are Prometheus rules (`infra/prometheus/rules`) with promtool unit tests
  (`make infra-check`, in CI): DLQ growth over 10 minutes, a review task older than 4 hours,
  and the monitor going quiet (without it neither of the others can fire).

### Confidence and calibration
- Confidences combine only through `manifest_domain.confidence` (corroborate, contradict,
  settle, joint, weakest) and are built by the recipe in `manifest_domain.scoring`
  (`SCORE_VERSION`): self-report x normaliser factor, settled across pages, corroborated (never
  lowered) by agreeing documents, then calibrated, then 0 without verified evidence.
- Calibrators (`extraction`, `hs`) are isotonic fits on the calibration split, committed as
  `libs/domain/src/manifest_domain/calibrators/<name>-v<N>.json` by `make calibrate`: build
  outputs, never hand-edited. `make eval` fails if a committed calibrator differs from a refit.
- The worker scores with the calibrators pinned in settings (`MANIFEST_EXTRACTION_CALIBRATOR`,
  `MANIFEST_HS_CALIBRATOR`); unpinned, confidences stay raw. Routing **fails closed**: only a
  calibrator fitted on a real model's answers (`provenance: model`), to the current scoring
  recipe and schema, sets an auto-approve threshold. Oracle-fitted calibrators never do.

### Migrations
- Never edit an applied migration. Use expand/contract for anything a running deploy reads.
- Revisions are numbered `0001`, `0002`, … (`alembic revision --rev-id 0006 -m ...`).
- Migrations are frozen snapshots: never import `manifest_db.models` or domain enums into one;
  spell out enum labels and types.
- Constraint names come from `manifest_db.base.NAMING_CONVENTION`; never rely on generated names.
- Long data backfills never run inside a migration: register them in `backfill_runs`, run them
  out of band (`manifest_worker.backfills`: resumable, single-runner, rate-limited), and guard
  the contract migration on completion.
- History stays linear — a test fails on multiple Alembic heads; another fails if models and
  migrations drift (`alembic check`), and a stairway test downgrades and re-applies every step.
- Reference data ships as vendored, checksummed files (HS 2022: see
  `libs/db/src/manifest_db/migrations/data/hs2022/SOURCE.md`), never fetched at migrate time.

### CI, evals in CI, releases (`.github/workflows`)
- Every action is pinned to a commit SHA (`# vX.Y.Z` beside it) and every image to a digest;
  pick releases at least two weeks old. Workflows start from `permissions: {}` and each job
  asks only for what it uses; secrets go to the step that needs them, never the job.
- `ci.yml` runs `make lint`, `make typecheck`, `make test`, the integration suite with Alembic
  up → check → down → up, `make contracts-check` and a 25-example schemathesis smoke, all via the
  Makefile. Docs-only changes skip them; **`ci-ok` is the check to require**. Integration in CI
  uses service containers: `conftest.py` takes `MANIFEST_TEST_*` addresses instead of starting
  testcontainers. Keep the images in `conftest.py`, `infra/compose.yaml` and the workflows in step.
- `eval.yml` replays the cassettes on every PR and push to main (free, gated). Live evals call
  the real models (~$4 a run; secrets `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`): nightly, by
  `workflow_dispatch`, and when a maintainer adds the `run-evals` label to a same-repo PR
  (re-add to rerun). Reports are artifacts (`eval-replay` from pushes to main, `eval-live` from
  nightlies, 90 days); a PR gets one comment per mode with deltas against main's last report
  (`python -m manifest_evals.compare`), updated in place.
- `release.yml` on a `v*` tag builds both images for amd64, smoke-tests them (API `/healthz`, a
  real OCR in the worker), and gates on Trivy: a fixable CRITICAL CVE fails the release (unfixed
  ones are listed in the summary). Only when both pass does it push amd64 + arm64 to
  `ghcr.io/<owner>/manifest-{api,worker}` with an SBOM and `provenance: mode=max`.
- Images: `uv sync --frozen --no-dev --no-editable --package manifest-<service>` in the builder,
  python slim at runtime, uid 10001, venv owned by root; tesseract only in the worker. An image
  has exactly what its package declares: a module the workspace happens to install (through
  `evals` or dev tools) is missing in production, so declare every runtime import.

## Commands

```
make sync              uv sync (Python 3.12, all workspace members + dev tools)
make up / down / nuke  local stack (nuke also deletes volumes)
make migrate           alembic upgrade head
make seed              generate the synthetic document packs into evals/ (40 packs, 15 mismatched)
make dlq ARGS="..."    dead letters: list | show ID | diff A B | redrive --id ID [--dry-run]
make backfill-origin   re-extract v1 documents lacking line-item origin (unblocks migration 0005)
make backfill-v3       re-extract documents not yet on schema v3 (unblocks migration 0008)
make hs-index          embed the HS subheadings for vector retrieval (VOYAGE_API_KEY; resumable)
make api / worker      run locally (API on :8010; the worker needs Linux for prefork)
make monitor          queue/DLQ depth, review queue age, auto-approve ratio (run exactly one)
make app              the api, worker and monitor images against the local stack
make test              unit tests + 85% coverage gate on libs/ (no infra, no Docker)
make test-integration  @pytest.mark.integration: real services via testcontainers (Docker)
make test-contract     schemathesis fuzzes the REST API against contracts/openapi.json
make test-live         golden packs through a real model (ANTHROPIC_API_KEY; RECORD=1 to keep)
make coverage          every non-live test (Docker) + coverage gate, HTML in htmlcov/
make corpus            rewrite the frozen eval corpus (55 packs + page snapshots + splits)
make cassettes         replace every eval cassette with the ground-truth oracle's answers
                       (offline; discards real recordings: the gate falls to `plumbing`)
make eval              replay the corpus and HS labels, report to evals/reports/latest, gate on
                       evals/thresholds.yaml and on calibrator drift (part of make check)
make calibrate         refit the calibrators and commit new versions when they change
make eval-record       re-record every cassette from a real model (keys; costs money), refit
make images            build the api and worker images for this machine (as release.yml does)
make lint / fmt        ruff + import contracts
make infra-check       promtool: check and unit-test the alert rules (Docker)
make loadtest         Locust against the app images, replaying recorded model answers at
                       their recorded latency; report vs tools/loadtest/targets.yaml
make loadtest-record-hs  record the load test's HS walks and retrievals (live models, ~$1.50;
                       again whenever the walk's prompt, model or the HS index changes)
make demo              re-record docs/demo.gif (vhs): a burst shed by admission, a storage
                       outage retried through, on the real containers with replayed answers
make readme            regenerate README.md's targets, cost and 10x tables from the latest
                       eval report and load-test reports (`--check` fails when stale)
make typecheck         mypy --strict on libs/
make contracts         export contracts/ from the app + regenerate all typed clients
make contracts-check   fail if contracts/ or clients drifted (CI); `uv run python
                       scripts/contracts.py check --base-url URL` checks a running app
make check             lint + typecheck + test + test-contract + contracts-check + eval — run before
                       calling work done (and test-integration when you touched db/worker/llm)
```

## README
- `README.md`'s tables between `<!-- generated:NAME -->` markers are build outputs of
  `scripts/readme.py`: never hand-edit them. They carry the commit and date of the reports they
  came from (eval `report.json` and load-test reports stamp `commit`, `dirty`, `generated_at`),
  so regenerate after `make eval` and `make loadtest` on a committed tree.
- Architecture decisions live in `docs/adr/`; add an ADR when a decision of that weight changes.

## Testing
- **Markers** (`--strict-markers`; the default run excludes the last three): unmarked = unit,
  `integration` (Docker), `contract` (schemathesis, via the `tools/codegen` env), `live` (calls a
  real LLM; never in CI by default).
- **Integration** uses testcontainers from the root `conftest.py` (`postgres_url`, `redis_url`,
  `broker`, `object_store`): session-scoped, lazy, same images as `infra/compose.yaml`. Never
  depend on `make up` in a test. Each test makes its own database (`scratch_database`), Redis
  namespace or RabbitMQ vhost, so CI can share one server per fixture (`MANIFEST_TEST_*`).
- **Telemetry in tests**: `manifest_telemetry.testing.capture()` installs in-memory span and
  metric collection for the test process (once); metrics are cumulative, so tests filter on
  attributes they make unique (a model name, a window) or compare before and after.
- **Property tests** (hypothesis) for anything with an algebra: normalisers, alignment, confidence.
  `HYPOTHESIS_PROFILE=ci` runs more examples, derandomised.
- **LLM calls in tests are replayed**, never made: `manifest_llm.cassette.CassetteProvider` keys
  each request by SHA-256 of (provider, model, system, prompt, schema, max_tokens, effort). The
  eval corpus (`evals/golden/{clean,scan,mism}-*`, frozen: `make corpus`) carries each page's
  text as the pipeline read it (`pages.json`), so replays don't depend on PDFium or tesseract
  versions; HS retrieval is recorded per query too (`evals/cassettes/hs-retrieval`). Changing
  the prompt, schema, model or page text changes the hashes: `evals/tests/test_golden.py`
  fails first; re-record with `make eval-record` (needs keys, costs money) and review the diff.
- **Committed answers are a real model's** (`recorded_with: anthropic`; HS retrieval `hybrid`),
  so `make eval` gates on the `model` profile of `evals/thresholds.yaml`: regression floors
  under the recorded baseline, with unmet product goals kept as `target` (reported, not
  enforced). Any stand-in answer (the ground-truth oracle, offline BM25) drops the gate to the
  `plumbing` profile. Machinery tests seed the oracle into a temporary folder instead.
- **Coverage**: libs/ only, branch coverage, gate 85% (`[tool.coverage]` in `pyproject.toml`).

## Conventions
- uv workspace; packages use a src layout and `uv_build`. Add a dependency to one package with
  `uv add --package manifest-<name> <dep>`; dev tools with `uv add --dev <dep>`.
- Tests live in each package's `tests/`. Anything needing Postgres/RabbitMQ/Redis/MinIO is marked
  `@pytest.mark.integration`; `make test` must pass with the stack down and Docker off.
- Absolute imports only (ruff `TID252`).
- Synthetic packs need Pango for WeasyPrint (`brew install pango`; Debian/Ubuntu:
  `libpango-1.0-0 libpangoft2-1.0-0`). On macOS the Makefile sets `DYLD_FALLBACK_LIBRARY_PATH`;
  plain `uv run pytest` skips the one rendering test if Pango can't be found.
- Tests needing a database use `manifest_db.testing.scratch_database(postgres_url)`: a throwaway
  database on the testcontainers server, migrated by the test itself.
- Local stack: Postgres `127.0.0.1:5440`, RabbitMQ `5672` (UI `15672`), Redis `6379`, MinIO
  `9000` (console `9001`), OTLP `4317/4318`, Prometheus `9090`, Grafana `3000`. Override in `.env`.
