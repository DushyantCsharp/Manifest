"""A synthetic shipment and the per-document views that describe it.

``Shipment`` is what really shipped. Each document renders its own view of it; a deliberate
mismatch alters one view (``dataclasses.replace``), never the shipment itself. Money,
quantities and weights are Decimal throughout.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from manifest_domain.lifecycle import DocumentKind
from manifest_domain.review import DiscrepancyKind
from manifest_evals.synth.world import Party

CENT = Decimal("0.01")

DOCUMENTS = (
    DocumentKind.COMMERCIAL_INVOICE,
    DocumentKind.PACKING_LIST,
    DocumentKind.BILL_OF_LADING,
)


@dataclass(frozen=True)
class Line:
    line_no: int
    description: str
    hs_code: str
    country_of_origin: str | None
    quantity: Decimal
    unit: str
    unit_price: Decimal
    packages: int
    net_weight_kg: Decimal
    gross_weight_kg: Decimal

    @property
    def amount(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True)
class Shipment:
    seller: Party
    buyer: Party
    notify_party: Party | None  # None: "same as consignee"
    invoice_number: str
    invoice_date: date
    currency: str
    incoterm: str
    incoterm_place: str
    payment_terms: str
    lines: tuple[Line, ...]
    package_type: str
    marks: str
    bl_number: str
    carrier: str
    vessel: str
    voyage: str
    port_of_loading: str
    port_of_discharge: str
    shipped_on_board: date
    container_number: str
    seal_number: str
    # Totals a document states that disagree with its own lines (set only by mismatches).
    stated_total_amount: Decimal | None = None
    stated_total_packages: int | None = None
    stated_net_weight_kg: Decimal | None = None
    stated_gross_weight_kg: Decimal | None = None

    @property
    def total_amount(self) -> Decimal:
        computed = sum((line.amount for line in self.lines), Decimal(0))
        return computed if self.stated_total_amount is None else self.stated_total_amount

    @property
    def total_packages(self) -> int:
        computed = sum(line.packages for line in self.lines)
        return computed if self.stated_total_packages is None else self.stated_total_packages

    @property
    def total_net_weight_kg(self) -> Decimal:
        computed = sum((line.net_weight_kg for line in self.lines), Decimal(0))
        return computed if self.stated_net_weight_kg is None else self.stated_net_weight_kg

    @property
    def total_gross_weight_kg(self) -> Decimal:
        computed = sum((line.gross_weight_kg for line in self.lines), Decimal(0))
        return computed if self.stated_gross_weight_kg is None else self.stated_gross_weight_kg

    @property
    def freight_terms(self) -> str:
        return "PREPAID" if self.incoterm[0] in "CD" else "COLLECT"


@dataclass(frozen=True)
class Mismatch:
    """One deliberate inconsistency, and what reconciliation should report for it."""

    mutation: str
    kind: DiscrepancyKind
    field: str  # truth field key; "line_items.<key>" for line-level fields
    documents: tuple[DocumentKind, ...]
    line_no: int | None = None  # invoice line number for line-level fields


@dataclass
class Pack:
    pack_id: str
    seed: int
    layout: str
    shipment: Shipment
    views: dict[DocumentKind, Shipment] = field(default_factory=dict)
    mismatches: list[Mismatch] = field(default_factory=list)
