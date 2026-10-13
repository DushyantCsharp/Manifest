"""The eval corpus and its cassettes stay in step with the extractor, with no infrastructure.

If the extraction prompt, schema or page text changes, request hashes change and these fail
first (fast, in ``make test``): reseed with ``make cassettes`` or re-record live.
"""

import json
from pathlib import Path

import pytest

from manifest_evals import golden
from manifest_evals.oracle import NAME, OracleProvider
from manifest_llm.gateway import JsonRequest
from manifest_worker.extraction import SYSTEM_V3, prompt, request_model

PACKS = golden.golden_packs()
TEXT_LAYER = [pack for pack in PACKS if golden.variant(pack) != "scanned"]
SCANNED = [pack for pack in PACKS if golden.variant(pack) == "scanned"]
MISMATCHED = [pack for pack in PACKS if golden.variant(pack) == "mismatch"]
DOCUMENTS = {"commercial_invoice", "packing_list", "bill_of_lading"}


def test_the_corpus_has_labels_and_truth_for_every_document() -> None:
    assert len(PACKS) == 55
    assert (len(TEXT_LAYER), len(SCANNED), len(MISMATCHED)) == (40, 15, 15)
    for pack in PACKS:
        assert set(golden.truths(pack)) == DOCUMENTS
        assert sorted(page.document for page in golden.pages(pack)) == sorted(DOCUMENTS)


@pytest.mark.parametrize("pack", TEXT_LAYER)
def test_text_layer_packs_replay_to_ground_truth_with_provenance(pack: str) -> None:
    provider = golden.cassette(pack)
    documents = golden.extract_pack(pack, provider)
    assert provider.counts == {"replayed": 3}
    for document, truth in golden.truths(pack).items():
        merged = documents[document].merged
        assert golden.differences(truth, golden.values(merged)) == []
        found = [*merged.fields.values(), *(v for line in merged.lines for v in line.values())]
        assert found
        for value in found:
            assert value.page == 1
            assert value.signals is not None
            assert value.signals.evidence == "verified"


def test_scanned_packs_replay_what_ocr_left_readable() -> None:
    right = total = 0
    for pack in SCANNED:
        readings = golden.read_pack(pack, golden.cassette(pack))
        for document, truth in golden.truths(pack).items():
            got, expected = golden.accuracy(truth, readings[document])
            right, total = right + got, total + expected
    assert 0.8 < right / total < 1  # OCR loses some values; a copier can't recover them


@pytest.mark.parametrize("pack", [TEXT_LAYER[0], SCANNED[0], MISMATCHED[0]])
def test_seeding_reproduces_the_committed_requests(pack: str, tmp_path: Path) -> None:
    golden.seed(pack, root=tmp_path)
    committed = {p.name: json.loads(p.read_text()) for p in (golden.CASSETTES / pack).glob("*")}
    seeded = {p.name: json.loads(p.read_text()) for p in (tmp_path / pack).glob("*")}
    assert set(seeded) == set(committed)  # same request hashes
    for name, recording in committed.items():
        assert recording["request"] == seeded[name]["request"]
        if recording["recorded_with"].startswith(NAME):  # not yet re-recorded from a model
            assert recording["response"] == seeded[name]["response"]
            assert recording["recorded_with"] == seeded[name]["recorded_with"]


def test_a_mismatch_is_reported_field_by_field() -> None:
    pack = TEXT_LAYER[0]
    truth = golden.truths(pack)["commercial_invoice"]
    perfect = golden.expected(truth)
    lines = [dict(line) for line in perfect.lines]
    lines[0]["quantity"] = str(int(lines[0]["quantity"] or 0) + 1)
    actual = perfect.fields["currency"]
    other = "USD" if actual != "USD" else "EUR"
    reading = golden.Reading({**perfect.fields, "currency": other}, lines)
    problems = golden.differences(truth, reading)
    assert f"commercial_invoice.currency: expected {actual!r}, got {other!r}" in problems
    assert any(problem.startswith("commercial_invoice.line1.quantity") for problem in problems)
    assert golden.accuracy(truth, reading)[0] < golden.accuracy(truth, perfect)[0]


def test_descriptions_compare_ignoring_case_and_spacing() -> None:
    assert golden.same("text", "Smartphone 6.1in", "SMARTPHONE  6.1IN")
    assert not golden.same("text", "Smartphone 6.1in", "Smartphone 6.2in")
    assert golden.same("decimal", "40", "40.00")
    assert not golden.same("currency", "EUR", "eur")


def _request(pack: str, document: str) -> tuple[JsonRequest, str]:
    [page] = [p for p in golden.pages(pack) if p.document == document]
    kind = page.page.kind
    assert kind is not None
    schema = request_model(kind).model_json_schema()
    return JsonRequest("m", SYSTEM_V3, prompt(page.page, kind), schema, 100), page.page.text


def test_the_oracle_never_answers_what_the_page_does_not_print() -> None:
    pack = TEXT_LAYER[0]
    truths = golden.truths(pack)
    oracle = OracleProvider(truths, golden.layout(pack))
    request, _ = _request(pack, "commercial_invoice")
    number = truths["commercial_invoice"]["fields"]["invoice_number"]
    blanked = request.prompt.replace(number, "XXXX")
    answer = oracle.complete_json(JsonRequest("m", SYSTEM_V3, blanked, request.schema, 100))
    assert answer.data["invoice_number"] == {"value": "", "evidence": "", "confidence": "0.97"}
    with pytest.raises(ValueError, match="not a page-extraction request"):
        oracle.complete_json(JsonRequest("m", SYSTEM_V3, "hello", request.schema, 100))


def test_on_ocr_pages_the_oracle_copies_what_ocr_produced() -> None:
    pack = SCANNED[0]
    oracle = OracleProvider(golden.truths(pack), golden.layout(pack), ocr=True)
    request, text = _request(pack, "commercial_invoice")
    answer = oracle.complete_json(request).data
    readings = [answer[name] for name in answer if name != "line_items"]
    readings += [reading for line in answer["line_items"] for reading in line.values()]
    copied = [reading for reading in readings if reading["value"]]
    page = " ".join(text.split())
    assert copied
    assert all(" ".join(reading["evidence"].split()) in page for reading in copied)
