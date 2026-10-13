"""Exceptions the API turns into responses.

REST maps each one to a problem type (``manifest_api.problems``); GraphQL mutations map the
port errors to ``userErrors``. Ports and handlers raise these and never build responses.
"""


class NotFoundError(Exception):
    """The addressed resource does not exist."""


class ConflictError(Exception):
    """The request is valid but conflicts with the resource's current state."""


class InvalidValueError(ValueError):
    """A value is well-formed but not acceptable here (e.g. wrong type for the field)."""


class NotWiredError(NotImplementedError):
    """The port has no adapter yet."""


class InvalidCursorError(ValueError):
    """A pagination cursor is malformed or belongs to a different listing."""


class OverloadedError(Exception):
    """Admission control refused new work."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"too much work pending; retry in {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class IdempotencyKeyReusedError(Exception):
    """The Idempotency-Key was already used for a request with a different fingerprint."""


class IdempotencyKeyInFlightError(Exception):
    """The original request for this Idempotency-Key has not finished yet."""
