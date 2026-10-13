"""REST request and response bodies: the public shapes recorded in contracts/openapi.json."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from manifest_domain.lifecycle import (
    DocumentKind,
    DocumentStatus,
    PageStatus,
    ShipmentStatus,
    TextSource,
)

MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_PARTS = 10_000  # S3 multipart limit


class _RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UploadContentType(StrEnum):
    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"
    TIFF = "image/tiff"


# ---------------------------------------------------------------- shipments


class ShipmentCreate(_RequestBody):
    reference: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Your own reference (PO or booking number). Not required to be unique.",
    )


class Shipment(BaseModel):
    id: UUID
    reference: str | None
    status: ShipmentStatus
    created_at: datetime
    updated_at: datetime


class ShipmentPage(BaseModel):
    items: list[Shipment]
    next_cursor: str | None = Field(
        description="Opaque. Pass as `cursor` to get the next page; null on the last page."
    )


class DocumentPage(BaseModel):
    page_number: int = Field(ge=1)
    status: PageStatus
    text_source: TextSource | None = Field(description="Null until the page's text is extracted.")
    attempts: int = Field(
        ge=0, description="Processing attempts so far. Failed pages are retried individually."
    )
    error: str | None = Field(description="Last failure reason while `status` is `failed`.")


class Document(BaseModel):
    id: UUID
    filename: str
    content_type: UploadContentType
    size_bytes: int
    kind: DocumentKind | None = Field(description="Null until the document is classified.")
    status: DocumentStatus
    page_count: int | None = Field(description="Null until the document is split into pages.")
    extraction_schema_version: int | None = Field(
        description="Schema version of the current extraction; null before extraction."
    )
    pages: list[DocumentPage]
    created_at: datetime
    updated_at: datetime


class ShipmentDetail(Shipment):
    documents: list[Document]


# ---------------------------------------------------------------- uploads


class DocumentCreate(_RequestBody):
    filename: str = Field(min_length=1, max_length=255)
    content_type: UploadContentType
    size_bytes: int = Field(gt=0, le=MAX_DOCUMENT_BYTES)
    sha256: str | None = Field(
        default=None,
        pattern="^[0-9a-f]{64}$",
        description="Hex SHA-256 of the whole file. When given, it is verified on finalize.",
    )


class UploadPart(BaseModel):
    part_number: int = Field(ge=1, le=MAX_UPLOAD_PARTS)
    url: str = Field(description="Presigned URL. PUT this byte range here and keep the ETag.")
    offset_bytes: int = Field(ge=0)
    size_bytes: int = Field(gt=0)


class MultipartUpload(BaseModel):
    upload_id: str
    part_size_bytes: int
    expires_at: datetime = Field(description="The presigned URLs stop working at this time.")
    parts: list[UploadPart]


class DocumentUpload(BaseModel):
    document: Document
    upload: MultipartUpload


class CompletedPart(_RequestBody):
    part_number: int = Field(ge=1, le=MAX_UPLOAD_PARTS)
    etag: str = Field(min_length=1, max_length=128)


class DocumentFinalize(_RequestBody):
    parts: list[CompletedPart] = Field(
        min_length=1,
        max_length=MAX_UPLOAD_PARTS,
        description="Every uploaded part, with the ETag its PUT returned.",
    )

    @field_validator("parts")
    @classmethod
    def _unique_part_numbers(cls, parts: list[CompletedPart]) -> list[CompletedPart]:
        numbers = [part.part_number for part in parts]
        if len(set(numbers)) != len(numbers):
            raise ValueError("part numbers must be unique")
        return sorted(parts, key=lambda part: part.part_number)


class FinalizeAccepted(BaseModel):
    job_id: UUID = Field(description="Pipeline run started for this document.")
    document_id: UUID
    status: DocumentStatus
