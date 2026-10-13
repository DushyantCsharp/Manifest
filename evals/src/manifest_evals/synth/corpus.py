"""The frozen eval corpus: 55 packs generated once and committed.

    python -m manifest_evals.synth freeze      (``make corpus``)

    clean-0001..0025   golden: the documents agree; clean renders with a text layer (5/layout)
    scan-0001..0015    golden: the documents agree; image-only scans, read by OCR (3/layout)
    mism-0001..0015    1-2 deliberate cross-document mismatches each; clean renders (3/layout)

Each pack folder holds its three PDFs, the ground truth of each, and ``pages.json``: every
page's text exactly as the pipeline read it (the native text layer, else tesseract). Replays
use the snapshot, so a different PDFium or tesseract build can't change the requests they look
up; ``TOOLS.json`` records the versions that read them. ``evals/splits.json`` assigns packs
(stratified by variant and layout) and HS descriptions (by chapter) to the calibration or the
test split. Frozen means committed: regenerating rewrites every file and every cassette key,
so only do it on purpose.
"""

import json
import random
import shutil
import subprocess
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

import pypdfium2

from manifest_evals.synth.generate import MUTATIONS, build_shipment, mutate
from manifest_evals.synth.layouts import BY_NAME, LAYOUTS
from manifest_evals.synth.model import DOCUMENTS, Pack
from manifest_evals.synth.output import write_json
from manifest_evals.synth.render import render_pdf
from manifest_evals.synth.scan import page_count, scan
from manifest_evals.synth.truth import document_truth, pack_label
from manifest_worker.app import WorkerSettings
from manifest_worker.text import native_text, split_pages, tesseract_ocr

Variant = Literal["clean", "scan", "mism"]
COUNTS: dict[Variant, int] = {"clean": 25, "scan": 15, "mism": 15}
LABEL_VARIANT = {"clean": "clean", "scan": "scanned", "mism": "mismatch"}
PREFIXES = tuple(f"{variant}-" for variant in COUNTS)
SEED = 7022
SPLITS = ("calibration", "test")
OCR_MIN_CHARS: int = WorkerSettings.model_fields["ocr_min_chars"].default


@dataclass(frozen=True)
class CorpusPack:
    pack: Pack
    variant: Variant


def build_corpus(seed: int = SEED) -> list[CorpusPack]:
    """The corpus's packs, the same every time for the same seed."""
    master = random.Random(seed)
    queue: deque[str] = deque()
    corpus = []
    for variant, count in COUNTS.items():
        for index in range(count):
            pack_seed = master.getrandbits(32)
            rng = random.Random(pack_seed)
            layout = LAYOUTS[index % len(LAYOUTS)]
            shipment = build_shipment(rng, layout)
            pack = Pack(f"{variant}-{index + 1:04d}", pack_seed, layout.name, shipment)
            pack.views = dict.fromkeys(DOCUMENTS, shipment)
            if variant == "mism":  # every kind of mismatch leads in turn, as in build_packs
                queue = queue or deque(MUTATIONS)
                first = queue.popleft()
                mutate(rng, pack, first)
                if all(m.mutation != first for m in pack.mismatches):
                    queue.appendleft(first)
            corpus.append(CorpusPack(pack, variant))
    return corpus


def read_pages(pdf: bytes, ocr: Callable[[bytes], str]) -> list[dict[str, Any]]:
    """Each page's text as the pipeline reads it: the native layer, else OCR."""
    pages = []
    for page_no, page in enumerate(split_pages(pdf), start=1):
        text, source = native_text(page), "native"
        if len(text.strip()) < OCR_MIN_CHARS:
            text, source = ocr(page), "ocr"
        pages.append({"page_no": page_no, "source": source, "text": text})
    return pages


def write_pack(
    entry: CorpusPack, golden: Path, labels: Path, *, dpi: int, ocr: Callable[[bytes], str]
) -> None:
    pack, layout = entry.pack, BY_NAME[entry.pack.layout]
    folder = golden / pack.pack_id
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    scan_rng = random.Random(pack.seed ^ 0x5CA9)
    snapshot: dict[str, Any] = {}
    for kind in DOCUMENTS:
        pdf = render_pdf(pack, kind, layout)
        pages = page_count(pdf)
        params = None
        if entry.variant == "scan":
            pdf, params = scan(pdf, scan_rng, dpi=dpi)
        (folder / f"{kind}.pdf").write_bytes(pdf)
        truth = document_truth(pack, kind, layout, pages=pages, scan=params)
        write_json(folder / f"{kind}.json", truth)
        snapshot[kind.value] = read_pages(pdf, ocr)
    write_json(folder / "pages.json", snapshot)
    label = pack_label(pack, layout)
    label.pop("scanned_documents")
    label["variant"] = LABEL_VARIANT[entry.variant]
    labels.mkdir(parents=True, exist_ok=True)
    write_json(labels / f"{pack.pack_id}.json", label)


def pack_splits(corpus: Sequence[CorpusPack]) -> dict[str, list[str]]:
    """Calibration/test halves, alternating within each (variant, layout) group."""
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entry in corpus:
        groups[(entry.variant, entry.pack.layout)].append(entry.pack.pack_id)
    split: dict[str, list[str]] = {name: [] for name in SPLITS}
    for g, (_, ids) in enumerate(sorted(groups.items())):
        for i, pack_id in enumerate(sorted(ids)):
            split[SPLITS[(g + i) % 2]].append(pack_id)
    return {name: sorted(ids) for name, ids in split.items()}


def hs_splits(labels: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """Calibration/test halves of the HS descriptions: grouped by code (a code never straddles
    the split) and alternating within each chapter."""
    by_chapter: dict[str, list[str]] = defaultdict(list)
    for label in labels:
        codes = by_chapter[label["hs6"][:2]]
        if label["hs6"] not in codes:
            codes.append(label["hs6"])
    side: dict[str, str] = {}
    for c, (_, codes) in enumerate(sorted(by_chapter.items())):
        for i, code in enumerate(sorted(codes)):
            side[code] = SPLITS[(c + i) % 2]
    split: dict[str, list[str]] = {name: [] for name in SPLITS}
    for label in labels:
        split[side[label["hs6"]]].append(label["id"])
    return {name: sorted(ids) for name, ids in split.items()}


def tool_versions(seed: int) -> dict[str, Any]:
    tesseract = subprocess.run(
        ["tesseract", "--version"],  # noqa: S607 - from PATH, like the pipeline
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()[0]
    return {
        "seed": seed,
        "pypdfium2": metadata.version("pypdfium2"),
        "pdfium": str(pypdfium2.PDFIUM_INFO),
        "tesseract": tesseract,
        "weasyprint": metadata.version("weasyprint"),
    }


def read_hs_labels(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def freeze(
    out: Path,
    *,
    seed: int = SEED,
    dpi: int = 150,
    ocr: Callable[[bytes], str] = tesseract_ocr,
    progress: Callable[[str], None] = print,
) -> list[CorpusPack]:
    """Write the corpus, its page snapshots, tool versions and splits under ``out``."""
    golden, labels = out / "golden", out / "labels"
    corpus = build_corpus(seed)
    for stale in [p for prefix in PREFIXES for p in golden.glob(f"{prefix}*")]:
        shutil.rmtree(stale)
    for stale in [p for prefix in PREFIXES for p in labels.glob(f"{prefix}*.json")]:
        stale.unlink()
    for entry in corpus:
        write_pack(entry, golden, labels, dpi=dpi, ocr=ocr)
        progress(f"{entry.pack.pack_id} {entry.pack.layout:<10} {len(entry.pack.mismatches)}")
    write_json(golden / "TOOLS.json", tool_versions(seed))
    splits = {
        "packs": pack_splits(corpus),
        "hs": hs_splits(read_hs_labels(labels / "hs.jsonl")),
    }
    write_json(out / "splits.json", splits)
    return corpus
