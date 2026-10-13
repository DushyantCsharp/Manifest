"""Recording from real models: retries, resumability, and what scoring accepts on a line."""

import json
from pathlib import Path

import pytest

from manifest_evals import golden
from manifest_evals.live import RetryingProvider, forget_stand_ins, in_parallel
from manifest_evals.oracle import NAME as ORACLE
from manifest_llm.errors import PermanentProviderError, TransientProviderError
from manifest_llm.gateway import JsonRequest, JsonResponse

REQUEST = JsonRequest("m", "s", "p", {}, 10)


class Flaky:
    name = "flaky"

    def __init__(self, failures: int, error: Exception) -> None:
        self.failures, self.error, self.calls = failures, error, 0

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return JsonResponse({"ok": True}, 1, 1)


def test_transient_errors_are_retried_with_backoff() -> None:
    waits: list[float] = []
    flaky = Flaky(2, TransientProviderError("429", retry_after=7))
    assert RetryingProvider(flaky, waits.append).complete_json(REQUEST).data == {"ok": True}
    assert (flaky.calls, waits) == (3, [7, 7])
    unknown = Flaky(1, TransientProviderError("503"))
    RetryingProvider(unknown, waits.append).complete_json(REQUEST)
    assert waits[-1] == 2.0  # no retry-after: exponential


def test_permanent_errors_and_exhausted_retries_propagate() -> None:
    with pytest.raises(PermanentProviderError):
        RetryingProvider(Flaky(1, PermanentProviderError("401")), lambda _: None).complete_json(
            REQUEST
        )
    with pytest.raises(TransientProviderError):
        RetryingProvider(Flaky(99, TransientProviderError("429")), lambda _: None).complete_json(
            REQUEST
        )


def test_only_stand_in_recordings_are_forgotten(tmp_path: Path) -> None:
    for name, source in [("a", ORACLE), ("b", f"{ORACLE} (OCR)"), ("c", "anthropic")]:
        (tmp_path / f"{name}.json").write_text(json.dumps({"recorded_with": source}))
    (tmp_path / "d.json").write_text(json.dumps({"retriever": "offline-lexical"}))
    assert forget_stand_ins(tmp_path) == 2
    assert forget_stand_ins(tmp_path, field="retriever") == 1
    assert sorted(p.stem for p in tmp_path.glob("*.json")) == ["c"]
    assert forget_stand_ins(tmp_path / "missing") == 0


def test_in_parallel_keeps_order() -> None:
    assert in_parallel(range(10), lambda n: n * n, workers=3) == [n * n for n in range(10)]


def test_a_line_may_carry_the_invoices_currency() -> None:
    pack = golden.golden_packs()[0]
    truth = golden.truths(pack)["commercial_invoice"]
    perfect = golden.expected(truth)
    currency = truth["fields"]["currency"]
    with_currency = [{**line, "currency": currency} for line in perfect.lines]
    assert golden.differences(truth, golden.Reading(perfect.fields, with_currency)) == []
    wrong = [{**line, "currency": "JPY"} for line in perfect.lines]
    assert golden.differences(truth, golden.Reading(perfect.fields, wrong))
