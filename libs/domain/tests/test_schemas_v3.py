"""Schema v3: what the model fills per document type stays inside the structured-output limits,
and v3 fragments must carry provenance."""

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from manifest_domain.extraction import LINE_FIELDS, ExtractedValue, PageFragment, Signals
from manifest_domain.lifecycle import DocumentKind
from manifest_domain.schemas import v3

# Per request, across the whole schema (platform.claude.com structured-outputs limits).
MAX_OPTIONAL, MAX_UNIONS = 24, 16


def _objects(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [o for item in node for o in _objects(item)]
    if not isinstance(node, dict):
        return []
    here = [node] if "properties" in node else []
    return here + [o for value in node.values() for o in _objects(value)]


@pytest.mark.parametrize("kind", sorted(v3.DOCUMENTS))
def test_schema_fits_structured_outputs(kind: DocumentKind) -> None:
    schema = v3.DOCUMENTS[kind].model_json_schema()
    text = json.dumps(schema)
    for banned in ('"anyOf"', '"minimum"', '"maximum"', '"minLength"', '"maxLength"', '"null"'):
        assert banned not in text, banned
    objects = _objects(schema)
    optional = sum(len(set(o["properties"]) - set(o.get("required", []))) for o in objects)
    assert optional == 0 < MAX_OPTIONAL
    assert all(o.get("additionalProperties") is False for o in objects)
    assert text.count('"type": [') == 0 < MAX_UNIONS


def test_every_field_is_a_reading_with_a_kind() -> None:
    for kind, model in v3.DOCUMENTS.items():
        kinds = v3.kinds(model)
        assert set(kinds) == set(model.model_fields) - {"line_items"}, kind
        assert v3.header_kinds(kind) == kinds
    assert v3.header_kinds(DocumentKind.OTHER) == {}
    assert v3.line_kinds(DocumentKind.BILL_OF_LADING) == {}


def test_line_columns_are_schema_line_fields() -> None:
    for kind in v3.LINES:
        assert set(v3.line_kinds(kind)) <= set(LINE_FIELDS[v3.VERSION])


def test_readings_reject_impossible_confidence() -> None:
    with pytest.raises(ValidationError):
        v3.Reading(value="x", evidence="x", confidence=Decimal("1.01"))
    with pytest.raises(ValidationError):
        v3.Reading.model_validate({"value": "x", "evidence": "x", "confidence": 0.5, "page": 2})


SIGNALS = Signals(self_report=Decimal("0.9"), evidence="verified", normaliser="exact")


def test_v3_fragments_must_carry_provenance() -> None:
    proven = ExtractedValue(
        value="INV-1", confidence=Decimal("0.9"), page=1, evidence="No INV-1", signals=SIGNALS
    )
    PageFragment(schema_version=3, fields={"invoice_number": proven})
    unproven = ExtractedValue(value="INV-1", confidence=Decimal("0.9"))
    with pytest.raises(ValidationError, match="no page or signals"):
        PageFragment(schema_version=3, fields={"invoice_number": unproven})
    quoteless = proven.model_copy(update={"evidence": None})
    with pytest.raises(ValidationError, match="no evidence"):
        PageFragment(schema_version=3, line_items=[{"description": quoteless}])
    PageFragment(schema_version=2, fields={"invoice_number": unproven})  # v2 never had any
    unreadable = proven.model_copy(update={"value": None, "evidence": None})
    PageFragment(schema_version=3, fields={"total_amount": unreadable})  # kept as missing
