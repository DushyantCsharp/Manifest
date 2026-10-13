from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.document_kind import DocumentKind
from ..models.document_status import DocumentStatus
from ..models.upload_content_type import UploadContentType

if TYPE_CHECKING:
    from ..models.document_page import DocumentPage


T = TypeVar("T", bound="Document")


@_attrs_define
class Document:
    """
    Attributes:
        id (UUID):
        filename (str):
        content_type (UploadContentType):
        size_bytes (int):
        kind (DocumentKind | None): Null until the document is classified.
        status (DocumentStatus):
        page_count (int | None): Null until the document is split into pages.
        extraction_schema_version (int | None): Schema version of the current extraction; null before extraction.
        pages (list[DocumentPage]):
        created_at (datetime.datetime):
        updated_at (datetime.datetime):
    """

    id: UUID
    filename: str
    content_type: UploadContentType
    size_bytes: int
    kind: DocumentKind | None
    status: DocumentStatus
    page_count: int | None
    extraction_schema_version: int | None
    pages: list[DocumentPage]
    created_at: datetime.datetime
    updated_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        filename = self.filename

        content_type = self.content_type.value

        size_bytes = self.size_bytes

        kind: None | str
        if isinstance(self.kind, DocumentKind):
            kind = self.kind.value
        else:
            kind = self.kind

        status = self.status.value

        page_count: int | None
        page_count = self.page_count

        extraction_schema_version: int | None
        extraction_schema_version = self.extraction_schema_version

        pages = []
        for pages_item_data in self.pages:
            pages_item = pages_item_data.to_dict()
            pages.append(pages_item)

        created_at = self.created_at.isoformat()

        updated_at = self.updated_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "kind": kind,
                "status": status,
                "page_count": page_count,
                "extraction_schema_version": extraction_schema_version,
                "pages": pages,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.document_page import DocumentPage  # noqa: PLC0415

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        filename = d.pop("filename")

        content_type = UploadContentType(d.pop("content_type"))

        size_bytes = d.pop("size_bytes")

        def _parse_kind(data: object) -> DocumentKind | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                kind_type_0 = DocumentKind(data)

                return kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DocumentKind | None, data)

        kind = _parse_kind(d.pop("kind"))

        status = DocumentStatus(d.pop("status"))

        def _parse_page_count(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        page_count = _parse_page_count(d.pop("page_count"))

        def _parse_extraction_schema_version(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        extraction_schema_version = _parse_extraction_schema_version(d.pop("extraction_schema_version"))

        pages = []
        _pages = d.pop("pages")
        for pages_item_data in _pages:
            pages_item = DocumentPage.from_dict(pages_item_data)

            pages.append(pages_item)

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))

        document = cls(
            id=id,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            kind=kind,
            status=status,
            page_count=page_count,
            extraction_schema_version=extraction_schema_version,
            pages=pages,
            created_at=created_at,
            updated_at=updated_at,
        )

        document.additional_properties = d
        return document

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
