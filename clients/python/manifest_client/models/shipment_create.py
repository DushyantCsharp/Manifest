from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="ShipmentCreate")


@_attrs_define
class ShipmentCreate:
    """
    Attributes:
        reference (None | str | Unset): Your own reference (PO or booking number). Not required to be unique.
    """

    reference: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        reference: None | str | Unset
        if isinstance(self.reference, Unset):
            reference = UNSET
        else:
            reference = self.reference

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if reference is not UNSET:
            field_dict["reference"] = reference

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_reference(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reference = _parse_reference(d.pop("reference", UNSET))

        shipment_create = cls(
            reference=reference,
        )

        return shipment_create
