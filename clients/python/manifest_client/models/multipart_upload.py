from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.upload_part import UploadPart


T = TypeVar("T", bound="MultipartUpload")


@_attrs_define
class MultipartUpload:
    """
    Attributes:
        upload_id (str):
        part_size_bytes (int):
        expires_at (datetime.datetime): The presigned URLs stop working at this time.
        parts (list[UploadPart]):
    """

    upload_id: str
    part_size_bytes: int
    expires_at: datetime.datetime
    parts: list[UploadPart]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        upload_id = self.upload_id

        part_size_bytes = self.part_size_bytes

        expires_at = self.expires_at.isoformat()

        parts = []
        for parts_item_data in self.parts:
            parts_item = parts_item_data.to_dict()
            parts.append(parts_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "upload_id": upload_id,
                "part_size_bytes": part_size_bytes,
                "expires_at": expires_at,
                "parts": parts,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.upload_part import UploadPart  # noqa: PLC0415

        d = dict(src_dict)
        upload_id = d.pop("upload_id")

        part_size_bytes = d.pop("part_size_bytes")

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        parts = []
        _parts = d.pop("parts")
        for parts_item_data in _parts:
            parts_item = UploadPart.from_dict(parts_item_data)

            parts.append(parts_item)

        multipart_upload = cls(
            upload_id=upload_id,
            part_size_bytes=part_size_bytes,
            expires_at=expires_at,
            parts=parts,
        )

        multipart_upload.additional_properties = d
        return multipart_upload

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
