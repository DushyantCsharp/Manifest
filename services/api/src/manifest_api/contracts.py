"""The API's contracts, serialised exactly as committed under ``contracts/``.

``scripts/contracts.py`` writes these files and fails CI when they drift from the app.
"""

import json

from manifest_api.graphql.schema import schema
from manifest_api.main import create_app


def openapi_json() -> str:
    return json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"


def graphql_sdl() -> str:
    return schema.as_str().rstrip("\n") + "\n"
