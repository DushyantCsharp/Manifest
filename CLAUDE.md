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
| `libs/telemetry` | `manifest_telemetry` | OpenTelemetry tracing/metrics, structured logging |
| `services/api` | `manifest_api` | FastAPI: REST under `/v1`, Strawberry GraphQL at `/graphql` |
| `services/worker` | `manifest_worker` | Celery app; one module per pipeline stage in `tasks/`; data backfills in `backfills/` |
| `evals` | `manifest_evals` | Frozen corpus (`golden/`), labels (`labels/`, incl. `hs.jsonl`), cassettes, splits, `thresholds.yaml`, harness (`run.py`), synthetic generator (`synth/`) |
| `contracts/` | — | **Generated, committed:** `openapi.json` (OpenAPI 3.1) and `schema.graphql` |
| `clients/python` | `manifest_client` | **Generated** REST SDK (openapi-python-client) |
| `clients/graphql` | `manifest_review_client` | **Generated** GraphQL client (ariadne-codegen); operations in `queries/` |
| `web/review` | — | Reviewer UI; for now only **generated** REST types in `src/generated/` |
| `tools/codegen` | — | Pinned client generators, locked outside the workspace |
| `scripts/contracts.py` | — | Export contracts, regenerate clients, drift check (`make contracts[-check]`) |
| `infra` | — | docker compose for the local stack |

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
- Handlers and resolvers call only the Protocols in `manifest_api.ports`. A port with no
  adapter answers 501; tests inject in-memory fakes (`services/api/tests/conftest.py`).
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
make api / worker      run locally (API on :8010)
make test              unit tests + 85% coverage gate on libs/ (no infra, no Docker)
make test-integration  @pytest.mark.integration: real services via testcontainers (Docker)
make test-contract     schemathesis fuzzes the REST API against contracts/openapi.json
make test-live         golden packs through a real model (ANTHROPIC_API_KEY; RECORD=1 to keep)
make coverage          every non-live test (Docker) + coverage gate, HTML in htmlcov/
make corpus            rewrite the frozen eval corpus (55 packs + page snapshots + splits)
make cassettes         reseed every eval cassette from ground truth (offline), then refit
make eval              replay the corpus and HS labels, report to evals/reports/latest, gate on
                       evals/thresholds.yaml and on calibrator drift (part of make check)
make calibrate         refit the calibrators and commit new versions when they change
make eval-record       re-record every cassette from a real model (keys; costs money), refit
make lint / fmt        ruff + import contracts
make typecheck         mypy --strict on libs/
make contracts         export contracts/ from the app + regenerate all typed clients
make contracts-check   fail if contracts/ or clients drifted (CI); `uv run python
                       scripts/contracts.py check --base-url URL` checks a running app
make check             lint + typecheck + test + test-contract + contracts-check + eval — run before
                       calling work done (and test-integration when you touched db/worker/llm)
```

## Testing
- **Markers** (`--strict-markers`; the default run excludes the last three): unmarked = unit,
  `integration` (Docker), `contract` (schemathesis, via the `tools/codegen` env), `live` (calls a
  real LLM; never in CI by default).
- **Integration** uses testcontainers from the root `conftest.py` (`postgres_url`, `redis_url`,
  `broker`, `object_store`): session-scoped, lazy, same images as `infra/compose.yaml`. Never
  depend on `make up` in a test. Each test makes its own database (`scratch_database`), Redis
  namespace or RabbitMQ vhost.
- **Property tests** (hypothesis) for anything with an algebra: normalisers, alignment, confidence.
  `HYPOTHESIS_PROFILE=ci` runs more examples, derandomised.
- **LLM calls in tests are replayed**, never made: `manifest_llm.cassette.CassetteProvider` keys
  each request by SHA-256 of (provider, model, system, prompt, schema, max_tokens, effort). The
  eval corpus (`evals/golden/{clean,scan,mism}-*`, frozen: `make corpus`) carries each page's
  text as the pipeline read it (`pages.json`), so replays don't depend on PDFium or tesseract
  versions; HS retrieval is recorded per query too (`evals/cassettes/hs-retrieval`). Changing
  the prompt, schema, model or page text changes the hashes: `evals/tests/test_golden.py`
  fails first; reseed with `make cassettes` or re-record with `make eval-record` and review.
- **Committed answers are seeded by a ground-truth oracle** until `make eval-record` runs
  (`recorded_with` says so). The harness then gates on the `plumbing` profile of
  `evals/thresholds.yaml` (machinery, not a model); real recordings switch it to `model`.
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
