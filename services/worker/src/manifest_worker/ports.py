"""The work each stage delegates: page extraction and HS classification.

Production adapters arrive with the extraction and HS prompts (schemas, prompts, retrieval);
until then extraction is unwired (pages fail as not configured and park for redrive) and HS
classification is skipped. Tests and evals plug in their own implementations.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from manifest_domain.extraction import PageFragment
from manifest_domain.lifecycle import DocumentKind
from manifest_worker.errors import NotConfiguredError


@dataclass(frozen=True)
class PageInput:
    document_id: UUID
    page_no: int
    kind: DocumentKind | None
    text: str
    schema_version: int


@dataclass(frozen=True)
class ExtractedPage:
    fragment: PageFragment
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int = 0


class PageExtractor(Protocol):
    def extract(self, page: PageInput) -> ExtractedPage:
        """Raise ``RateLimitedError``, ``TransientProviderError``, ``OutputValidationError``..."""
        ...


@dataclass(frozen=True)
class LineToClassify:
    line_item_id: UUID
    description: str | None
    country_of_origin: str | None


@dataclass(frozen=True)
class HsResult:
    code: str  # 6-digit HS 2022 subheading
    confidence: Decimal  # raw: the task calibrates it with the pinned HS calibrator
    rationale: str
    model: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    walk: dict[str, Any] | None = None  # chapter, heading, subheading steps


class HsClassifier(Protocol):
    def classify(self, line: LineToClassify) -> HsResult: ...


class UnwiredExtractor:
    def extract(self, page: PageInput) -> ExtractedPage:
        raise NotConfiguredError("structured extraction is not wired in yet")
