"""DLQ tooling without a broker: reasons, payload diffs, selection, redrive rewriting."""

from typing import Any

import pytest

from manifest_worker.dlq import DeadLetter, diff, redrive_message, select


class FakeMessage:
    def __init__(self, headers: dict[str, Any], args: list[Any]) -> None:
        self.headers = headers
        self.body = (args, {}, {"chord": {"task": "manifest.reconcile"}, "callbacks": None})
        self.acknowledged = False

    def decode(self) -> Any:
        return self.body


def letter(task_id: str, page: int, **headers: Any) -> DeadLetter:
    base = {"id": task_id, "task": "manifest.page_extract", "retries": 4, "group": "g1"}
    message = FakeMessage(base | headers, ["doc-1", page, 2])
    return DeadLetter("page_extract", message)  # type: ignore[arg-type]


PARKED = {
    "x-manifest-failure": "transient_exhausted",
    "x-manifest-exception": "TransientProviderError",
    "x-manifest-parked-at": "2026-10-12T10:00:00+00:00",
}
POISON = {"x-death": [{"reason": "delivery_limit", "count": 1, "queue": "page_extract"}]}


def test_reason_distinguishes_parked_from_broker_dead_lettered() -> None:
    assert letter("a", 4, **PARKED).reason == "parked: transient_exhausted"
    assert letter("b", 7, **POISON).reason == "broker: delivery_limit x1"


def test_diff_shows_payload_changes_not_per_delivery_noise() -> None:
    first = letter("aaaa", 4, **PARKED)
    same_but_later = letter("bbbb", 4, **(PARKED | {"x-manifest-parked-at": "2026-10-13"}))
    other_page = letter("cccc", 7, **PARKED)
    assert diff(first, same_but_later) == []
    changed = [line for line in diff(first, other_page) if line[:1] in "+-"]
    assert "-    4," in changed
    assert "+    7," in changed


def test_select_by_id_prefix_exception_and_failure() -> None:
    letters = [letter("aaaa1", 4, **PARKED), letter("bbbb2", 7, **POISON)]
    assert select(letters, ids=["bbbb"]) == [letters[1]]
    assert select(letters, exception="TransientProviderError") == [letters[0]]
    assert select(letters, failure="transient_exhausted") == [letters[0]]
    with pytest.raises(SystemExit, match="0 dead letters match"):
        select(letters, ids=["zzz"])


def test_redrive_is_a_fresh_standalone_task() -> None:
    original = letter("aaaa1", 4, **PARKED)
    (args, kwargs, embed), headers = redrive_message(original)
    assert (args, kwargs) == (["doc-1", 4, 2], {})
    assert embed["chord"] is None
    assert headers["id"] != "aaaa1"
    assert (headers["retries"], headers["group"]) == (0, None)
    assert headers["x-manifest-redriven-from"] == "aaaa1"
    assert not any(key.startswith("x-manifest-failure") for key in headers)
