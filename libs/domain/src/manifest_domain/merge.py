"""One document's extraction from its pages' fragments (shared by the worker and evals)."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from manifest_domain.confidence import settle
from manifest_domain.extraction import ExtractedValue, PageFragment


@dataclass
class Merged:
    fields: dict[str, ExtractedValue] = field(default_factory=dict)
    lines: list[dict[str, ExtractedValue]] = field(default_factory=list)


def merge(fragments: Sequence[PageFragment]) -> Merged:
    """Header fields: settled across every page that read them (agreement corroborates,
    disagreement costs confidence). Line items: in page order.
    """
    merged = Merged()
    readings: dict[str, list[ExtractedValue]] = defaultdict(list)
    for fragment in fragments:
        for name, value in fragment.fields.items():
            readings[name].append(value)
        merged.lines.extend(fragment.line_items)
    merged.fields = {name: settle(values) for name, values in readings.items()}
    return merged
