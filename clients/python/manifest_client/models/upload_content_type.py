from enum import StrEnum


class UploadContentType(StrEnum):
    APPLICATIONPDF = "application/pdf"
    IMAGEJPEG = "image/jpeg"
    IMAGEPNG = "image/png"
    IMAGETIFF = "image/tiff"

    def __str__(self) -> str:
        return str(self.value)
