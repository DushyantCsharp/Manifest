"""Deterministic pack generation: the same seed always yields the same shipments and mismatches.

Integer randomness only (no floats): every amount is built from Decimal steps.
"""

import math
import random
import string
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from manifest_domain.lifecycle import DocumentKind
from manifest_domain.review import DiscrepancyKind
from manifest_evals.synth.catalog import CATALOG, Product
from manifest_evals.synth.layouts import LAYOUTS, Layout
from manifest_evals.synth.model import CENT, DOCUMENTS, Line, Mismatch, Pack, Shipment
from manifest_evals.synth.world import (
    BUYERS,
    CARRIERS,
    SELLERS,
    VESSEL_NAMES,
    container_number,
    make_party,
)

USD_RATES = {
    "USD": Decimal(1),
    "EUR": Decimal("0.92"),
    "GBP": Decimal("0.79"),
    "CAD": Decimal("1.37"),
}
INCOTERMS = ("EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DDP")  # Incoterms 2020
PAYMENT_TERMS = (
    "T/T 30 days after B/L date",
    "T/T in advance",
    "L/C at sight",
    "30% deposit, 70% against copy of B/L",
    "Cash against documents",
)
FIRST_INVOICE_DATE = date(2026, 1, 5)
MAX_GROSS_KG = Decimal(26_000)  # one 40ft container
ORIGINS = sorted({origin for product in CATALOG for origin in product.origins})

CI = DocumentKind.COMMERCIAL_INVOICE
PL = DocumentKind.PACKING_LIST
BL = DocumentKind.BILL_OF_LADING


def _between(rng: random.Random, low: Decimal, high: Decimal, places: int) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    return low + quantum * rng.randint(0, int((high - low) / quantum))


def _line(rng: random.Random, number: int, product: Product, seller: str, currency: str) -> Line:
    per_package = product.units_per_package
    low, high = (math.ceil(bound / per_package) for bound in product.quantity)
    packages = rng.randint(low, high)
    quantity = packages * per_package
    if per_package > 1 and product.unit != "kg" and rng.randint(1, 4) == 1:
        quantity -= rng.randint(1, per_package - 1)  # last carton part-filled
    price = _between(rng, *product.unit_price_usd, 4) * USD_RATES[currency]
    price_places = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    net = (quantity * _between(rng, *product.net_kg_per_unit, 3)).quantize(CENT, ROUND_HALF_UP)
    packing = net * Decimal(rng.randint(30, 80)) / 1000
    tare = packages * _between(rng, Decimal("0.10"), Decimal("1.50"), 2)
    origin = seller if seller in product.origins else rng.choice(product.origins)
    return Line(
        line_no=number,
        description=product.description,
        hs_code=product.hs_code,
        country_of_origin=origin,
        quantity=Decimal(quantity),
        unit=product.unit,
        unit_price=price.quantize(price_places, ROUND_HALF_UP),
        packages=packages,
        net_weight_kg=net,
        gross_weight_kg=(net + packing + tare).quantize(CENT, ROUND_HALF_UP),
    )


def build_shipment(rng: random.Random, layout: Layout) -> Shipment:
    seller_country = SELLERS[rng.choice(layout.seller_countries)]
    buyer_country = BUYERS[rng.choice(layout.buyer_countries)]
    currency = rng.choice(layout.currencies)

    count = rng.randint(1, layout.max_lines)
    local = [p for p in CATALOG if seller_country.code in p.origins]
    trader = rng.randint(1, 5) == 1 or len(local) < count  # traders mix origins
    products = rng.sample(list(CATALOG) if trader else local, count)
    lines = tuple(
        _line(rng, number, product, seller_country.code, currency)
        for number, product in enumerate(products, start=1)
    )
    while len(lines) > 1 and sum(line.gross_weight_kg for line in lines) > MAX_GROSS_KG:
        lines = lines[:-1]

    invoice_date = FIRST_INVOICE_DATE + timedelta(days=rng.randint(0, 260))
    seller, buyer = make_party(rng, seller_country), make_party(rng, buyer_country)
    port_of_loading = rng.choice(seller_country.ports)
    port_of_discharge = rng.choice(buyer_country.ports)
    incoterm = rng.choice(INCOTERMS)
    place = {
        "E": rng.choice(seller_country.cities),
        "F": port_of_loading if incoterm == "FOB" else rng.choice(seller_country.cities),
        "C": port_of_discharge if incoterm in {"CFR", "CIF"} else rng.choice(buyer_country.cities),
        "D": rng.choice(buyer_country.cities),
    }[incoterm[0]]
    carrier, owner = rng.choice(CARRIERS)
    invoice_number = layout.invoice_number(rng, invoice_date)
    letters = "".join(rng.choice(string.ascii_uppercase) for _ in range(2))
    bulk = all(line.unit == "kg" for line in lines)

    return Shipment(
        seller=seller,
        buyer=buyer,
        notify_party=None if rng.randint(0, 1) else make_party(rng, buyer_country),
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        currency=currency,
        incoterm=incoterm,
        incoterm_place=place,
        payment_terms=rng.choice(PAYMENT_TERMS),
        lines=lines,
        package_type="BAGS" if bulk else "CARTONS",
        marks=f"{buyer.name.split()[0].upper()} / {invoice_number}",
        bl_number=f"{owner}{letters}{rng.randint(1_000_000, 9_999_999)}",
        carrier=carrier,
        vessel=f"{carrier.split()[0]} {rng.choice(VESSEL_NAMES)}",
        voyage=f"{rng.randint(100, 999)}{rng.choice('EWNS')}",
        port_of_loading=port_of_loading,
        port_of_discharge=port_of_discharge,
        shipped_on_board=invoice_date + timedelta(days=rng.randint(2, 12)),
        container_number=container_number(rng, owner),
        seal_number=str(rng.randint(1_000_000, 9_999_999)),
    )


# ---------------------------------------------------------------- deliberate mismatches


def _replace_view(pack: Pack, kind: DocumentKind, **changes: Any) -> None:
    pack.views[kind] = replace(pack.views[kind], **changes)


def _replace_line(pack: Pack, kind: DocumentKind, line_no: int, **changes: Any) -> None:
    view = pack.views[kind]
    lines = tuple(
        replace(line, **changes) if line.line_no == line_no else line for line in view.lines
    )
    pack.views[kind] = replace(view, lines=lines)


def _gross_weight(rng: random.Random, pack: Pack) -> Mismatch | None:
    total = pack.views[BL].total_gross_weight_kg
    factor = 1 + Decimal(rng.randint(30, 90)) / 1000 * rng.choice((1, -1))
    _replace_view(pack, BL, stated_gross_weight_kg=(total * factor).quantize(CENT))
    kind = DiscrepancyKind.VALUE_MISMATCH
    return Mismatch("gross_weight", kind, "total_gross_weight_kg", (PL, BL))


def _package_count(rng: random.Random, pack: Pack) -> Mismatch | None:
    total = pack.views[BL].total_packages
    sign = rng.choice((1, -1)) if total > 3 else 1
    _replace_view(pack, BL, stated_total_packages=total + sign * rng.randint(1, 3))
    return Mismatch("package_count", DiscrepancyKind.VALUE_MISMATCH, "total_packages", (PL, BL))


def _line_quantity(rng: random.Random, pack: Pack) -> Mismatch | None:
    line = rng.choice(pack.views[PL].lines)
    change = Decimal(rng.randint(5, 20)) / 100 * rng.choice((1, -1))
    quantity = max(Decimal(1), (line.quantity * (1 + change)).quantize(Decimal(1)))
    if quantity == line.quantity:
        quantity += 1
    _replace_line(pack, PL, line.line_no, quantity=quantity)
    field = "line_items.quantity"
    return Mismatch("line_quantity", DiscrepancyKind.VALUE_MISMATCH, field, (CI, PL), line.line_no)


def _missing_line(rng: random.Random, pack: Pack) -> Mismatch | None:
    view = pack.views[PL]
    if len(view.lines) == 1:
        return None
    dropped = rng.choice(view.lines)
    kept = [line for line in view.lines if line is not dropped]
    # The totals still describe the whole shipment: only the line itself is missing.
    _replace_view(
        pack,
        PL,
        lines=tuple(replace(line, line_no=n) for n, line in enumerate(kept, start=1)),
        stated_total_packages=view.total_packages,
        stated_net_weight_kg=view.total_net_weight_kg,
        stated_gross_weight_kg=view.total_gross_weight_kg,
    )
    kind = DiscrepancyKind.LINE_ITEMS_MISMATCH
    return Mismatch("missing_line", kind, "line_items", (CI, PL), dropped.line_no)


def _invoice_total(rng: random.Random, pack: Pack) -> Mismatch | None:
    total = pack.views[CI].total_amount
    stated = (total * (1 + Decimal(rng.randint(10, 50)) / 1000)).quantize(CENT)
    _replace_view(pack, CI, stated_total_amount=stated)
    return Mismatch("invoice_total", DiscrepancyKind.TOTALS_MISMATCH, "total_amount", (CI,))


def _invoice_number(rng: random.Random, pack: Pack) -> Mismatch | None:
    number = pack.views[PL].invoice_number
    swappable = [
        i
        for i in range(len(number) - 1)
        if number[i].isdigit() and number[i + 1].isdigit() and number[i] != number[i + 1]
    ]
    if not swappable:
        return None
    i = rng.choice(swappable)
    swapped = f"{number[:i]}{number[i + 1]}{number[i]}{number[i + 2 :]}"
    _replace_view(pack, PL, invoice_number=swapped)
    return Mismatch("invoice_number", DiscrepancyKind.VALUE_MISMATCH, "invoice_number", (CI, PL))


def _consignee(rng: random.Random, pack: Pack) -> Mismatch | None:
    buyer = pack.views[BL].buyer
    other = make_party(rng, BUYERS[buyer.country])
    if other.name == buyer.name:
        return None
    _replace_view(pack, BL, buyer=replace(buyer, name=other.name))
    return Mismatch("consignee", DiscrepancyKind.VALUE_MISMATCH, "buyer.name", (CI, BL))


def _incoterm(rng: random.Random, pack: Pack) -> Mismatch | None:
    current = pack.views[PL].incoterm
    _replace_view(pack, PL, incoterm=rng.choice([t for t in INCOTERMS if t != current]))
    return Mismatch("incoterm", DiscrepancyKind.VALUE_MISMATCH, "incoterm", (CI, PL))


def _origin(rng: random.Random, pack: Pack) -> Mismatch | None:
    line = rng.choice(pack.views[PL].lines)
    other = rng.choice([code for code in ORIGINS if code != line.country_of_origin])
    _replace_line(pack, PL, line.line_no, country_of_origin=other)
    field = "line_items.country_of_origin"
    return Mismatch("origin", DiscrepancyKind.VALUE_MISMATCH, field, (CI, PL), line.line_no)


def _missing_origin(rng: random.Random, pack: Pack) -> Mismatch | None:
    line = rng.choice(pack.views[PL].lines)
    _replace_line(pack, PL, line.line_no, country_of_origin=None)
    field = "line_items.country_of_origin"
    return Mismatch("missing_origin", DiscrepancyKind.MISSING_FIELD, field, (CI, PL), line.line_no)


MUTATIONS: dict[str, Callable[[random.Random, Pack], Mismatch | None]] = {
    "gross_weight": _gross_weight,
    "package_count": _package_count,
    "line_quantity": _line_quantity,
    "missing_line": _missing_line,
    "invoice_total": _invoice_total,
    "invoice_number": _invoice_number,
    "consignee": _consignee,
    "incoterm": _incoterm,
    "origin": _origin,
    "missing_origin": _missing_origin,
}
LINE_LEVEL = frozenset({"line_quantity", "missing_line", "origin", "missing_origin"})


def mutate(rng: random.Random, pack: Pack, first: str) -> None:
    """Apply 1-2 mismatches, trying ``first`` first; at most one touches line items."""
    wanted = 2 if rng.randint(1, 5) <= 2 else 1
    others = [name for name in MUTATIONS if name != first]
    rng.shuffle(others)
    for name in [first, *others]:
        if len(pack.mismatches) == wanted:
            return
        if name in LINE_LEVEL and any(m.mutation in LINE_LEVEL for m in pack.mismatches):
            continue
        mismatch = MUTATIONS[name](rng, pack)
        if mismatch is not None:
            pack.mismatches.append(mismatch)


def build_packs(count: int, mismatched: int, seed: int) -> list[Pack]:
    if not 0 <= mismatched <= count:
        raise ValueError("mismatched must be between 0 and the number of packs")
    master = random.Random(seed)
    with_mismatches = set(master.sample(range(count), mismatched))
    # Each kind of mismatch leads in turn; one that can't apply to a pack (e.g. dropping a
    # line from a one-line pack) stays first in the queue, so every kind gets covered.
    queue: deque[str] = deque()
    packs = []
    for index in range(count):
        pack_seed = master.getrandbits(32)
        rng = random.Random(pack_seed)
        layout = LAYOUTS[index % len(LAYOUTS)]
        shipment = build_shipment(rng, layout)
        pack = Pack(f"syn-{index + 1:04d}", pack_seed, layout.name, shipment)
        pack.views = dict.fromkeys(DOCUMENTS, shipment)
        if index in with_mismatches:
            queue = queue or deque(MUTATIONS)
            first = queue.popleft()
            mutate(rng, pack, first)
            if all(m.mutation != first for m in pack.mismatches):
                queue.appendleft(first)
        packs.append(pack)
    return packs
