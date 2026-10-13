from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.page_status import PageStatus
from ..models.text_source import TextSource

T = TypeVar("T", bound="DocumentPage")


@_attrs_define
class DocumentPage:
    """
    Attributes:
        page_number (int):
        status (PageStatus):
        text_source (None | TextSource): Null until the page's text is extracted.
        attempts (int): Processing attempts so far. Failed pages are retried individually.
        error (None | str): Last failure reason while `status` is `failed`.
    """

    page_number: int
    status: PageStatus
    text_source: None | TextSource
    attempts: int
    error: None | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        page_number = self.page_number

        status = self.status.value

        text_source: None | str
        if isinstance(self.text_source, TextSource):
            text_source = self.text_source.value
        else:
            text_source = self.text_source

        attempts = self.attempts

        error: None | str
        error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "page_number": page_number,
                "status": status,
                "text_source": text_source,
                "attempts": attempts,
                "error": error,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        page_number = d.pop("page_number")

        status = PageStatus(d.pop("status"))

        def _parse_text_source(data: object) -> None | TextSource:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                text_source_type_0 = TextSource(data)

                return text_source_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TextSource, data)

        text_source = _parse_text_source(d.pop("text_source"))

        attempts = d.pop("attempts")

        def _parse_error(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        error = _parse_error(d.pop("error"))

        document_page = cls(
            page_number=page_number,
            status=status,
            text_source=text_source,
            attempts=attempts,
            error=error,
        )

        document_page.additional_properties = d
        return document_page

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
