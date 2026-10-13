from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.shipment_status import ShipmentStatus

T = TypeVar("T", bound="Shipment")


@_attrs_define
class Shipment:
    """
    Attributes:
        id (UUID):
        reference (None | str):
        status (ShipmentStatus):
        created_at (datetime.datetime):
        updated_at (datetime.datetime):
    """

    id: UUID
    reference: None | str
    status: ShipmentStatus
    created_at: datetime.datetime
    updated_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        reference: None | str
        reference = self.reference

        status = self.status.value

        created_at = self.created_at.isoformat()

        updated_at = self.updated_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "reference": reference,
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        def _parse_reference(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        reference = _parse_reference(d.pop("reference"))

        status = ShipmentStatus(d.pop("status"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))

        shipment = cls(
            id=id,
            reference=reference,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
        )

        shipment.additional_properties = d
        return shipment

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
