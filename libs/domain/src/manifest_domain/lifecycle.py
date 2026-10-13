"""Processing states shared by the API (which reports them) and the worker (which sets them).

Values are the wire format in REST and the database, so they never change once released; add
new members instead.
"""

from enum import StrEnum


class DocumentKind(StrEnum):
    COMMERCIAL_INVOICE = "commercial_invoice"
    PACKING_LIST = "packing_list"
    BILL_OF_LADING = "bill_of_lading"
    OTHER = "other"


class ShipmentStatus(StrEnum):
    OPEN = "open"
    """Accepting documents; nothing finalized yet."""
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    """Approved by a reviewer."""
    AUTO_APPROVED = "auto_approved"
    """Every result cleared the confidence thresholds; no human looked at it."""


class DocumentStatus(StrEnum):
    AWAITING_UPLOAD = "awaiting_upload"
    QUEUED = "queued"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    PARTIALLY_EXTRACTED = "partially_extracted"
    """Some pages failed after retries; the rest were extracted and only failed pages retry."""
    FAILED = "failed"


class PageStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    FAILED = "failed"


class TextSource(StrEnum):
    NATIVE = "native"
    """Text layer embedded in the PDF."""
    OCR = "ocr"
