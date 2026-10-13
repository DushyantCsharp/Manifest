from enum import StrEnum


class DocumentKind(StrEnum):
    BILL_OF_LADING = "bill_of_lading"
    COMMERCIAL_INVOICE = "commercial_invoice"
    OTHER = "other"
    PACKING_LIST = "packing_list"

    def __str__(self) -> str:
        return str(self.value)
