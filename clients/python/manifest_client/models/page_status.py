from enum import StrEnum


class PageStatus(StrEnum):
    EXTRACTED = "extracted"
    FAILED = "failed"
    PENDING = "pending"
    PROCESSING = "processing"

    def __str__(self) -> str:
        return str(self.value)
