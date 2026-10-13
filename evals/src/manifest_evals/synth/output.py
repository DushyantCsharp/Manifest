"""Write generated packs where the eval harness discovers them.

evals/golden/<pack_id>/<document>.pdf          clean render (has a text layer)
evals/golden/<pack_id>/<document>.json         ground truth for that PDF
evals/golden/<pack_id>/<document>.scanned.pdf  rasterised, skewed, noisy (needs OCR)
evals/golden/<pack_id>/<document>.scanned.json
evals/labels/<pack_id>.json                    shipment truth + expected discrepancies
"""

import json
import random
import shutil
from pathlib import Path
from typing import Any

from manifest_evals.synth.layouts import BY_NAME
from manifest_evals.synth.model import DOCUMENTS, Pack
from manifest_evals.synth.render import render_pdf
from manifest_evals.synth.scan import page_count, scan
from manifest_evals.synth.truth import document_truth, pack_label

PREFIX = "syn-"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def clear(golden: Path, labels: Path) -> None:
    """Remove previously generated packs (only ours: ``syn-*``), so none go stale."""
    for folder in golden.glob(f"{PREFIX}*/"):
        shutil.rmtree(folder)
    for label in labels.glob(f"{PREFIX}*.json"):
        label.unlink()


def write_pack(
    pack: Pack, golden: Path, labels: Path, *, scans: bool = True, dpi: int = 150
) -> None:
    layout = BY_NAME[pack.layout]
    folder = golden / pack.pack_id
    folder.mkdir(parents=True, exist_ok=True)
    scan_rng = random.Random(pack.seed ^ 0x5CA9)  # independent of the content RNG
    for kind in DOCUMENTS:
        pdf = render_pdf(pack, kind, layout)
        pages = page_count(pdf)
        (folder / f"{kind}.pdf").write_bytes(pdf)
        write_json(folder / f"{kind}.json", document_truth(pack, kind, layout, pages=pages))
        if scans:
            scanned, params = scan(pdf, scan_rng, dpi=dpi)
            (folder / f"{kind}.scanned.pdf").write_bytes(scanned)
            truth = document_truth(pack, kind, layout, pages=pages, scan=params)
            write_json(folder / f"{kind}.scanned.json", truth)
    labels.mkdir(parents=True, exist_ok=True)
    write_json(labels / f"{pack.pack_id}.json", pack_label(pack, layout))
