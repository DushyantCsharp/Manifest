"""The five invoice layouts and the conventions each one prints in.

Every layout formats numbers, dates and weights its own way, so extraction has to normalise
(``1.234,56`` vs ``1,234.56``, ``30.09.2026`` vs ``09/30/2026``, lb vs kg). ``kg_truth`` gives
the normalised value of what a layout actually prints, which is what ground truth records.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

LB = Decimal("0.45359237")  # kg per pound, exact by definition
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")  # fmt: skip

NumberStyle = Literal["en", "de", "fr"]


def _separators(text: str, style: NumberStyle) -> str:
    if style == "en":
        return text
    grouping = "." if style == "de" else "\u202f"  # fr: narrow no-break space
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", grouping)


@dataclass(frozen=True)
class Layout:
    name: str
    template: str
    currencies: tuple[str, ...]
    seller_countries: tuple[str, ...]
    buyer_countries: tuple[str, ...]
    number_style: NumberStyle
    date_style: Literal["us", "eu", "exporter", "long", "iso"]
    weight_unit: Literal["kg", "lb"]
    invoice_number: Callable[[random.Random, date], str]
    shows_hs_code: bool
    max_lines: int

    # ------------------------------------------------------------ printing
    def number(self, value: Decimal, places: int) -> str:
        quantum = Decimal(1).scaleb(-places)
        return _separators(f"{value.quantize(quantum, ROUND_HALF_UP):,}", self.number_style)

    def money(self, value: Decimal) -> str:
        return self.number(value, 2)

    def price(self, value: Decimal) -> str:
        return self.number(value, max(2, -int(value.as_tuple().exponent)))

    def quantity(self, value: Decimal) -> str:
        return self.number(value, max(0, -int(value.normalize().as_tuple().exponent)))

    def weight(self, kg: Decimal) -> str:
        if self.weight_unit == "lb":
            return f"{self.number(kg / LB, 1)} lb"
        return f"{self.number(kg, 2)} kg"

    def date(self, value: date) -> str:
        match self.date_style:
            case "us":
                return f"{value:%m/%d/%Y}"
            case "eu":
                return f"{value:%d.%m.%Y}"
            case "exporter":
                return f"{value.day:02d}-{MONTHS[value.month - 1][:3].upper()}-{value.year}"
            case "long":
                return f"{MONTHS[value.month - 1]} {value.day}, {value.year}"
            case "iso":
                return value.isoformat()

    # ------------------------------------------------------------ ground truth
    def kg_truth(self, kg: Decimal) -> Decimal:
        """The kg value a perfect normaliser recovers from what this layout prints."""
        if self.weight_unit == "lb":
            printed_lb = (kg / LB).quantize(Decimal("0.1"), ROUND_HALF_UP)
            return (printed_lb * LB).quantize(Decimal("0.01"), ROUND_HALF_UP)
        return kg.quantize(Decimal("0.01"), ROUND_HALF_UP)


def _digits(rng: random.Random, count: int) -> str:
    return "".join(rng.choice("0123456789") for _ in range(count))


LAYOUTS: tuple[Layout, ...] = (
    Layout(
        name="us",
        template="invoice_us.html.j2",
        currencies=("USD", "CAD"),
        seller_countries=("CN", "VN", "IN", "TW", "KR", "TH"),
        buyer_countries=("US", "CA"),
        number_style="en",
        date_style="us",
        weight_unit="kg",
        invoice_number=lambda rng, d: f"INV-{d.year}-{_digits(rng, 5)}",
        shows_hs_code=False,
        max_lines=8,
    ),
    Layout(
        name="eu",
        template="invoice_eu.html.j2",
        currencies=("EUR",),
        seller_countries=("IT", "ES", "DE", "TR", "CN", "VN", "IN", "BD"),
        buyer_countries=("DE", "NL", "FR", "BE", "PL"),
        number_style="de",
        date_style="eu",
        weight_unit="kg",
        invoice_number=lambda rng, d: f"RE-{d:%y}-{_digits(rng, 4)}",
        shows_hs_code=False,
        max_lines=8,
    ),
    Layout(
        name="exporter",
        template="invoice_exporter.html.j2",
        currencies=("USD",),
        seller_countries=("CN", "VN", "BD", "PK", "IN", "TH", "MY", "ID", "KR", "TW"),
        buyer_countries=("US", "GB", "NL", "ZA", "AU"),
        number_style="en",
        date_style="exporter",
        weight_unit="kg",
        invoice_number=lambda rng, d: f"EX{d.year}{_digits(rng, 2)}-{_digits(rng, 4)}",
        shows_hs_code=True,
        max_lines=10,
    ),
    Layout(
        name="letterhead",
        template="invoice_letterhead.html.j2",
        currencies=("GBP", "USD"),
        seller_countries=("CN", "IN", "TR", "IT", "BR", "EC", "CO"),
        buyer_countries=("GB", "US"),
        number_style="en",
        date_style="long",
        weight_unit="lb",
        invoice_number=lambda rng, d: f"{d.year}/{_digits(rng, 4)}",
        shows_hs_code=False,
        max_lines=6,
    ),
    Layout(
        name="compact",
        template="invoice_compact.html.j2",
        currencies=("EUR", "USD"),
        seller_countries=("CN", "VN", "IN", "TR", "IT", "ES", "MY", "ID"),
        buyer_countries=("FR", "BE", "DE"),
        number_style="fr",
        date_style="iso",
        weight_unit="kg",
        invoice_number=lambda rng, d: f"FC-{_digits(rng, 4)}/{d:%y}",
        shows_hs_code=False,
        max_lines=12,
    ),
)

BY_NAME = {layout.name: layout for layout in LAYOUTS}
