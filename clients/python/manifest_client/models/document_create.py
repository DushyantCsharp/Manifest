from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.upload_content_type import UploadContentType
from ..types import UNSET, Unset

T = TypeVar("T", bound="DocumentCreate")


@_attrs_define
class DocumentCreate:
    """
    Attributes:
        filename (str):
        content_type (UploadContentType):
        size_bytes (int):
        sha256 (None | str | Unset): Hex SHA-256 of the whole file. When given, it is verified on finalize.
    """

    filename: str
    content_type: UploadContentType
    size_bytes: int
    sha256: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        filename = self.filename

        content_type = self.content_type.value

        size_bytes = self.size_bytes

        sha256: None | str | Unset
        if isinstance(self.sha256, Unset):
            sha256 = UNSET
        else:
            sha256 = self.sha256

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
            }
        )
        if sha256 is not UNSET:
            field_dict["sha256"] = sha256

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        filename = d.pop("filename")

        content_type = UploadContentType(d.pop("content_type"))

        size_bytes = d.pop("size_bytes")

        def _parse_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        sha256 = _parse_sha256(d.pop("sha256", UNSET))

        document_create = cls(
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
        )

        return document_create
