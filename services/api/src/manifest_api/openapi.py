"""The REST contract as FastAPI generates it (OpenAPI 3.1), plus the problem+json media type."""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from manifest_api.problems import PROBLEM_MEDIA_TYPE, PROBLEM_TYPES

_PROBLEM_ROWS = "\n".join(
    f"| `{problem.slug}` | {problem.status} | {problem.title} |" for problem in PROBLEM_TYPES
)

DESCRIPTION = f"""\
Upload a shipment's document pack, then poll its processing status. Reviewer workflows are on
the GraphQL endpoint at `/graphql`.

## Uploads
1. `POST /v1/shipments` creates a shipment.
2. `POST /v1/shipments/{{shipment_id}}/documents` returns presigned URLs, one per part; `PUT`
   each byte range straight to storage and keep each response's `ETag`.
3. `POST /v1/shipments/{{shipment_id}}/documents/{{document_id}}:finalize` with the ETags
   queues processing (202). Poll `GET /v1/shipments/{{shipment_id}}`.

## Idempotency
Every POST requires an `Idempotency-Key` header. Retrying with the same key and body returns
the original response (with `Idempotent-Replayed: true`) instead of repeating the action.
Failed requests are not remembered, so retry them with the same key.

## Backpressure
`:finalize` answers 429 with `Retry-After` (seconds) when too many pages are waiting. Nothing
is queued; retry after the delay with the same key.

## Pagination
Lists are keyset-paginated. Pass `next_cursor` back as `cursor`; treat it as opaque.

## Errors
Every 4xx and 5xx body is `application/problem+json` (RFC 9457). `type` is
`https://manifest.example/problems/<slug>`, or `about:blank` when the status says it all.

| slug | status | title |
|---|---|---|
{_PROBLEM_ROWS}
"""

TAGS = [
    {"name": "shipments", "description": "Shipments and their processing status."},
    {"name": "documents", "description": "Document uploads and processing."},
]


def _use_problem_media_type(document: dict[str, Any]) -> None:
    """FastAPI puts every response model under application/json; errors are problem+json."""
    for path_item in document.get("paths", {}).values():
        for operation in path_item.values():
            for status, response in operation.get("responses", {}).items():
                content = response.get("content", {})
                if status[0] in "45" and "application/json" in content:
                    content[PROBLEM_MEDIA_TYPE] = content.pop("application/json")


def install_openapi(app: FastAPI) -> None:
    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            document = get_openapi(
                title=app.title,
                version=app.version,
                openapi_version=app.openapi_version,
                description=app.description,
                routes=app.routes,
                tags=app.openapi_tags,
                separate_input_output_schemas=app.separate_input_output_schemas,
            )
            _use_problem_media_type(document)
            app.openapi_schema = document
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
