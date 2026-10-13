"""The FastAPI app. ``uvicorn manifest_api.main:app`` serves the wired one.

``create_app(ports)`` (tests) uses the ports it is given; ``create_app()`` leaves every port
unwired (501); ``create_app(wired=True)`` builds the production adapters at startup and turns
on telemetry: a server span per request, continued by the database, Redis, S3 and broker
spans it causes, and by the worker that picks up the published task.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from fastapi.routing import APIRoute

import manifest_telemetry
from manifest_api.adapters.settings import ApiSettings
from manifest_api.graphql.schema import build_graphql_router
from manifest_api.openapi import DESCRIPTION, TAGS, install_openapi
from manifest_api.ports import Ports
from manifest_api.problems import install_problem_handlers
from manifest_api.rest.router import router as rest_router


def _operation_id(route: APIRoute) -> str:
    # Stable, readable operationIds; generated clients name their methods after them.
    return route.name


@asynccontextmanager
async def _wired(app: FastAPI) -> AsyncIterator[None]:
    from manifest_api.adapters.wiring import build

    if manifest_telemetry.configure("manifest-api"):
        manifest_telemetry.instrument()  # before the adapters create their engine and clients
    ports, close = build(ApiSettings())
    app.state.ports = ports
    try:
        yield
    finally:
        await close()
        manifest_telemetry.shutdown()


def create_app(ports: Ports | None = None, *, wired: bool = False) -> FastAPI:
    app = FastAPI(
        title="Manifest API",
        version=version("manifest-api"),
        description=DESCRIPTION,
        openapi_tags=TAGS,
        generate_unique_id_function=_operation_id,
        lifespan=_wired if wired else None,
    )
    app.state.ports = ports or Ports.unwired()
    if wired:
        manifest_telemetry.instrument_fastapi(app)  # middleware: before the app starts
    install_problem_handlers(app)
    install_openapi(app)
    app.include_router(rest_router)
    app.include_router(build_graphql_router(), prefix="/graphql", include_in_schema=False)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app(wired=True)
