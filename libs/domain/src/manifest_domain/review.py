"""Review-queue vocabulary: why a shipment needs a human, and the state of each reviewed field."""

from enum import IntEnum, StrEnum


class ReviewTaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    """A reviewer has approved or corrected at least one field."""
    APPROVED = "approved"


class ReviewPriority(IntEnum):
    """Lower value = reviewed sooner. The queue is ordered by (priority, created_at, id)."""

    URGENT = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


class ReviewReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    DISCREPANCY = "discrepancy"
    MISSING_FIELD = "missing_field"
    PARTIAL_EXTRACTION = "partial_extraction"
    HS_UNCERTAIN = "hs_uncertain"


class FieldStatus(StrEnum):
    EXTRACTED = "extracted"
    """Produced by the pipeline, not yet reviewed."""
    MISSING = "missing"
    """Required by the schema but not found; flagged rather than failing the document."""
    APPROVED = "approved"
    CORRECTED = "corrected"


class CorrectionAction(StrEnum):
    """A reviewer's verdict on one field. Both are labels: approvals mark correct extractions."""

    APPROVE = "approve"
    CORRECT = "correct"


class DiscrepancyKind(StrEnum):
    VALUE_MISMATCH = "value_mismatch"
    """The same field has different values on different documents."""
    MISSING_FIELD = "missing_field"
    LINE_ITEMS_MISMATCH = "line_items_mismatch"
    """Line items don't pair up across invoice and packing list."""
    TOTALS_MISMATCH = "totals_mismatch"
    """Stated totals disagree with the sum of line items."""


class DiscrepancySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"
