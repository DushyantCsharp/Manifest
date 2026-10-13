from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.document_status import DocumentStatus

T = TypeVar("T", bound="FinalizeAccepted")


@_attrs_define
class FinalizeAccepted:
    """
    Attributes:
        job_id (UUID): Pipeline run started for this document.
        document_id (UUID):
        status (DocumentStatus):
    """

    job_id: UUID
    document_id: UUID
    status: DocumentStatus
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        job_id = str(self.job_id)

        document_id = str(self.document_id)

        status = self.status.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "job_id": job_id,
                "document_id": document_id,
                "status": status,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        job_id = UUID(d.pop("job_id"))

        document_id = UUID(d.pop("document_id"))

        status = DocumentStatus(d.pop("status"))

        finalize_accepted = cls(
            job_id=job_id,
            document_id=document_id,
            status=status,
        )

        finalize_accepted.additional_properties = d
        return finalize_accepted

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
