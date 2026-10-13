"""The frozen eval corpus: its shape, its page snapshots, its splits and its HS labels."""

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest

import manifest_db
from manifest_evals.harness import EVALS_ROOT
from manifest_evals.synth.corpus import (
    COUNTS,
    LABEL_VARIANT,
    PREFIXES,
    build_corpus,
    hs_splits,
    pack_splits,
    read_hs_labels,
)
from manifest_evals.synth.generate import MUTATIONS
from manifest_evals.synth.layouts import LAYOUTS
from manifest_evals.synth.model import DOCUMENTS
from manifest_worker.text import native_text, split_pages

GOLDEN, LABELS = EVALS_ROOT / "golden", EVALS_ROOT / "labels"
HS_CSV = Path(manifest_db.__file__).parent / "migrations/data/hs2022/harmonized-system.csv"
CORPUS = build_corpus()
PACK_IDS = sorted(entry.pack.pack_id for entry in CORPUS)


def committed() -> list[Path]:
    return sorted(p for prefix in PREFIXES for p in GOLDEN.glob(f"{prefix}*") if p.is_dir())


def test_corpus_shape() -> None:
    assert (
        Counter(entry.variant for entry in CORPUS)
        == COUNTS
        == {
            "clean": 25,
            "scan": 15,
            "mism": 15,
        }
    )
    per_layout = Counter((entry.variant, entry.pack.layout) for entry in CORPUS)
    for layout in LAYOUTS:
        assert per_layout[("clean", layout.name)] == 5
        assert per_layout[("scan", layout.name)] == 3
        assert per_layout[("mism", layout.name)] == 3
    for entry in CORPUS:
        assert bool(entry.pack.mismatches) is (entry.variant == "mism"), entry.pack.pack_id
    led = {entry.pack.mismatches[0].mutation for entry in CORPUS if entry.variant == "mism"}
    assert led == set(MUTATIONS)  # every kind of mismatch is represented


def test_corpus_is_deterministic() -> None:
    again = build_corpus()
    assert [(e.pack.pack_id, e.pack.seed) for e in again] == [
        (e.pack.pack_id, e.pack.seed) for e in CORPUS
    ]


def test_the_committed_corpus_is_the_built_one() -> None:
    assert [path.name for path in committed()] == PACK_IDS
    for entry in CORPUS:
        pack_id = entry.pack.pack_id
        folder = GOLDEN / pack_id
        for kind in DOCUMENTS:
            assert (folder / f"{kind}.pdf").is_file()
            truth = json.loads((folder / f"{kind}.json").read_text())
            assert truth["pack_id"] == pack_id
        label = json.loads((LABELS / f"{pack_id}.json").read_text())
        assert label["variant"] == LABEL_VARIANT[entry.variant]
        assert bool(label["discrepancies"]) is (entry.variant == "mism")


def _snapshot(pack_id: str) -> dict[str, list[dict[str, Any]]]:
    text = (GOLDEN / pack_id / "pages.json").read_text()
    data: dict[str, list[dict[str, Any]]] = json.loads(text)
    return data


@pytest.mark.parametrize("pack_id", [p for p in PACK_IDS if not p.startswith("scan-")])
def test_clean_snapshots_are_the_native_text_layer(pack_id: str) -> None:
    snapshot = _snapshot(pack_id)
    for kind in DOCUMENTS:
        pages = split_pages((GOLDEN / pack_id / f"{kind}.pdf").read_bytes())
        assert [p["text"] for p in snapshot[kind.value]] == [native_text(p) for p in pages]
        assert {p["source"] for p in snapshot[kind.value]} == {"native"}


def test_scanned_packs_were_read_by_ocr() -> None:
    tools = json.loads((GOLDEN / "TOOLS.json").read_text())
    assert tools["tesseract"].startswith("tesseract ")
    for pack_id in (p for p in PACK_IDS if p.startswith("scan-")):
        snapshot = _snapshot(pack_id)
        for kind in DOCUMENTS:
            pdf = (GOLDEN / pack_id / f"{kind}.pdf").read_bytes()
            assert all(native_text(page).strip() == "" for page in split_pages(pdf))  # image-only
            assert {p["source"] for p in snapshot[kind.value]} == {"ocr"}
            assert all(p["text"].strip() for p in snapshot[kind.value])


SPLITS = json.loads((EVALS_ROOT / "splits.json").read_text())


def test_pack_splits_partition_the_corpus_and_are_stratified() -> None:
    calibration, test = SPLITS["packs"]["calibration"], SPLITS["packs"]["test"]
    assert sorted(calibration + test) == PACK_IDS
    assert not set(calibration) & set(test)
    assert SPLITS["packs"] == pack_splits(CORPUS)
    side = dict.fromkeys(calibration, "calibration") | dict.fromkeys(test, "test")
    groups: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for entry in CORPUS:
        groups[(entry.variant, entry.pack.layout)][side[entry.pack.pack_id]] += 1
    for counts in groups.values():
        assert abs(counts["calibration"] - counts["test"]) <= 1


HS_LABELS = read_hs_labels(LABELS / "hs.jsonl")


def test_hs_labels_are_real_hs2022_subheadings() -> None:
    with HS_CSV.open(encoding="utf-8") as handle:
        subheadings = {row["hscode"] for row in csv.DictReader(handle) if row["level"] == "6"}
    assert len(HS_LABELS) == 120
    assert len({label["id"] for label in HS_LABELS}) == 120
    for label in HS_LABELS:
        assert label["hs6"] in subheadings, label
        assert label["hs6"] not in label["description"]  # the answer isn't in the question
        assert label["origin"] is None or len(label["origin"]) == 2


def test_hs_split_never_splits_a_code() -> None:
    calibration, test = SPLITS["hs"]["calibration"], SPLITS["hs"]["test"]
    assert sorted(calibration + test) == sorted(label["id"] for label in HS_LABELS)
    assert SPLITS["hs"] == hs_splits(HS_LABELS)
    code = {label["id"]: label["hs6"] for label in HS_LABELS}
    assert not {code[i] for i in calibration} & {code[i] for i in test}
    assert abs(len(calibration) - len(test)) <= 12
