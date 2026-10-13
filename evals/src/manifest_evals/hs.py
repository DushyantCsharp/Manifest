"""HS classification evals: 120 labelled goods descriptions through retrieval and the walk.

    python -m manifest_evals.hs seed     # offline: BM25 retrieval, answers from the labels

Retrieval results are recorded per query in ``evals/cassettes/hs-retrieval`` and the walk's
model answers in ``evals/cassettes/hs``, so replays need neither a database nor a key.
``record`` (from ``make eval-record``) replaces both with a live index and a real model.

Offline seeding is plumbing, not a measurement. Retrieval is ``offline-lexical`` BM25 over the
nomenclature (it never sees the label). The stand-in model knows each label: where the right
code is among a step's options it picks it with a fixed confidence (0.95, 0.9, 0.85 by
level); where retrieval missed it, it makes the best-ranked guess at 0.3. Its accuracy is
therefore bounded by retrieval recall, and its confidences describe the stand-in, not a model.
"""

import argparse
import json
import re
import shutil
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from manifest_evals.golden import CASSETTES, LABELS, PROVIDER, splits
from manifest_evals.oracle import NAME as ORACLE
from manifest_hs.offline import Bm25Retriever
from manifest_hs.recorded import Mode as RetrievalMode
from manifest_hs.recorded import RecordedRetriever
from manifest_hs.retrieval import Retriever
from manifest_hs.walk import DIGITS, LEVELS, MODEL, NONE, HsWalker, Walk
from manifest_llm.anthropic_provider import AnthropicProvider
from manifest_llm.cassette import CassetteProvider
from manifest_llm.cassette import Mode as CassetteMode
from manifest_llm.gateway import JsonRequest, JsonResponse, LlmGateway, Provider, Unlimited
from manifest_llm.ratelimit import Limits

HS_LABELS = LABELS / "hs.jsonl"
RIGHT = {"chapter": "0.95", "heading": "0.90", "subheading": "0.85"}
GUESS = "0.30"


@dataclass(frozen=True)
class Label:
    id: str
    description: str
    origin: str | None
    hs6: str


def labels() -> list[Label]:
    rows = HS_LABELS.read_text(encoding="utf-8").splitlines()
    return [Label(**json.loads(row)) for row in rows if row]


class HsOracle:
    """Answers walk steps from the labels (see the module docstring)."""

    name = ORACLE

    def __init__(self, known: Sequence[Label]) -> None:
        self._by_description = {label.description: label for label in known}

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        goods = re.search(r"^Goods: (.+)$", request.prompt, re.MULTILINE)
        asked = re.search(r"^Choose the (\w+) that", request.prompt, re.MULTILINE)
        if goods is None or asked is None or asked.group(1) not in RIGHT:
            raise ValueError("not an HS walk request")
        label = self._by_description[goods.group(1)]
        level = next(level for level in LEVELS if level == asked.group(1))
        options = [o for o in request.schema["properties"]["choice"]["enum"] if o != NONE]
        right = label.hs6[: DIGITS[level]]
        if right in options:
            data = {
                "choice": right,
                "cited": [right],
                "confidence": RIGHT[level],
                "reason": "label",
            }
        else:
            reason = "best guess: the labelled code was not retrieved"
            data = {
                "choice": options[0],
                "cited": [options[0]],
                "confidence": GUESS,
                "reason": reason,
            }
        encoded = json.dumps(data)
        return JsonResponse(data, len(request.system + request.prompt) // 4, len(encoded) // 4, 0)


def walker(provider: Provider, retriever: Retriever) -> HsWalker:
    """The pipeline's walk, without the shared rate limit (offline tooling only)."""
    limits = Limits(requests_per_minute=1_000, tokens_per_minute=10_000_000)
    gateway = LlmGateway({provider.name: provider}, Unlimited(), default_limits=limits)
    return HsWalker(gateway, provider.name, retriever, model=MODEL)


def cassettes(
    mode: CassetteMode = "replay", inner: Provider | None = None, root: Path = CASSETTES
) -> CassetteProvider:
    recorded_with = inner.name if inner is not None else None
    return CassetteProvider(
        root / "hs", mode=mode, inner=inner, name=PROVIDER, recorded_with=recorded_with
    )


def retrievals(
    mode: RetrievalMode = "replay", inner: Retriever | None = None, root: Path = CASSETTES
) -> RecordedRetriever:
    return RecordedRetriever(root / "hs-retrieval", mode=mode, inner=inner)


@dataclass
class Outcome:
    label: Label
    split: str
    walk: Walk

    @property
    def recalled(self) -> bool:
        return any(c.code == self.label.hs6 for c in self.walk.candidates)

    def correct(self, digits: int = 6) -> bool:
        code = self.walk.code
        return code is not None and code[:digits] == self.label.hs6[:digits]


@dataclass
class HsRun:
    outcomes: list[Outcome]
    sources: Counter[str] = field(default_factory=Counter)
    retrieval: Counter[str] = field(default_factory=Counter)


def evaluate(
    provider: CassetteProvider | None = None, retriever: RecordedRetriever | None = None
) -> HsRun:
    """Replay (by default) every labelled description through retrieval and the walk."""
    provider = provider or cassettes()
    retriever = retriever or retrievals()
    split_of = {i: s for s, ids in splits()["hs"].items() for i in ids}
    hs = walker(provider, retriever)
    outcomes = [
        Outcome(label, split_of[label.id], hs.classify(label.description, label.origin))
        for label in labels()
    ]
    return HsRun(outcomes, Counter(provider.sources), Counter(retriever.sources))


def seed(root: Path = CASSETTES) -> HsRun:
    """Record retrieval (offline BM25) and walk answers (from the labels) for every label."""
    for folder in (root / "hs", root / "hs-retrieval"):
        shutil.rmtree(folder, ignore_errors=True)
    provider = cassettes("record", HsOracle(labels()), root)
    return evaluate(provider, retrievals("record", Bm25Retriever(), root))


def record(retriever: Retriever, root: Path = CASSETTES) -> HsRun:
    """Record a real model's walks over a live retriever (``PostgresRetriever``)."""
    for folder in (root / "hs", root / "hs-retrieval"):
        shutil.rmtree(folder, ignore_errors=True)
    provider = cassettes("record", AnthropicProvider(), root)
    return evaluate(provider, retrievals("record", retriever, root))


def summary(run: HsRun) -> dict[str, Any]:
    found = {f"accuracy_{n}": sum(o.correct(n) for o in run.outcomes) for n in (2, 4, 6)}
    recalled = sum(o.recalled for o in run.outcomes)
    return {"descriptions": len(run.outcomes), "recall_at_20": recalled, **found}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m manifest_evals.hs", description=__doc__)
    parser.add_argument("action", choices=["seed"])
    parser.parse_args(argv)
    print(json.dumps(summary(seed()), indent=2))
    replayed = evaluate()
    print(f"replayed {len(replayed.outcomes)} walks; answers from {dict(replayed.sources)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
