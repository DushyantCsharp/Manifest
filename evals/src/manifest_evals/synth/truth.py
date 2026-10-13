"""Ground-truth JSON for synthetic packs.

Per document (written next to each PDF): the normalised value of everything that document
prints, i.e. what a perfect extractor plus normaliser should produce. Values are strings for
decimals (never floats), ISO 8601 dates, ISO 4217 currencies and ISO 3166-1 alpha-2 countries;
weights are kg as normalised from what was printed (lb layouts round-trip through lb).

Per pack (``evals/labels/<pack_id>.json``): the shipment as it really was, with each line's
true HS code, plus the discrepancies reconciliation is expected to report.
"""

from typing import Any

from manifest_domain.lifecycle import DocumentKind
from manifest_evals.synth.layouts import Layout
from manifest_evals.synth.model import DOCUMENTS, Line, Mismatch, Pack, Shipment
from manifest_evals.synth.world import Party

TRUTH_VERSION = 1
# Reconciliation should treat weights within this relative difference as equal: lb layouts
# round to 0.1 lb, so their kg values differ slightly from documents printed in kg.
WEIGHT_TOLERANCE = "0.005"


def _party(party: Party | None) -> dict[str, Any] | None:
    if party is None:
        return None
    return {"name": party.name, "address": list(party.address), "country": party.country}


def _invoice_line(line: Line, layout: Layout) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "line_no": line.line_no,
        "description": line.description,
        "country_of_origin": line.country_of_origin,
        "quantity": str(line.quantity),
        "unit": line.unit,
        "unit_price": str(line.unit_price),
        "amount": str(line.amount),
    }
    if layout.shows_hs_code:
        fields["hs_code"] = line.hs_code
    return fields


def _packing_line(line: Line, layout: Layout) -> dict[str, Any]:
    return {
        "line_no": line.line_no,
        "description": line.description,
        "country_of_origin": line.country_of_origin,
        "quantity": str(line.quantity),
        "unit": line.unit,
        "packages": line.packages,
        "net_weight_kg": str(layout.kg_truth(line.net_weight_kg)),
        "gross_weight_kg": str(layout.kg_truth(line.gross_weight_kg)),
    }


def goods_description(view: Shipment) -> str:
    """The B/L's summary of the goods, exactly as printed."""
    items = "; ".join(line.description for line in view.lines)
    return f"SAID TO CONTAIN: {items}"


def document_fields(view: Shipment, kind: DocumentKind, layout: Layout) -> dict[str, Any]:
    match kind:
        case DocumentKind.COMMERCIAL_INVOICE:
            return {
                "invoice_number": view.invoice_number,
                "invoice_date": view.invoice_date.isoformat(),
                "seller": _party(view.seller),
                "buyer": _party(view.buyer),
                "currency": view.currency,
                "incoterm": view.incoterm,
                "incoterm_place": view.incoterm_place,
                "payment_terms": view.payment_terms,
                "total_amount": str(view.total_amount),
            }
        case DocumentKind.PACKING_LIST:
            return {
                "invoice_number": view.invoice_number,
                "date": view.invoice_date.isoformat(),
                "seller": _party(view.seller),
                "buyer": _party(view.buyer),
                "incoterm": view.incoterm,
                "incoterm_place": view.incoterm_place,
                "package_type": view.package_type,
                "total_packages": view.total_packages,
                "total_net_weight_kg": str(layout.kg_truth(view.total_net_weight_kg)),
                "total_gross_weight_kg": str(layout.kg_truth(view.total_gross_weight_kg)),
            }
        case DocumentKind.BILL_OF_LADING:
            return {
                "bl_number": view.bl_number,
                "seller": _party(view.seller),
                "buyer": _party(view.buyer),
                "notify_party": _party(view.notify_party),
                "carrier": view.carrier,
                "vessel": view.vessel,
                "voyage": view.voyage,
                "port_of_loading": view.port_of_loading,
                "port_of_discharge": view.port_of_discharge,
                "shipped_on_board": view.shipped_on_board.isoformat(),
                "container_number": view.container_number,
                "seal_number": view.seal_number,
                "freight_terms": view.freight_terms,
                "package_type": view.package_type,
                "total_packages": view.total_packages,
                "total_gross_weight_kg": str(layout.kg_truth(view.total_gross_weight_kg)),
                "goods_description": goods_description(view),
            }
        case _:
            raise ValueError(f"no synthetic template for {kind}")


def document_lines(view: Shipment, kind: DocumentKind, layout: Layout) -> list[dict[str, Any]]:
    if kind is DocumentKind.COMMERCIAL_INVOICE:
        return [_invoice_line(line, layout) for line in view.lines]
    if kind is DocumentKind.PACKING_LIST:
        return [_packing_line(line, layout) for line in view.lines]
    return []  # a B/L describes the goods in summary only


def document_truth(
    pack: Pack,
    kind: DocumentKind,
    layout: Layout,
    *,
    pages: int,
    scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    view = pack.views[kind]
    return {
        "truth_version": TRUTH_VERSION,
        "pack_id": pack.pack_id,
        "document": kind.value,
        "layout": layout.name if kind is DocumentKind.COMMERCIAL_INVOICE else "standard",
        "number_style": layout.number_style,
        "variant": "clean" if scan is None else "scanned",
        "scan": scan,
        "pages": pages,
        "fields": document_fields(view, kind, layout),
        "line_items": document_lines(view, kind, layout),
    }


def _lookup(truth: dict[str, Any], field: str, description: str | None) -> Any:
    """The value ``truth`` states for ``field``; line fields are matched by description."""
    if field.startswith("line_items"):
        line = next((li for li in truth["line_items"] if li["description"] == description), None)
        if field == "line_items" or line is None:
            return None if line is None else line["description"]
        return line[field.removeprefix("line_items.")]
    value: Any = truth["fields"]
    for key in field.split("."):
        value = value[key]
    return value


def _discrepancy(
    mismatch: Mismatch, pack: Pack, truths: dict[DocumentKind, dict[str, Any]]
) -> dict[str, Any]:
    description = None
    if mismatch.line_no is not None:
        description = pack.shipment.lines[mismatch.line_no - 1].description
    return {
        "mutation": mismatch.mutation,
        "kind": mismatch.kind.value,
        "field": mismatch.field,
        "line_no": mismatch.line_no,
        "description": description,
        "values": {
            kind.value: _lookup(truths[kind], mismatch.field, description)
            for kind in mismatch.documents
        },
    }


def pack_label(pack: Pack, layout: Layout) -> dict[str, Any]:
    shipment = pack.shipment
    truths = {kind: document_truth(pack, kind, layout, pages=0) for kind in DOCUMENTS}
    shipped_lines = [
        {
            **_invoice_line(line, layout),
            "hs_code": line.hs_code,
            "packages": line.packages,
            "net_weight_kg": str(line.net_weight_kg),
            "gross_weight_kg": str(line.gross_weight_kg),
        }
        for line in shipment.lines
    ]
    return {
        "truth_version": TRUTH_VERSION,
        "pack_id": pack.pack_id,
        "seed": pack.seed,
        "layout": pack.layout,
        "documents": {kind.value: f"{kind.value}.pdf" for kind in DOCUMENTS},
        "scanned_documents": {kind.value: f"{kind.value}.scanned.pdf" for kind in DOCUMENTS},
        "shipment": {
            **document_fields(shipment, DocumentKind.COMMERCIAL_INVOICE, layout),
            **document_fields(shipment, DocumentKind.BILL_OF_LADING, layout),
            "total_net_weight_kg": str(shipment.total_net_weight_kg),
            "total_gross_weight_kg": str(shipment.total_gross_weight_kg),
            "line_items": shipped_lines,
        },
        "tolerances": {"weight_kg_relative": WEIGHT_TOLERANCE},
        "discrepancies": [_discrepancy(m, pack, truths) for m in pack.mismatches],
    }
