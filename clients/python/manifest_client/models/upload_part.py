from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="UploadPart")


@_attrs_define
class UploadPart:
    """
    Attributes:
        part_number (int):
        url (str): Presigned URL. PUT this byte range here and keep the ETag.
        offset_bytes (int):
        size_bytes (int):
    """

    part_number: int
    url: str
    offset_bytes: int
    size_bytes: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        part_number = self.part_number

        url = self.url

        offset_bytes = self.offset_bytes

        size_bytes = self.size_bytes

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "part_number": part_number,
                "url": url,
                "offset_bytes": offset_bytes,
                "size_bytes": size_bytes,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        part_number = d.pop("part_number")

        url = d.pop("url")

        offset_bytes = d.pop("offset_bytes")

        size_bytes = d.pop("size_bytes")

        upload_part = cls(
            part_number=part_number,
            url=url,
            offset_bytes=offset_bytes,
            size_bytes=size_bytes,
        )

        upload_part.additional_properties = d
        return upload_part

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
