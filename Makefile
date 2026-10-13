.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE := docker compose -f infra/compose.yaml $(if $(wildcard .env),--env-file .env,)
API_PORT ?= 8010

# WeasyPrint (synthetic packs) dlopens Pango. uv's Python doesn't search Homebrew's lib dir and
# macOS strips DYLD_* from system binaries such as make and bash, so set it per command.
PANGO := $(if $(wildcard /opt/homebrew/lib/libpango-1.0.dylib),DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib,)

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup

.PHONY: sync
sync: ## Install the workspace into .venv
	uv sync

.PHONY: hooks
hooks: ## Install pre-commit hooks
	uv run pre-commit install

# ---------------------------------------------------------------- infra

.PHONY: up
up: ## Start the local stack, wait until healthy, create the bucket
	$(COMPOSE) up -d --wait
	$(COMPOSE) run --rm minio-init

.PHONY: down
down: ## Stop the local stack (keeps volumes)
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop the local stack and delete its volumes
	$(COMPOSE) down -v

.PHONY: ps
ps: ## Show stack status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Tail stack logs
	$(COMPOSE) logs -f --tail=100

.PHONY: migrate
migrate: ## Apply Alembic migrations to head
	uv run alembic -c libs/db/alembic.ini upgrade head

.PHONY: seed
seed: ## Generate the synthetic document packs into evals/ (deterministic; needs Pango)
	$(PANGO) uv run python -m manifest_evals.synth

.PHONY: corpus
corpus: ## Rewrite the frozen eval corpus (55 packs, page snapshots, splits); then reseed cassettes
	$(PANGO) uv run python -m manifest_evals.synth freeze

.PHONY: dlq
dlq: ## Dead letters: make dlq ARGS="list" | "show ID" | "diff A B" | "redrive --id ID"
	uv run python -m manifest_worker.dlq $(or $(ARGS),list)

.PHONY: backfill-origin
backfill-origin: ## Re-extract v1 documents lacking line-item origin (unblocks migration 0005)
	uv run python -m manifest_worker.backfills country-of-origin

.PHONY: backfill-v3
backfill-v3: ## Re-extract documents not yet on extraction schema v3 (unblocks migration 0008)
	uv run python -m manifest_worker.backfills schema-v3

# ---------------------------------------------------------------- run

.PHONY: api
api: ## Run the API with reload
	uv run uvicorn manifest_api.main:app --reload --port $(API_PORT)

.PHONY: worker
worker: ## Run a Celery worker
	uv run celery -A manifest_worker.app:celery_app worker --loglevel=INFO --without-gossip --without-mingle

# ---------------------------------------------------------------- quality

.PHONY: test
test: ## Unit tests + the 85% coverage gate on libs/ (no infra, no Docker)
	$(PANGO) uv run pytest --cov --cov-report=term

.PHONY: test-integration
test-integration: ## Integration tests on real Postgres/RabbitMQ/Redis/MinIO (testcontainers; needs Docker)
	$(PANGO) uv run pytest -m integration

.PHONY: test-contract
test-contract: ## Schemathesis fuzzes the REST API against contracts/openapi.json
	uv run pytest -m contract

.PHONY: test-live
test-live: ## Golden packs through a real model (needs ANTHROPIC_API_KEY); RECORD=1 rewrites evals/cassettes
	MANIFEST_RECORD_CASSETTES=$(RECORD) uv run pytest -m live

.PHONY: coverage
coverage: ## Every non-live test (needs Docker), libs/ coverage gate + HTML report in htmlcov/
	$(PANGO) uv run pytest -m "not live" --cov --cov-report=term --cov-report=html

.PHONY: cassettes
cassettes: ## Replace every eval cassette with the oracle's answers (offline; discards real recordings)
	uv run python -m manifest_evals.golden seed
	uv run python -m manifest_evals.hs seed
	uv run python -m manifest_evals calibrate

.PHONY: hs-index
hs-index: ## Embed the HS 2022 subheadings for vector retrieval (VOYAGE_API_KEY; resumable)
	uv run python -m manifest_hs.index

.PHONY: eval
eval: ## Replay the eval corpus, write evals/reports/latest, fail on evals/thresholds.yaml
	uv run python -m manifest_evals run

.PHONY: calibrate
calibrate: ## Refit the confidence calibrators on the calibration split and commit a new version
	uv run python -m manifest_evals calibrate

.PHONY: eval-record
eval-record: ## Re-record every cassette from a real model (ANTHROPIC_API_KEY; costs money), then refit
	uv run python -m manifest_evals run --mode record; uv run python -m manifest_evals calibrate

.PHONY: images
images: ## Build the api and worker images for this machine's platform (release.yml pushes amd64+arm64)
	docker build -f services/api/Dockerfile -t manifest-api:dev .
	docker build -f services/worker/Dockerfile -t manifest-worker:dev .

.PHONY: lint
lint: ## Ruff lint + format check + import contracts
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports

.PHONY: fmt
fmt: ## Auto-fix lint and format
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: typecheck
typecheck: ## mypy --strict on libs/
	uv run mypy

.PHONY: contracts
contracts: ## Export contracts/ from the app and regenerate every typed client
	uv run python scripts/contracts.py export
	uv run python scripts/contracts.py generate

.PHONY: contracts-check
contracts-check: ## Fail if contracts/ or generated clients drifted from the app (CI)
	uv run python scripts/contracts.py check

.PHONY: check
check: lint typecheck test test-contract contracts-check eval ## Everything CI runs without Docker
