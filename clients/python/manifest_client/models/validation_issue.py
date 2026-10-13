from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ValidationIssue")


@_attrs_define
class ValidationIssue:
    """
    Attributes:
        detail (str):
        pointer (None | str | Unset): JSON Pointer (RFC 6901, URI fragment form) into the body.
        parameter (None | str | Unset): Name of the offending query, path or header parameter.
    """

    detail: str
    pointer: None | str | Unset = UNSET
    parameter: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        detail = self.detail

        pointer: None | str | Unset
        if isinstance(self.pointer, Unset):
            pointer = UNSET
        else:
            pointer = self.pointer

        parameter: None | str | Unset
        if isinstance(self.parameter, Unset):
            parameter = UNSET
        else:
            parameter = self.parameter

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "detail": detail,
            }
        )
        if pointer is not UNSET:
            field_dict["pointer"] = pointer
        if parameter is not UNSET:
            field_dict["parameter"] = parameter

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        detail = d.pop("detail")

        def _parse_pointer(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pointer = _parse_pointer(d.pop("pointer", UNSET))

        def _parse_parameter(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parameter = _parse_parameter(d.pop("parameter", UNSET))

        validation_issue = cls(
            detail=detail,
            pointer=pointer,
            parameter=parameter,
        )

        validation_issue.additional_properties = d
        return validation_issue

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
