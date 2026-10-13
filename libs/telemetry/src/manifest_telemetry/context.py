"""Which pipeline stage the current code is working for (``classify``, ``page_extract``, ...).

Set around each Celery task by the worker; read by ``manifest_llm`` to label token and cost
metrics. Outside a stage (offline tools, the API) it is ``none``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

NO_STAGE = "none"

_stage: ContextVar[str] = ContextVar("manifest_stage", default=NO_STAGE)


def current_stage() -> str:
    return _stage.get()


@contextmanager
def stage(name: str) -> Iterator[None]:
    token = _stage.set(name or NO_STAGE)
    try:
        yield
    finally:
        _stage.reset(token)
