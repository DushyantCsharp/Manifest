"""The LLM page extractor: what it asks for, and how it normalises what the model copied."""

import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from manifest_domain.extraction import Signals
from manifest_domain.lifecycle import DocumentKind
from manifest_domain.normalisers.numbers import NormalisationError
from manifest_domain.schemas import v3
from manifest_llm.errors import OutputValidationError
from manifest_llm.gateway import JsonRequest, JsonResponse, LlmGateway, Unlimited
from manifest_llm.ratelimit import Limits
from manifest_worker.extraction import (
    AMBIGUITY_PENALTY,
    SYSTEM,
    SYSTEM_V3,
    LlmPageExtractor,
    PageContext,
    fragment_from,
    fragment_v3,
    normalise,
    output_model,
    request_model,
)
from manifest_worker.ports import PageInput

INVOICE = DocumentKind.COMMERCIAL_INVOICE
EN, DE, UNKNOWN = PageContext("."), PageContext(","), PageContext(None)


def reading(text: str | None, confidence: str = "0.9") -> dict[str, Any]:
    return {"text": text, "confidence": Decimal(confidence)}


@pytest.mark.parametrize(
    ("kind", "text", "context", "expected"),
    [
        ("text", "  Hnos   Valverde S.L. ", EN, ("Hnos Valverde S.L.", Decimal(1))),
        ("decimal", "1.234,56", DE, ("1234.56", Decimal(1))),
        ("decimal", "1,234", EN, ("1234", Decimal(1))),
        ("decimal", "1,234", DE, ("1.234", Decimal(1))),
        ("decimal", "1,234", UNKNOWN, ("1234", AMBIGUITY_PENALTY)),  # read as grouping
        ("integer", "1.425", DE, ("1425", Decimal(1))),
        ("weight", "2,388.9 lb", EN, ("1083.59", Decimal(1))),
        ("weight", "1,234 kg", UNKNOWN, ("1234.00", AMBIGUITY_PENALTY)),
        ("date", "07.04.2026", DE, ("2026-04-07", Decimal(1))),
        ("date", "04/07/2026", EN, ("2026-04-07", AMBIGUITY_PENALTY)),  # month first with "."
        ("date", "04/07/2026", DE, ("2026-07-04", AMBIGUITY_PENALTY)),  # day first with ","
        ("currency", "pounds sterling (GBP)", EN, ("GBP", Decimal(1))),
        ("currency", "$", PageContext(".", dollar="CAD"), ("CAD", Decimal(1))),
        ("country", " cn ", EN, ("CN", Decimal(1))),
        ("incoterm", "FCA Manta (Incoterms 2020)", EN, ("FCA", Decimal(1))),
    ],
)
def test_normalise(
    kind: Any, text: str, context: PageContext, expected: tuple[str, Decimal]
) -> None:
    assert normalise(kind, text, context) == expected


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("text", "   "),
        ("decimal", "lots"),
        ("integer", "12,5"),
        ("date", "next week"),
        ("currency", "$"),  # no page currency to say which dollar
        ("country", "China"),
        ("incoterm", "ASAP"),
    ],
)
def test_values_that_do_not_normalise(kind: Any, text: str) -> None:
    with pytest.raises(NormalisationError):
        normalise(kind, text, DE)


def test_a_page_is_normalised_in_its_own_conventions() -> None:
    raw = {
        "fields": {
            "invoice_date": reading("04.07.2026"),
            "currency": reading("Euro"),
            "incoterm": reading("CIP Hamburg", "0.95"),
            "total_amount": reading("205.523,60", "0.99"),
            "payment_terms": reading("next Tuesday-ish"),
            "invoice_number": None,
        },
        "line_items": [
            {
                "description": reading("Rubber flip-flops"),
                "quantity": reading("3.834"),  # grouping, given the page's decimal comma
                "unit_price": reading("1,41"),
                "currency": reading("$"),  # a bare dollar on a EUR invoice: resolved by header
                "country_of_origin": reading("China"),  # not a code: the model broke a rule
            },
            {"description": None, "quantity": reading(None)},  # an empty row
        ],
    }
    fragment = fragment_from(raw, INVOICE, 2)
    values = {name: value.value for name, value in fragment.fields.items()}
    assert values == {
        "invoice_date": "2026-07-04",
        "currency": "EUR",
        "incoterm": "CIP",
        "incoterm_place": "Hamburg",
        "total_amount": "205523.60",
        "payment_terms": "next Tuesday-ish",
    }
    assert fragment.fields["incoterm_place"].confidence == Decimal("0.95")
    [line] = fragment.line_items
    assert line["quantity"].value == "3834"
    assert line["unit_price"].value == "1.41"
    assert line["currency"].value == "EUR"
    assert (line["country_of_origin"].value, line["country_of_origin"].confidence) == (None, 0)


def test_schema_versions_ask_for_their_own_line_fields() -> None:
    def line_fields(kind: DocumentKind, version: int) -> set[str]:
        schema = output_model(kind, version).model_json_schema()
        ref = schema["properties"]["line_items"]["items"]["$ref"].rsplit("/", 1)[1]
        return set(schema["$defs"][ref]["properties"])

    assert "country_of_origin" not in line_fields(INVOICE, 1)
    assert "country_of_origin" in line_fields(INVOICE, 2)
    assert {"net_weight_kg", "gross_weight_kg"} <= line_fields(DocumentKind.PACKING_LIST, 2)
    assert line_fields(DocumentKind.BILL_OF_LADING, 2) == set()
    assert output_model(INVOICE, 2) is output_model(INVOICE, 2)  # built once


class Model:
    name = "fake"

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.requests: list[JsonRequest] = []

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        self.requests.append(request)
        return JsonResponse(self.answer, 900, 120)


def extractor(model: Model) -> LlmPageExtractor:
    limits = Limits(requests_per_minute=60, tokens_per_minute=100_000)
    gateway = LlmGateway({"fake": model}, Unlimited(), default_limits=limits)
    return LlmPageExtractor(gateway, "fake", "fake-1")


def page(kind: DocumentKind | None, text: str = "INVOICE RE-1") -> PageInput:
    return PageInput(uuid4(), 1, kind, text, 2)


def answer(**fields: Any) -> dict[str, Any]:
    return {"fields": fields, "line_items": []}


def test_extract_through_the_gateway() -> None:
    model = Model(answer(invoice_number={"text": "RE-1", "confidence": "0.9"}))
    extracted = extractor(model).extract(page(None))  # unclassified: read as an invoice
    assert extracted.fragment.fields["invoice_number"].value == "RE-1"
    assert (extracted.model, extracted.tokens_in, extracted.tokens_out) == ("fake/fake-1", 900, 120)
    [request] = model.requests
    assert request.system == SYSTEM
    assert request.prompt.startswith("Document: commercial invoice\nPage 1\n")
    assert "INVOICE RE-1" in request.prompt


@pytest.mark.parametrize(
    "bad",
    [
        answer(invoice_number={"text": "RE-1", "confidence": "2"}),  # not a probability
        answer(made_up={"text": "x", "confidence": "0.5"}),  # not a field we asked for
        {"fields": {}},  # no line_items
    ],
)
def test_answers_outside_the_schema_fail_validation(bad: dict[str, Any]) -> None:
    with pytest.raises(OutputValidationError):
        extractor(Model(bad)).extract(page(INVOICE))


def test_documents_we_do_not_model_are_not_sent() -> None:
    model = Model({})
    extracted = extractor(model).extract(page(DocumentKind.OTHER))
    assert model.requests == []
    assert extracted.fragment.fields == {}
    assert extracted.fragment.line_items == []


# ---------------------------------------------------------------- schema v3

PAGE_TEXT = (
    "COMMERCIAL INVOICE\nInvoice No: RE-26-3487\nDate: 07.04.2026\nIncoterms: CIP Hamburg\n"
    "Currency: EUR\n1 Laptop computer 14in 1.234 pcs 1.234,50 1.523.373,00 China\n"
    "Total EUR 205.523,60"
)


def r(value: str, evidence: str | None = None, confidence: str = "0.9") -> dict[str, Any]:
    return {
        "value": value,
        "evidence": value if evidence is None else evidence,
        "confidence": Decimal(confidence),
    }


def blank() -> dict[str, Any]:
    return r("", "")


def v3_answer(**fields: dict[str, Any]) -> dict[str, Any]:
    names = v3.header_kinds(INVOICE)
    return {name: fields.get(name, blank()) for name in names} | {
        "line_items": fields.get("line_items", [])  # type: ignore[dict-item]
    }


def v3_page(text: str = PAGE_TEXT, page_no: int = 2) -> PageInput:
    return PageInput(uuid4(), page_no, INVOICE, text, 3)


def test_v3_request_is_the_domain_schema_with_this_prompts_descriptions() -> None:
    model = request_model(INVOICE)
    assert issubclass(model, v3.CommercialInvoice)
    schema = model.model_json_schema()
    assert schema["properties"]["invoice_number"]["description"] == "Invoice number"
    assert "name or a code" in json.dumps(schema)  # country of origin, as printed
    assert '"anyOf"' not in json.dumps(schema)
    assert set(schema["required"]) == set(v3.CommercialInvoice.model_fields)
    assert request_model(INVOICE) is model  # built once
    assert "line_items" not in request_model(DocumentKind.BILL_OF_LADING).model_fields


def test_v3_values_carry_page_evidence_and_signals() -> None:
    answer = v3_answer(
        invoice_number=r("RE-26-3487", "Invoice No: RE-26-3487", "0.95"),
        invoice_date=r("07.04.2026", "Date: 07.04.2026"),
        incoterm=r("CIP Hamburg", "Incoterms: CIP Hamburg"),
        currency=r("EUR", "Currency: EUR"),
        total_amount=r("205.523,60", "Total EUR 205.523,60"),
        seller_name=r("Hnos Valverde", "Seller: Hnos Valverde"),  # not on the page
    )
    fragment = fragment_v3(answer, INVOICE, v3_page())
    number = fragment.fields["invoice_number"]
    assert (number.value, number.page, number.evidence) == (
        "RE-26-3487",
        2,
        "Invoice No: RE-26-3487",
    )
    assert number.signals == Signals(
        self_report=Decimal("0.95"), evidence="verified", normaliser="exact"
    )
    assert number.confidence == Decimal("0.95")
    assert fragment.fields["total_amount"].value == "205523.60"  # the page writes 1.234,56
    assert fragment.fields["invoice_date"].value == "2026-04-07"
    assert fragment.fields["incoterm_place"].value == "Hamburg"
    seller = fragment.fields["seller_name"]
    assert seller.signals is not None
    assert seller.signals.evidence == "failed"
    assert "payment_terms" not in fragment.fields  # "" means not printed


def test_v3_lines_normalise_country_names_and_flag_what_fails() -> None:
    line = {name: blank() for name in v3.line_kinds(INVOICE)}
    line |= {
        "description": r("Laptop computer 14in"),
        "quantity": r("1.234", "Laptop computer 14in 1.234 pcs"),
        "unit_price": r("1.234,50"),
        "country_of_origin": r("China"),
        "amount": r("lots", "1.523.373,00"),  # doesn't normalise
    }
    fragment = fragment_v3(v3_answer(line_items=[line]), INVOICE, v3_page())  # type: ignore[arg-type]
    [read] = fragment.line_items
    assert read["country_of_origin"].value == "CN"
    assert read["unit_price"].value == "1234.50"
    amount = read["amount"]
    assert (amount.value, amount.confidence) == (None, 0)
    assert amount.signals is not None
    assert amount.signals.normaliser == "failed"


def test_ambiguity_resolved_by_context_costs_confidence() -> None:
    answer = v3_answer(total_amount=r("1,234", "Total 1,234", "1"))
    fragment = fragment_v3(answer, INVOICE, v3_page("Total 1,234"))
    total = fragment.fields["total_amount"]
    assert total.signals is not None
    assert total.signals.normaliser == "ambiguous"
    assert total.confidence == AMBIGUITY_PENALTY


def test_v3_pages_are_asked_with_the_v3_prompt_and_v2_pages_with_v2() -> None:
    model = Model(v3_answer(invoice_number=r("RE-26-3487", "Invoice No: RE-26-3487")))
    extracted = extractor(model).extract(v3_page())
    assert extracted.fragment.schema_version == 3
    assert extracted.fragment.fields["invoice_number"].page == 2
    [request] = model.requests
    assert (request.system, request.effort, request.max_tokens) == (SYSTEM_V3, "low", 16_000)
    v2_model = Model(answer(invoice_number={"text": "RE-1", "confidence": "0.9"}))
    extractor(v2_model).extract(page(INVOICE))
    assert v2_model.requests[0].system == SYSTEM
    assert v2_model.requests[0].effort is None
