"""The eval corpus as the pipeline sees it, its LLM cassettes, and scoring against truth.

    python -m manifest_evals.golden seed     # cassettes answered from ground truth (offline)
    python -m manifest_evals.golden record   # a real model's answers (needs ANTHROPIC_API_KEY)

Pages come from each pack's ``pages.json`` snapshot (the text the pipeline read: native layer,
else OCR) and are classified and extracted by the pipeline's own code, so the request hashes
recorded here are the ones a replaying pipeline looks up. ``evals/cassettes/<pack_id>/`` holds
one JSON recording per page request.
"""

import argparse
import json
import shutil
import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from manifest_domain.alignment import align, line_distance
from manifest_domain.extraction import CURRENT_SCHEMA_VERSION
from manifest_domain.lifecycle import DocumentKind
from manifest_domain.merge import Merged, merge
from manifest_domain.schemas import v3
from manifest_evals.harness import EVALS_ROOT
from manifest_evals.oracle import OracleProvider
from manifest_evals.synth.corpus import PREFIXES
from manifest_evals.synth.layouts import BY_NAME, Layout
from manifest_llm.anthropic_provider import AnthropicProvider
from manifest_llm.cassette import CassetteProvider, Mode
from manifest_llm.gateway import LlmGateway, Provider, Unlimited
from manifest_llm.ratelimit import Limits
from manifest_worker.extraction import MODEL, LlmPageExtractor
from manifest_worker.ports import ExtractedPage, PageInput
from manifest_worker.text import classify_kind

GOLDEN = EVALS_ROOT / "golden"
LABELS = EVALS_ROOT / "labels"
CASSETTES = EVALS_ROOT / "cassettes"
SPLITS = EVALS_ROOT / "splits.json"
PROVIDER = "anthropic"
_PARTIES = {"seller": "seller_name", "buyer": "buyer_name", "notify_party": "notify_party_name"}
_NUMERIC = frozenset({"decimal", "integer", "weight"})


def golden_packs() -> list[str]:
    """The committed eval corpus (``clean-*``, ``scan-*``, ``mism-*``), not ``syn-*`` packs."""
    return sorted(
        path.name for prefix in PREFIXES for path in GOLDEN.glob(f"{prefix}*") if path.is_dir()
    )


def label(pack_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((LABELS / f"{pack_id}.json").read_text(encoding="utf-8"))
    return data


def variant(pack_id: str) -> str:
    """``clean``, ``scanned`` or ``mismatch``."""
    return str(label(pack_id)["variant"])


def splits() -> dict[str, dict[str, list[str]]]:
    data: dict[str, dict[str, list[str]]] = json.loads(SPLITS.read_text(encoding="utf-8"))
    return data


def truths(pack_id: str) -> dict[str, dict[str, Any]]:
    """Per-document ground truth, keyed by kind (``packing_list``)."""
    documents = (
        json.loads(path.read_text(encoding="utf-8"))
        for path in (GOLDEN / pack_id).glob("*.json")
        if path.name != "pages.json"
    )
    return {truth["document"]: truth for truth in documents}


def layout(pack_id: str) -> Layout:
    return BY_NAME[label(pack_id)["layout"]]


@dataclass(frozen=True)
class GoldenPage:
    document: str  # file stem: commercial_invoice, packing_list, bill_of_lading
    page: PageInput


def pages(pack_id: str) -> Iterator[GoldenPage]:
    """Every page of the pack, from its snapshot, classified as the pipeline does."""
    snapshot = json.loads((GOLDEN / pack_id / "pages.json").read_text(encoding="utf-8"))
    for document in sorted(snapshot):
        read = snapshot[document]
        kind = classify_kind(read[0]["text"])
        document_id = uuid5(NAMESPACE_URL, f"manifest:golden/{pack_id}/{document}.pdf")
        for entry in read:
            yield GoldenPage(
                document,
                PageInput(
                    document_id, entry["page_no"], kind, entry["text"], CURRENT_SCHEMA_VERSION
                ),
            )


def cassette(
    pack_id: str,
    *,
    mode: Mode = "replay",
    inner: Provider | None = None,
    root: Path = CASSETTES,
) -> CassetteProvider:
    recorded_with = inner.name if inner is not None else None
    if isinstance(inner, OracleProvider) and variant(pack_id) == "scanned":
        recorded_with = f"{inner.name} (OCR)"
    return CassetteProvider(
        root / pack_id, mode=mode, inner=inner, name=PROVIDER, recorded_with=recorded_with
    )


def extractor(provider: Provider) -> LlmPageExtractor:
    """The pipeline's extractor, without the shared rate limit (offline tooling only)."""
    limits = Limits(requests_per_minute=1_000, tokens_per_minute=10_000_000)
    gateway = LlmGateway({provider.name: provider}, Unlimited(), default_limits=limits)
    return LlmPageExtractor(gateway, provider.name, MODEL)


@dataclass
class DocumentExtraction:
    """One document of a pack as the pipeline extracted it."""

    kind: DocumentKind | None
    pages: list[ExtractedPage] = field(default_factory=list)

    @property
    def merged(self) -> Merged:
        return merge([page.fragment for page in self.pages])


def extract_pack(pack_id: str, provider: Provider) -> dict[str, DocumentExtraction]:
    """Extract every page of the pack, grouped by document (file stem)."""
    page_extractor = extractor(provider)
    documents: dict[str, DocumentExtraction] = {}
    for golden in pages(pack_id):
        document = documents.setdefault(golden.document, DocumentExtraction(golden.page.kind))
        document.pages.append(page_extractor.extract(golden.page))
    return documents


@dataclass(frozen=True)
class Reading:
    """What extraction made of one document: normalised header values and line items."""

    fields: dict[str, str | None]
    lines: list[dict[str, str | None]]


def values(merged: Merged) -> Reading:
    fields = {name: value.value for name, value in merged.fields.items()}
    lines = [{k: v.value for k, v in line.items()} for line in merged.lines]
    return Reading(fields, lines)


def read_pack(pack_id: str, provider: Provider) -> dict[str, Reading]:
    """Extract every page of the pack and merge each document's pages, as reconcile does."""
    return {doc: values(found.merged) for doc, found in extract_pack(pack_id, provider).items()}


# ---------------------------------------------------------------- scoring


def kinds(kind: DocumentKind) -> dict[str, str]:
    """Normalisation kind of every field a document of ``kind`` can yield."""
    header: dict[str, str] = dict(v3.header_kinds(kind))
    for source, derived in v3.DERIVED.items():
        if source in header:
            header[derived] = "text"
    return {**header, **v3.line_kinds(kind)}


def expected(truth: dict[str, Any]) -> Reading:
    """What a perfect extraction of this document holds, in extraction's field names."""
    kind = DocumentKind(truth["document"])
    known = kinds(kind)
    fields: dict[str, str | None] = {}
    for key, value in truth["fields"].items():
        name = _PARTIES.get(key, key)
        if key in _PARTIES:
            value = value["name"] if value else None
        if value is not None and name in known:
            fields[name] = str(value)
    columns = v3.line_kinds(kind)
    lines: list[dict[str, str | None]] = [
        {key: str(value) for key, value in line.items() if key in columns and value is not None}
        for line in truth["line_items"]
    ]
    return Reading(fields, lines)


def same(kind: str | None, want: str | None, got: str | None) -> bool:
    """Whether an extracted value is the true one: numbers numerically; free text ignoring case
    and spacing (a layout may print a description in capitals or wrap it)."""
    if want is None or got is None:
        return want == got
    if kind == "text":
        return " ".join(want.casefold().split()) == " ".join(got.casefold().split())
    if kind not in _NUMERIC:
        return want == got
    try:
        return Decimal(want) == Decimal(got)
    except InvalidOperation:
        return False


def _compare(
    where: str,
    known: dict[str, str],
    want: dict[str, str | None],
    got: dict[str, str | None],
) -> list[str]:
    keys = sorted({k for k, v in want.items() if v is not None} | {k for k, v in got.items() if v})
    return [
        f"{where}.{key}: expected {want.get(key)!r}, got {got.get(key)!r}"
        for key in keys
        if not same(known.get(key), want.get(key), got.get(key))
    ]


def differences(truth: dict[str, Any], reading: Reading) -> list[str]:
    """Every way ``reading`` departs from the document's truth (empty: perfect)."""
    want, document = expected(truth), truth["document"]
    known = kinds(DocumentKind(document))
    problems = _compare(document, known, want.fields, reading.fields)
    alignment = align(want.lines, reading.lines, line_distance)
    problems += [f"{document}: line {i + 1} not extracted" for i in alignment.left_only]
    problems += [
        f"{document}: extracted line {j + 1} is not in truth" for j in alignment.right_only
    ]
    for i, j in alignment.pairs:
        problems += _compare(f"{document}.line{i + 1}", known, want.lines[i], reading.lines[j])
    return problems


def accuracy(truth: dict[str, Any], reading: Reading) -> tuple[int, int]:
    """(values right, values expected): what a live run reports."""
    want = expected(truth)
    total = len(want.fields) + sum(len(line) for line in want.lines)
    return max(total - len(differences(truth, reading)), 0), total


# ---------------------------------------------------------------- CLI


def seed(pack_id: str, root: Path = CASSETTES) -> None:
    """Record the pack's cassette from ground truth (no API key, no network)."""
    shutil.rmtree(root / pack_id, ignore_errors=True)
    oracle = OracleProvider(truths(pack_id), layout(pack_id), ocr=variant(pack_id) == "scanned")
    read_pack(pack_id, cassette(pack_id, mode="record", inner=oracle, root=root))


def record(pack_id: str, root: Path = CASSETTES, provider: Provider | None = None) -> None:
    """Record the pack's cassette from a real model (Anthropic unless ``provider`` is given)."""
    shutil.rmtree(root / pack_id, ignore_errors=True)
    live = provider or AnthropicProvider()
    read_pack(pack_id, cassette(pack_id, mode="record", inner=live, root=root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manifest_evals.golden", description=__doc__)
    parser.add_argument("action", choices=["seed", "record"])
    parser.add_argument("packs", nargs="*", help="pack ids (default: the whole corpus)")
    args = parser.parse_args(argv)
    failed = False
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for pack_id in args.packs or golden_packs():
        seed(pack_id) if args.action == "seed" else record(pack_id)
        replayed = read_pack(pack_id, cassette(pack_id))
        for document, truth in sorted(truths(pack_id).items()):
            right, total = accuracy(truth, replayed[document])
            totals[variant(pack_id)][0] += right
            totals[variant(pack_id)][1] += total
            if variant(pack_id) != "scanned" and right < total:
                failed = True  # a text-layer page must replay perfectly from ground truth
                for problem in differences(truth, replayed[document]):
                    print(f"  {pack_id} {problem}", file=sys.stderr)
    for name, (right, total) in sorted(totals.items()):
        print(f"{name}: {right}/{total} values right")
    return 1 if failed and args.action == "seed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
