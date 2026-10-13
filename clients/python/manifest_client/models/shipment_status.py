from enum import StrEnum


class ShipmentStatus(StrEnum):
    APPROVED = "approved"
    AUTO_APPROVED = "auto_approved"
    NEEDS_REVIEW = "needs_review"
    OPEN = "open"
    PROCESSING = "processing"

    def __str__(self) -> str:
        return str(self.value)
