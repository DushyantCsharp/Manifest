"""How pipeline failures are sorted.

- Transient (``TRANSIENT``): retried with jittered exponential backoff, at most ``max_retries``
  times; still failing after that, the message is parked in its stage's DLQ for redrive.
- Content problems (``CONTENT_ERRORS``): the document or the model's answer is bad. Never
  retried; the page fails and the shipment goes to human review.
- Configuration problems (``REJECTED_ERRORS``): retrying won't help until someone fixes the
  setup, so the message is parked for redrive and the page fails.
"""

from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from manifest_llm.errors import (
    OutputValidationError,
    PermanentProviderError,
    TransientProviderError,
)


class TransientError(Exception):
    """Worth retrying: a dependency timed out or was briefly unavailable."""


class TransientStorageError(TransientError):
    """Object storage timed out or was unavailable."""


class ContentError(Exception):
    """The document itself can't be processed (e.g. not a readable PDF)."""


class NotConfiguredError(Exception):
    """A stage has no adapter wired in yet."""


class PoisonMessageError(Exception):
    """The message has repeatedly killed the worker processing it."""


TRANSIENT = (TransientProviderError, TransientError, OperationalError)
CONTENT_ERRORS = (OutputValidationError, ValidationError, ContentError)
REJECTED_ERRORS = (PermanentProviderError, NotConfiguredError)
