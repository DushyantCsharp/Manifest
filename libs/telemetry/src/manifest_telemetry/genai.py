"""One span per model call, in the OpenTelemetry semantic conventions for generative AI.

The span is named ``{operation} {model}`` (``chat claude-sonnet-5-5``), is a CLIENT span, and
carries ``gen_ai.operation.name``, ``gen_ai.provider.name``, ``gen_ai.request.model``,
``gen_ai.request.max_tokens``, ``gen_ai.output.type``, then from the answer
``gen_ai.response.model``, ``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens`` and
``gen_ai.response.finish_reasons``. A failed call is ``error.type`` plus the recorded
exception. The call's duration and token usage also go to the conventions' two histograms.

Prompts and answers are personal data in this domain (names, addresses, prices), so they are
captured only when ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true``, as
``gen_ai.system_instructions``, ``gen_ai.input.messages`` and ``gen_ai.output.messages``.
"""

import json
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.trace import Span, SpanKind

from manifest_telemetry import metrics

CAPTURE_CONTENT = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
ERROR_TYPE = "error.type"


_tracer = trace.get_tracer("manifest_llm")


def capture_content(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(CAPTURE_CONTENT, "").strip().lower() == "true"


@dataclass
class Call:
    """The span of one model call, for the gateway to fill in as the answer arrives."""

    span: Span
    operation: str
    provider: str
    model: str

    @property
    def dimensions(self) -> dict[str, str]:
        """What the conventions' histograms are broken down by."""
        return {
            gen_ai.GEN_AI_OPERATION_NAME: self.operation,
            gen_ai.GEN_AI_PROVIDER_NAME: self.provider,
            gen_ai.GEN_AI_REQUEST_MODEL: self.model,
        }

    def answered(
        self,
        *,
        input_tokens: int,
        output_tokens: int | None = None,
        finish_reason: str | None = None,
        response_model: str | None = None,
    ) -> None:
        span = self.span
        span.set_attribute(gen_ai.GEN_AI_RESPONSE_MODEL, response_model or self.model)
        span.set_attribute(gen_ai.GEN_AI_USAGE_INPUT_TOKENS, input_tokens)
        metrics.genai_token_usage.record(
            input_tokens, {**self.dimensions, gen_ai.GEN_AI_TOKEN_TYPE: "input"}
        )
        if output_tokens is not None:
            span.set_attribute(gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS, output_tokens)
            usage = {**self.dimensions, gen_ai.GEN_AI_TOKEN_TYPE: "output"}
            metrics.genai_token_usage.record(output_tokens, usage)
        if finish_reason is not None:
            span.set_attribute(gen_ai.GEN_AI_RESPONSE_FINISH_REASONS, [finish_reason])

    def content(
        self, *, system: str | None, prompt: str | Sequence[str], output: Any = None
    ) -> None:
        """Prompt and answer, recorded only when capture is turned on."""
        if not capture_content():
            return
        prompts = [prompt] if isinstance(prompt, str) else list(prompt)
        if system:
            instructions = [{"type": "text", "content": system}]
            self.span.set_attribute(gen_ai.GEN_AI_SYSTEM_INSTRUCTIONS, json.dumps(instructions))
        messages = [{"role": "user", "parts": [{"type": "text", "content": p}]} for p in prompts]
        self.span.set_attribute(gen_ai.GEN_AI_INPUT_MESSAGES, json.dumps(messages))
        if output is not None:
            answer = output if isinstance(output, str) else json.dumps(output, default=str)
            reply = [{"role": "assistant", "parts": [{"type": "text", "content": answer}]}]
            self.span.set_attribute(gen_ai.GEN_AI_OUTPUT_MESSAGES, json.dumps(reply))


@contextmanager
def call(
    operation: str,
    provider: str,
    model: str,
    *,
    max_tokens: int | None = None,
    output_type: str | None = None,
) -> Iterator[Call]:
    """A span around one model call (``operation``: ``chat`` or ``embeddings``)."""
    attributes: dict[str, Any] = {
        gen_ai.GEN_AI_OPERATION_NAME: operation,
        gen_ai.GEN_AI_PROVIDER_NAME: provider,
        gen_ai.GEN_AI_REQUEST_MODEL: model,
    }
    if max_tokens is not None:
        attributes[gen_ai.GEN_AI_REQUEST_MAX_TOKENS] = max_tokens
    if output_type is not None:
        attributes[gen_ai.GEN_AI_OUTPUT_TYPE] = output_type
    started = time.monotonic()
    with _tracer.start_as_current_span(
        f"{operation} {model}", kind=SpanKind.CLIENT, attributes=attributes
    ) as span:
        current = Call(span, operation, provider, model)
        dimensions = current.dimensions
        try:
            yield current
        except BaseException as exc:
            dimensions[ERROR_TYPE] = type(exc).__qualname__
            span.set_attribute(ERROR_TYPE, dimensions[ERROR_TYPE])
            raise
        finally:
            metrics.genai_duration.record(time.monotonic() - started, dimensions)
