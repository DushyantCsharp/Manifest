from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

if TYPE_CHECKING:
    from ..models.completed_part import CompletedPart


T = TypeVar("T", bound="DocumentFinalize")


@_attrs_define
class DocumentFinalize:
    """
    Attributes:
        parts (list[CompletedPart]): Every uploaded part, with the ETag its PUT returned.
    """

    parts: list[CompletedPart]

    def to_dict(self) -> dict[str, Any]:
        parts = []
        for parts_item_data in self.parts:
            parts_item = parts_item_data.to_dict()
            parts.append(parts_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "parts": parts,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.completed_part import CompletedPart  # noqa: PLC0415

        d = dict(src_dict)
        parts = []
        _parts = d.pop("parts")
        for parts_item_data in _parts:
            parts_item = CompletedPart.from_dict(parts_item_data)

            parts.append(parts_item)

        document_finalize = cls(
            parts=parts,
        )

        return document_finalize
