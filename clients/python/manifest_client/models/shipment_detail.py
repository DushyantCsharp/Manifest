from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.shipment_status import ShipmentStatus

if TYPE_CHECKING:
    from ..models.document import Document


T = TypeVar("T", bound="ShipmentDetail")


@_attrs_define
class ShipmentDetail:
    """
    Attributes:
        id (UUID):
        reference (None | str):
        status (ShipmentStatus):
        created_at (datetime.datetime):
        updated_at (datetime.datetime):
        documents (list[Document]):
    """

    id: UUID
    reference: None | str
    status: ShipmentStatus
    created_at: datetime.datetime
    updated_at: datetime.datetime
    documents: list[Document]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        reference: None | str
        reference = self.reference

        status = self.status.value

        created_at = self.created_at.isoformat()

        updated_at = self.updated_at.isoformat()

        documents = []
        for documents_item_data in self.documents:
            documents_item = documents_item_data.to_dict()
            documents.append(documents_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "reference": reference,
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
                "documents": documents,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.document import Document  # noqa: PLC0415

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

        documents = []
        _documents = d.pop("documents")
        for documents_item_data in _documents:
            documents_item = Document.from_dict(documents_item_data)

            documents.append(documents_item)

        shipment_detail = cls(
            id=id,
            reference=reference,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            documents=documents,
        )

        shipment_detail.additional_properties = d
        return shipment_detail

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
