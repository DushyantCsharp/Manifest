from enum import StrEnum


class TextSource(StrEnum):
    NATIVE = "native"
    OCR = "ocr"

    def __str__(self) -> str:
        return str(self.value)
