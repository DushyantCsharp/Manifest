# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# The pipeline worker: Celery, with tesseract for scanned pages (the only image that has it).
# Build from the repository root:
#   docker build -f services/worker/Dockerfile -t manifest-worker .
# Base images are pinned by digest; bump them together with services/api/Dockerfile.

ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.14@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------- builder: /app/.venv
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Third-party dependencies from the lockfile alone, so this layer survives code changes.
COPY pyproject.toml uv.lock ./
COPY libs/domain/pyproject.toml libs/domain/
COPY libs/db/pyproject.toml libs/db/
COPY libs/llm/pyproject.toml libs/llm/
COPY libs/hs/pyproject.toml libs/hs/
COPY libs/telemetry/pyproject.toml libs/telemetry/
COPY services/api/pyproject.toml services/api/
COPY services/worker/pyproject.toml services/worker/
COPY evals/pyproject.toml evals/
COPY clients/python/pyproject.toml clients/python/
COPY clients/graphql/pyproject.toml clients/graphql/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package manifest-worker

# Then the workspace packages, as wheels (not editable), so the runtime needs only the venv.
COPY libs/ libs/
COPY services/worker/ services/worker/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package manifest-worker

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
# tesseract (English data) for pages without a text layer: manifest_worker.text.tesseract_ocr.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 manifest \
    && useradd --system --uid 10001 --gid manifest --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin manifest
# Owned by root: the worker can read and run its code, not change it.
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001:10001
# RabbitMQ 4 refuses Celery's transient control queues (see CLAUDE.md), so no gossip or mingle,
# and no `celery inspect ping` health check either: probe the broker and queues instead.
CMD ["celery", "-A", "manifest_worker.app:celery_app", "worker", "--loglevel=INFO", "--without-gossip", "--without-mingle"]
