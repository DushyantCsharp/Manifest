"""Schemathesis hooks for ``test_schemathesis.py`` (loaded by ``st`` via SCHEMATHESIS_HOOKS).

Runs inside the tools/codegen environment, not the workspace. It teaches the fuzzer the two
rules of the API that JSON Schema can't state, and nothing more:

- An Idempotency-Key names one logical request. Reusing a key for a different body is a client
  error (422, as draft-ietf-httpapi-idempotency-key-header specifies), so each request gets a
  fresh key. Only keys that are already valid are replaced: invalid ones stay invalid for
  negative testing.
- Two rejections of schema-valid input are excused, by exact problem type and location: an
  unknown pagination cursor (an opaque token, so any string is schema-valid) and duplicate
  part numbers on finalize. Any other failure still fails the run.
"""

import json
import uuid
from typing import Any

import schemathesis

IDEMPOTENCY_KEY = "Idempotency-Key"
PROBLEMS = "https://manifest.example/problems/"


@schemathesis.hook
def before_call(context: Any, case: Any, kwargs: dict[str, Any]) -> None:
    key = (case.headers or {}).get(IDEMPOTENCY_KEY)
    if isinstance(key, str) and 1 <= len(key) <= 255:
        case.headers[IDEMPOTENCY_KEY] = str(uuid.uuid4())


def _problem(response: Any) -> dict[str, Any]:
    try:
        body = json.loads(response.content)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _schema_cannot_say(case: Any, response: Any) -> bool:
    problem = _problem(response)
    operation = f"{case.method.upper()} {case.path}"
    if operation == "GET /v1/shipments" and response.status_code == 400:
        return problem.get("type") == PROBLEMS + "invalid-cursor"
    if operation.endswith(":finalize") and response.status_code == 422:
        issues = problem.get("errors") or []
        return (
            problem.get("type") == PROBLEMS + "validation-error"
            and all(
                issue.get("pointer") == "#/parts"
                and "part numbers must be unique" in issue["detail"]
                for issue in issues
            )
            and bool(issues)
        )
    return False


@schemathesis.hook
def filter_failure(context: Any, failure: Any, case: Any, response: Any) -> bool:
    """Keep every failure except the documented rejections above."""
    rejected_valid_data = type(failure).__name__ == "RejectedPositiveData"
    return not (rejected_valid_data and _schema_cannot_say(case, response))
