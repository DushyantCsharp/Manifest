"""Architecture guard: no domain model may carry a float.

Money is Decimal + ISO 4217 code and quantities are Decimal. A float anywhere in a model's
annotations (including inside Optional/list/dict) fails this test.
"""

import importlib
import inspect
import pkgutil
import typing
from collections.abc import Iterator

from pydantic import BaseModel

import manifest_domain


def _domain_models() -> Iterator[type[BaseModel]]:
    for info in pkgutil.walk_packages(manifest_domain.__path__, prefix="manifest_domain."):
        module = importlib.import_module(info.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseModel) and obj.__module__ == module.__name__:
                yield obj


def _contains_float(annotation: object) -> bool:
    if annotation is float:
        return True
    return any(_contains_float(arg) for arg in typing.get_args(annotation))


def float_fields(model: type[BaseModel]) -> list[str]:
    return [name for name, field in model.model_fields.items() if _contains_float(field.annotation)]


def test_detector_catches_nested_floats() -> None:
    class Bad(BaseModel):
        ok: int
        weight: float
        readings: list[float | None]
        lookup: dict[str, typing.Annotated[float, "kg"]]

    assert float_fields(Bad) == ["weight", "readings", "lookup"]


def test_no_domain_model_uses_float() -> None:
    offenders = {
        f"{model.__module__}.{model.__qualname__}": fields
        for model in _domain_models()
        if (fields := float_fields(model))
    }
    assert offenders == {}, f"float fields found (use Decimal): {offenders}"
