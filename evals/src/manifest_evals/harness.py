"""Golden-pack discovery and integrity checks.

Layout::

    evals/golden/<pack_id>/*.pdf   source documents for one shipment (other files, such as
                                   per-document ground truth JSON, are not documents)
    evals/labels/<pack_id>.json    ground truth for that pack
"""

from dataclasses import dataclass
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class GoldenPack:
    pack_id: str
    documents: tuple[Path, ...]
    label: Path


@dataclass(frozen=True)
class Discovery:
    packs: tuple[GoldenPack, ...]
    problems: tuple[str, ...]


def discover(golden_dir: Path, labels_dir: Path) -> Discovery:
    pack_dirs = {p.name: p for p in golden_dir.glob("*/")} if golden_dir.is_dir() else {}
    labels = {p.stem: p for p in labels_dir.glob("*.json")} if labels_dir.is_dir() else {}

    problems = [f"pack {pid!r} has no label" for pid in pack_dirs.keys() - labels.keys()]
    problems += [f"label {pid!r} has no pack" for pid in labels.keys() - pack_dirs.keys()]

    packs = []
    for pid in sorted(pack_dirs.keys() & labels.keys()):
        files = pack_dirs[pid].iterdir()
        documents = tuple(sorted(f for f in files if f.is_file() and f.suffix == ".pdf"))
        if not documents:
            problems.append(f"pack {pid!r} has no documents")
            continue
        packs.append(GoldenPack(pack_id=pid, documents=documents, label=labels[pid]))

    return Discovery(packs=tuple(packs), problems=tuple(sorted(problems)))
