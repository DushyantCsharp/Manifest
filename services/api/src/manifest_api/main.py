from importlib.metadata import version

from fastapi import FastAPI
from fastapi.routing import APIRoute

from manifest_api.graphql.schema import build_graphql_router
from manifest_api.openapi import DESCRIPTION, TAGS, install_openapi
from manifest_api.ports import Ports
from manifest_api.problems import install_problem_handlers
from manifest_api.rest.router import router as rest_router


def _operation_id(route: APIRoute) -> str:
    # Stable, readable operationIds; generated clients name their methods after them.
    return route.name


def create_app(ports: Ports | None = None) -> FastAPI:
    app = FastAPI(
        title="Manifest API",
        version=version("manifest-api"),
        description=DESCRIPTION,
        openapi_tags=TAGS,
        generate_unique_id_function=_operation_id,
    )
    app.state.ports = ports or Ports.unwired()
    install_problem_handlers(app)
    install_openapi(app)
    app.include_router(rest_router)
    app.include_router(build_graphql_router(), prefix="/graphql", include_in_schema=False)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
