"""Opaque keyset cursors, shared by REST (``cursor``) and GraphQL connections (``after``).

A cursor is the sort key of the last item seen, base64url-encoded JSON tagged with its listing,
so a shipments cursor can't be replayed against the review queue. Clients must treat it as
opaque; its encoding may change.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Self
from uuid import UUID

from manifest_api.errors import InvalidCursorError


def _encode(listing: str, *values: str | int) -> str:
    raw = json.dumps([listing, *values], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(listing: str, token: str, arity: int) -> list[object]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        data = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise InvalidCursorError("cursor is not valid") from exc
    if not isinstance(data, list) or len(data) != arity + 1 or data[0] != listing:
        raise InvalidCursorError("cursor does not belong to this listing")
    return data[1:]


def _aware(value: object) -> datetime:
    if not isinstance(value, str):
        raise InvalidCursorError("cursor is not valid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidCursorError("cursor is not valid") from exc
    if parsed.tzinfo is None:
        raise InvalidCursorError("cursor is not valid")
    return parsed


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise InvalidCursorError("cursor is not valid") from exc


@dataclass(frozen=True)
class ShipmentCursor:
    """Shipments are listed newest first: ``ORDER BY created_at DESC, id DESC``."""

    LISTING: ClassVar[str] = "shipments"

    created_at: datetime
    id: UUID

    def encode(self) -> str:
        return _encode(self.LISTING, self.created_at.isoformat(), str(self.id))

    @classmethod
    def decode(cls, token: str) -> Self:
        created_at, id_ = _decode(cls.LISTING, token, 2)
        return cls(_aware(created_at), _uuid(id_))


@dataclass(frozen=True)
class ReviewTaskCursor:
    """The review queue is ``ORDER BY priority, created_at, id`` (most urgent, oldest first)."""

    LISTING: ClassVar[str] = "review_tasks"

    priority: int
    created_at: datetime
    id: UUID

    def encode(self) -> str:
        return _encode(self.LISTING, self.priority, self.created_at.isoformat(), str(self.id))

    @classmethod
    def decode(cls, token: str) -> Self:
        priority, created_at, id_ = _decode(cls.LISTING, token, 3)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise InvalidCursorError("cursor is not valid")
        return cls(priority, _aware(created_at), _uuid(id_))
