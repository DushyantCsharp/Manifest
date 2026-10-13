"""Synthetic packs: real HS labels, exact arithmetic, and mismatches exactly where labelled."""

import csv
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

import manifest_db
from manifest_domain.lifecycle import DocumentKind
from manifest_evals.synth.catalog import CATALOG
from manifest_evals.synth.generate import MUTATIONS, build_packs
from manifest_evals.synth.layouts import BY_NAME, LAYOUTS
from manifest_evals.synth.model import CENT, DOCUMENTS, Pack
from manifest_evals.synth.render import amount_in_words
from manifest_evals.synth.truth import document_truth, pack_label
from manifest_evals.synth.world import iso6346_check_digit

HS_CSV = Path(manifest_db.__file__).parent / "migrations/data/hs2022/harmonized-system.csv"
CI, PL, BL = DOCUMENTS


@pytest.fixture(scope="module")
def corpus() -> list[Pack]:
    return build_packs(40, 15, seed=2022)  # what `make seed` writes


def truths(pack: Pack) -> dict[DocumentKind, dict[str, Any]]:
    layout = BY_NAME[pack.layout]
    return {kind: document_truth(pack, kind, layout, pages=1) for kind in DOCUMENTS}


def disagreements(pack: Pack) -> set[tuple[str, str | None]]:
    """Every place a pack's documents contradict each other (or an invoice its own lines)."""
    by_kind = truths(pack)
    fields = {kind: truth["fields"] for kind, truth in by_kind.items()}
    lines = {
        kind: {line["description"]: line for line in truth["line_items"]}
        for kind, truth in by_kind.items()
    }
    found: set[tuple[str, str | None]] = set()
    pairs = [
        ("invoice_number", CI, PL),
        ("incoterm", CI, PL),
        ("total_packages", PL, BL),
        ("total_gross_weight_kg", PL, BL),
    ]
    for field, a, b in pairs:
        if fields[a][field] != fields[b][field]:
            found.add((field, None))
    if fields[CI]["buyer"]["name"] != fields[BL]["buyer"]["name"]:
        found.add(("buyer.name", None))
    line_sum = sum(Decimal(line["amount"]) for line in lines[CI].values())
    if Decimal(fields[CI]["total_amount"]) != line_sum:
        found.add(("total_amount", None))
    for description, invoiced in lines[CI].items():
        packed = lines[PL].get(description)
        if packed is None:
            found.add(("line_items", description))
            continue
        for key in ("quantity", "country_of_origin"):
            if invoiced[key] != packed[key]:
                found.add((f"line_items.{key}", description))
    return found


def floats_in(value: Any) -> list[Any]:
    if isinstance(value, float):
        return [value]
    if isinstance(value, dict):
        return [f for v in value.values() for f in floats_in(v)]
    if isinstance(value, list):
        return [f for v in value for f in floats_in(v)]
    return []


# ---------------------------------------------------------------- catalog and corpus


def test_catalog_hs_codes_are_hs2022_subheadings() -> None:
    with HS_CSV.open(encoding="utf-8") as handle:
        subheadings = {row["hscode"] for row in csv.DictReader(handle) if row["level"] == "6"}
    assert {product.hs_code for product in CATALOG} - subheadings == set()


def test_default_corpus_shape(corpus: list[Pack]) -> None:
    mismatched = [pack for pack in corpus if pack.mismatches]
    assert len(mismatched) == 15
    assert {m.mutation for pack in mismatched for m in pack.mismatches} == set(MUTATIONS)
    assert {layout.name: sum(p.layout == layout.name for p in corpus) for layout in LAYOUTS} == {
        layout.name: 8 for layout in LAYOUTS
    }


def test_generation_is_deterministic() -> None:
    def labels(seed: int) -> str:
        packs = build_packs(10, 4, seed)
        return json.dumps([pack_label(pack, BY_NAME[pack.layout]) for pack in packs])

    assert labels(7) == labels(7)
    assert labels(7) != labels(8)


# ---------------------------------------------------------------- ground truth


def test_clean_packs_agree_across_documents(corpus: list[Pack]) -> None:
    for pack in corpus:
        if not pack.mismatches:
            assert disagreements(pack) == set(), pack.pack_id


def test_mismatches_change_only_what_the_label_says(corpus: list[Pack]) -> None:
    for pack in corpus:
        if not pack.mismatches:
            continue
        label = pack_label(pack, BY_NAME[pack.layout])
        expected = {(d["field"], d["description"]) for d in label["discrepancies"]}
        assert disagreements(pack) == expected, pack.pack_id
        for discrepancy in label["discrepancies"]:
            values = list(discrepancy["values"].values())
            assert len(values) == 1 or values[0] != values[1], discrepancy


def test_money_is_exact_and_nothing_is_a_float(corpus: list[Pack]) -> None:
    for pack in corpus:
        for line in pack.shipment.lines:
            # Commercial rounding (half up), not Python's default banker's rounding.
            assert line.amount == (line.quantity * line.unit_price).quantize(CENT, ROUND_HALF_UP)
        assert floats_in(pack_label(pack, BY_NAME[pack.layout])) == []
        assert all(floats_in(truth) == [] for truth in truths(pack).values())


def test_lb_documents_record_the_kg_their_printed_pounds_mean() -> None:
    letterhead = BY_NAME["letterhead"]
    assert letterhead.weight(Decimal("1000.00")) == "2,204.6 lb"
    assert letterhead.kg_truth(Decimal("1000.00")) == Decimal("999.99")  # 2,204.6 lb in kg
    assert BY_NAME["us"].kg_truth(Decimal("1000.004")) == Decimal("1000.00")


def test_number_and_date_conventions() -> None:
    amount = Decimal("1234567.5")
    assert BY_NAME["us"].money(amount) == "1,234,567.50"
    assert BY_NAME["eu"].money(amount) == "1.234.567,50"
    assert BY_NAME["compact"].money(amount) == "1\u202f234\u202f567,50"
    day = date(2026, 9, 30)
    printed = {name: BY_NAME[name].date(day) for name in BY_NAME}
    assert printed == {
        "us": "09/30/2026",
        "eu": "30.09.2026",
        "exporter": "30-SEP-2026",
        "letterhead": "September 30, 2026",
        "compact": "2026-09-30",
    }


def test_amount_in_words() -> None:
    assert amount_in_words(Decimal("151727.96"), "USD") == (
        "SAY TOTAL US DOLLARS ONE HUNDRED FIFTY-ONE THOUSAND SEVEN HUNDRED TWENTY-SEVEN "
        "AND CENTS NINETY-SIX ONLY"
    )
    two_thousand = amount_in_words(Decimal("2000.00"), "GBP")
    assert two_thousand == "SAY TOTAL POUNDS STERLING TWO THOUSAND ONLY"


def test_container_check_digit_matches_iso_6346_example() -> None:
    assert iso6346_check_digit("CSQU305438") == 3


# ---------------------------------------------------------------- rendering (needs Pango)


def _weasyprint_loads() -> bool:
    try:
        import weasyprint  # noqa: F401
    except OSError:
        return False
    return True


@pytest.mark.skipif(
    not _weasyprint_loads(),
    reason="WeasyPrint can't find Pango; run via `make test` (sets the library path on macOS)",
)
def test_writes_pdfs_with_truth_alongside(tmp_path: Path) -> None:
    import pypdfium2 as pdfium

    from manifest_evals.synth.output import write_pack

    pack = build_packs(1, 1, seed=11)[0]
    write_pack(pack, tmp_path / "golden", tmp_path / "labels")
    folder = tmp_path / "golden" / pack.pack_id

    expected = {
        f"{kind}{variant}.{ext}"
        for kind in DOCUMENTS
        for variant in ("", ".scanned")
        for ext in ("pdf", "json")
    }
    assert {path.name for path in folder.iterdir()} == expected

    view = pack.views[CI]
    text = pdfium.PdfDocument(folder / "commercial_invoice.pdf")[0].get_textpage().get_text_range()
    assert view.invoice_number in text
    assert BY_NAME[pack.layout].money(view.total_amount) in text

    scanned = pdfium.PdfDocument(folder / "commercial_invoice.scanned.pdf")
    assert scanned[0].get_textpage().get_text_range().strip() == ""  # image only: needs OCR
    truth = json.loads((folder / "commercial_invoice.scanned.json").read_text())
    assert truth["variant"] == "scanned"
    assert abs(Decimal(truth["scan"]["rotation_degrees"])) <= 3
    assert (tmp_path / "labels" / f"{pack.pack_id}.json").is_file()
