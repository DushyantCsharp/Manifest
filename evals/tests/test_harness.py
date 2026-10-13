from pathlib import Path

from manifest_evals.__main__ import main
from manifest_evals.harness import discover


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    golden, labels = tmp_path / "golden", tmp_path / "labels"
    golden.mkdir()
    labels.mkdir()
    return golden, labels


def test_pairs_packs_with_labels(tmp_path: Path) -> None:
    golden, labels = _layout(tmp_path)
    (golden / "pack-001").mkdir()
    (golden / "pack-001" / "invoice.pdf").write_bytes(b"%PDF")
    (golden / "pack-001" / "invoice.json").write_text("{}")  # per-document truth, not a document
    (golden / "pack-001" / ".DS_Store").write_bytes(b"")
    (labels / "pack-001.json").write_text("{}")

    found = discover(golden, labels)

    assert found.problems == ()
    assert [p.pack_id for p in found.packs] == ["pack-001"]
    assert [d.name for d in found.packs[0].documents] == ["invoice.pdf"]


def test_reports_unpaired_and_empty_packs(tmp_path: Path) -> None:
    golden, labels = _layout(tmp_path)
    (golden / "no-label").mkdir()
    (golden / "no-label" / "invoice.pdf").write_bytes(b"%PDF")
    (golden / "empty").mkdir()
    (labels / "empty.json").write_text("{}")
    (labels / "orphan.json").write_text("{}")

    found = discover(golden, labels)

    assert found.packs == ()
    assert found.problems == (
        "label 'orphan' has no pack",
        "pack 'empty' has no documents",
        "pack 'no-label' has no label",
    )


def test_cli_exit_code_reflects_problems(tmp_path: Path) -> None:
    golden, labels = _layout(tmp_path)
    args = ["--golden", str(golden), "--labels", str(labels)]
    assert main(args) == 0

    (labels / "orphan.json").write_text("{}")
    assert main(args) == 1
