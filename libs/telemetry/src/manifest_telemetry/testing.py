"""In-memory telemetry for tests: what this process traced and measured, inspectable.

``capture()`` installs (once per process) global providers that keep every finished span and
every metric point in memory, then returns them. Metric values are cumulative for the life of
the process, so compare a value before and after the code under test, or filter on attributes
the test makes unique.
"""

from collections.abc import Iterator, Mapping
from typing import Any

from opentelemetry.sdk.metrics.export import (
    ExponentialHistogramDataPoint,
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from manifest_telemetry.setup import configure

Point = NumberDataPoint | HistogramDataPoint | ExponentialHistogramDataPoint


class Capture:
    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()

    def spans(self, name: str | None = None) -> list[ReadableSpan]:
        found = self.exporter.get_finished_spans()
        return [span for span in found if name is None or span.name == name]

    def clear_spans(self) -> None:
        self.exporter.clear()

    def points(self, metric: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Point]:
        """Every point of ``metric`` whose attributes include ``attributes``."""
        wanted = dict(attributes or {})
        data = self.reader.get_metrics_data()
        for resource_metrics in data.resource_metrics if data else ():
            for scope in resource_metrics.scope_metrics:
                for found in scope.metrics:
                    if found.name != metric:
                        continue
                    for point in found.data.data_points:
                        attrs = dict(point.attributes or {})
                        if all(attrs.get(k) == v for k, v in wanted.items()):
                            yield point

    def total(self, metric: str, attributes: Mapping[str, Any] | None = None) -> float:
        """A counter's sum, a histogram's sum, or a gauge's latest values, added up."""
        total = 0.0
        for point in self.points(metric, attributes):
            total += point.value if isinstance(point, NumberDataPoint) else point.sum
        return total

    def count(self, metric: str, attributes: Mapping[str, Any] | None = None) -> int:
        """How many values a histogram recorded."""
        return sum(
            point.count
            for point in self.points(metric, attributes)
            if isinstance(point, HistogramDataPoint)
        )


_capture: Capture | None = None


def capture() -> Capture:
    """The process-wide in-memory capture, installed on first use."""
    global _capture
    if _capture is None:
        candidate = Capture()
        processor = SimpleSpanProcessor(candidate.exporter)
        if not configure(
            "manifest-tests", span_processor=processor, metric_reader=candidate.reader, environ={}
        ):
            raise RuntimeError("telemetry is disabled")
        _capture = candidate
    return _capture
