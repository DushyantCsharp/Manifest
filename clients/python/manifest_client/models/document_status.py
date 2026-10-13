from enum import StrEnum


class DocumentStatus(StrEnum):
    AWAITING_UPLOAD = "awaiting_upload"
    EXTRACTED = "extracted"
    FAILED = "failed"
    PARTIALLY_EXTRACTED = "partially_extracted"
    PROCESSING = "processing"
    QUEUED = "queued"

    def __str__(self) -> str:
        return str(self.value)
