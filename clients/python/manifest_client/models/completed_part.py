from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="CompletedPart")


@_attrs_define
class CompletedPart:
    """
    Attributes:
        part_number (int):
        etag (str):
    """

    part_number: int
    etag: str

    def to_dict(self) -> dict[str, Any]:
        part_number = self.part_number

        etag = self.etag

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "part_number": part_number,
                "etag": etag,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        part_number = d.pop("part_number")

        etag = d.pop("etag")

        completed_part = cls(
            part_number=part_number,
            etag=etag,
        )

        return completed_part
