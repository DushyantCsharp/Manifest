"""What can go wrong calling a model, sorted by what the caller should do about it."""


class LlmError(Exception):
    """Base for every error the gateway raises."""


class TransientProviderError(LlmError):
    """Try again later: provider rate limit (429), overload, 5xx, timeout, connection failure."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class PermanentProviderError(LlmError):
    """Retrying won't help until something changes: bad request, credentials, unknown model."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OutputValidationError(LlmError):
    """The model answered, but not in the requested structure. Never retried: goes to review."""


class RateLimitedError(LlmError):
    """Our shared budget for this provider and model is spent for now; wait ``retry_after`` s."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited; retry in {retry_after:.1f}s")
        self.retry_after = retry_after
