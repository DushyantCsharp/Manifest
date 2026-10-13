"""Anthropic Messages API as a ``Provider``: structured outputs constrained to a JSON schema.

The answer is decoded against the schema (``output_config.format``), so it is valid JSON of the
right shape; the gateway still validates it with Pydantic. Forced tool calls are not used: the
current models reject ``tool_choice`` ``any``/``tool``. The schema is rewritten for the API's
JSON Schema subset (``strict_schema``) inside this provider, so what callers hash and record
(cassette keys) doesn't depend on it.

The SDK's own retries are disabled; errors are classified so the pipeline retries what is
transient (429, 408, 409, 5xx, 529 overloaded, timeouts, connection failures) and stops on what
isn't (bad request, credentials, unknown model). A refusal or an answer cut off at
``max_tokens`` is invalid output: it goes to review rather than being retried as is.
"""

import json
from decimal import Decimal
from typing import Any, cast

import anthropic
from anthropic.types import MessageParam, OutputConfigParam

from manifest_llm.errors import (
    LlmError,
    OutputValidationError,
    PermanentProviderError,
    TransientProviderError,
)
from manifest_llm.gateway import JsonRequest, JsonResponse

TRANSIENT_STATUSES = frozenset({408, 409, 429, 529})

# Keywords the structured-output schema subset rejects (numeric and length bounds, ...).
_UNSUPPORTED = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "patternProperties",
        "propertyNames",
    }
)
_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)
# Regex features the subset rejects: lookaround, backreferences, word boundaries.
_UNSUPPORTED_REGEX = ("(?=", "(?!", "(?<", "\\b", "\\B", *(f"\\{n}" for n in range(1, 10)))


def strict_schema(node: Any) -> Any:
    """``node`` rewritten for structured outputs: unsupported constraints dropped (Pydantic
    still enforces them on the answer) and every object closed with
    ``additionalProperties: false``. An object that allows arbitrary extra keys can't be
    expressed, so it raises rather than silently changing meaning.
    """
    if isinstance(node, list):
        return [strict_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED:
            continue
        if key == "minItems" and value not in (0, 1):
            continue
        if key == "format" and value not in _FORMATS:
            continue
        if key == "pattern" and any(token in str(value) for token in _UNSUPPORTED_REGEX):
            continue
        if key in ("properties", "$defs", "definitions"):
            out[key] = {name: strict_schema(child) for name, child in value.items()}
        else:
            out[key] = strict_schema(value)
    if out.get("type") == "object" or "properties" in out:
        extra = out.get("additionalProperties", False)
        if extra not in (False, True):
            raise PermanentProviderError("structured outputs can't express open-ended objects")
        out["additionalProperties"] = False
    return out


def classify(exc: anthropic.APIError) -> LlmError:
    """Map an SDK error onto what the pipeline should do about it."""
    if isinstance(exc, anthropic.APIConnectionError):  # includes timeouts
        return TransientProviderError(f"anthropic unreachable: {exc}")
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if status in TRANSIENT_STATUSES or status >= 500:
            header = exc.response.headers.get("retry-after")
            retry_after = float(header) if header and header.replace(".", "", 1).isdigit() else None
            return TransientProviderError(
                f"anthropic {status}: {exc.message}", status=status, retry_after=retry_after
            )
        return PermanentProviderError(f"anthropic {status}: {exc.message}", status=status)
    return PermanentProviderError(f"anthropic: {exc}")


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 120,
    ) -> None:
        """``api_key`` defaults to the ANTHROPIC_API_KEY environment variable."""
        self._client = client or anthropic.Anthropic(
            api_key=api_key, max_retries=0, timeout=timeout
        )

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": strict_schema(request.schema)}
        }
        if request.effort is not None:
            config["effort"] = request.effort
        messages: list[MessageParam] = [{"role": "user", "content": request.prompt}]
        try:
            message = self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=messages,
                output_config=cast(OutputConfigParam, config),
            )
        except anthropic.APIError as exc:
            raise classify(exc) from exc
        if message.stop_reason in ("refusal", "max_tokens"):
            raise OutputValidationError(
                f"{request.model} stopped with {message.stop_reason}",
                finish_reason=message.stop_reason,
            )
        text = "".join(block.text for block in message.content if block.type == "text")
        try:
            data = json.loads(text, parse_float=Decimal)  # never a float
        except json.JSONDecodeError as exc:
            raise OutputValidationError(f"{request.model} answered invalid JSON: {exc}") from exc
        return JsonResponse(
            data,
            message.usage.input_tokens,
            message.usage.output_tokens,
            finish_reason=message.stop_reason,
            response_model=message.model,
        )
