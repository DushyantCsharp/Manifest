# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# The API: FastAPI under uvicorn on :8010. Build from the repository root:
#   docker build -f services/api/Dockerfile -t manifest-api .
# Base images are pinned by digest; bump them together with services/worker/Dockerfile.

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
    uv sync --frozen --no-dev --no-install-workspace --package manifest-api

# Then the workspace packages, as wheels (not editable), so the runtime needs only the venv.
COPY libs/ libs/
COPY services/api/ services/api/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package manifest-api

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 manifest \
    && useradd --system --uid 10001 --gid manifest --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin manifest
# Owned by root: the app can read and run its code, not change it.
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001:10001
EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=4)"]
CMD ["uvicorn", "manifest_api.main:app", "--host", "0.0.0.0", "--port", "8010"]
