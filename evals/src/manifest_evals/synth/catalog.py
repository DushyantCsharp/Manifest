# ruff: noqa: E501 - one product per line reads better than wrapped rows
"""Products the synthetic packs ship, each with its true HS 2022 subheading.

The HS codes are ground truth for classification evals, so a test checks every one exists in
the vendored nomenclature. Prices are USD per unit; weights are kg per unit.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Product:
    description: str
    hs_code: str
    unit: str
    unit_price_usd: tuple[Decimal, Decimal]
    net_kg_per_unit: tuple[Decimal, Decimal]
    quantity: tuple[int, int]
    units_per_package: int
    origins: tuple[str, ...]  # ISO 3166-1 alpha-2


def _p(
    description: str,
    hs_code: str,
    unit: str,
    price: tuple[str, str],
    net_kg: tuple[str, str],
    quantity: tuple[int, int],
    per_package: int,
    *origins: str,
) -> Product:
    return Product(
        description,
        hs_code,
        unit,
        (Decimal(price[0]), Decimal(price[1])),
        (Decimal(net_kg[0]), Decimal(net_kg[1])),
        quantity,
        per_package,
        origins,
    )


# fmt: off
CATALOG: tuple[Product, ...] = (
    # electronics and electrical
    _p("Laptop computer 14in, 16GB RAM", "847130", "pcs", ("420", "1350"), ("1.20", "2.10"), (20, 400), 5, "CN", "TW", "VN"),
    _p("Smartphone 6.1in, 128GB", "851713", "pcs", ("110", "640"), ("0.17", "0.23"), (100, 2000), 40, "CN", "VN", "IN"),
    _p("LED bulb E27 9W warm white", "853952", "pcs", ("0.60", "2.40"), ("0.04", "0.07"), (2000, 40000), 200, "CN"),
    _p("Lithium-ion battery pack 48V 20Ah", "850760", "pcs", ("95", "310"), ("5.5", "9.0"), (20, 300), 4, "CN", "KR"),
    _p("Electric kettle 1.7L stainless", "851679", "pcs", ("7.50", "19.00"), ("0.9", "1.3"), (300, 3000), 12, "CN"),
    _p("Hair dryer 2200W ionic", "851631", "pcs", ("6.00", "21.00"), ("0.5", "0.8"), (300, 4000), 20, "CN"),
    _p("AC induction motor 0.55kW 3-phase", "850151", "pcs", ("38", "95"), ("6.5", "9.5"), (20, 400), 2, "CN", "DE", "IT"),
    _p("Solar PV module 410W monocrystalline", "854143", "pcs", ("62", "118"), ("20.5", "22.5"), (60, 620), 31, "CN", "VN", "MY"),
    _p("Split air conditioner 12000 BTU", "841510", "pcs", ("160", "390"), ("34", "42"), (20, 200), 1, "CN", "TH"),
    _p("LED television 55in 4K", "852872", "pcs", ("180", "460"), ("14", "18"), (20, 300), 1, "CN", "MX", "PL"),
    _p("Deep groove ball bearing 6205-2RS", "848210", "pcs", ("0.80", "4.50"), ("0.12", "0.13"), (1000, 20000), 100, "CN", "DE", "JP"),
    _p("Centrifugal water pump 1.1kW", "841370", "pcs", ("55", "160"), ("11", "16"), (20, 300), 1, "IT", "DE", "CN"),
    # apparel and footwear
    _p("Men's crew-neck T-shirt, 100% cotton jersey", "610910", "pcs", ("1.80", "6.50"), ("0.15", "0.22"), (1000, 20000), 50, "BD", "VN", "IN"),
    _p("Women's cotton denim trousers", "620462", "pcs", ("5.50", "14.00"), ("0.50", "0.70"), (500, 10000), 30, "BD", "PK", "TR"),
    _p("Men's woven cotton shirt, long sleeve", "620520", "pcs", ("4.50", "12.00"), ("0.25", "0.35"), (500, 8000), 40, "IN", "BD", "VN"),
    _p("Cotton knitted pullover", "611020", "pcs", ("6.00", "18.00"), ("0.35", "0.55"), (300, 6000), 24, "CN", "BD", "KH"),
    _p("Running shoes, rubber sole, textile upper", "640411", "pairs", ("9.00", "28.00"), ("0.55", "0.80"), (300, 6000), 12, "VN", "ID", "CN"),
    _p("Rubber flip-flops, thong plugged into the sole", "640220", "pairs", ("0.70", "2.20"), ("0.18", "0.25"), (1000, 20000), 60, "CN", "VN", "BR"),
    _p("Leather handbag with shoulder strap", "420221", "pcs", ("18", "85"), ("0.7", "1.2"), (100, 2000), 10, "IT", "IN", "CN"),
    _p("Terry cotton bath towel 70x140", "630260", "pcs", ("2.20", "6.50"), ("0.45", "0.60"), (500, 10000), 40, "PK", "IN", "TR"),
    # home and consumer goods
    _p("Solid wood bed frame, queen", "940350", "pcs", ("85", "240"), ("38", "55"), (20, 200), 1, "VN", "MY", "ID"),
    _p("Upholstered office chair, steel base", "940171", "pcs", ("28", "95"), ("12", "17"), (50, 600), 1, "CN", "PL"),
    _p("Porcelain dinner plate 27cm", "691110", "pcs", ("0.90", "3.80"), ("0.55", "0.70"), (1000, 12000), 24, "CN", "PT", "TH"),
    _p("Stainless cutlery set, 24 pieces", "821520", "sets", ("6.50", "22.00"), ("1.4", "1.9"), (200, 3000), 10, "CN", "DE", "IN"),
    _p("Stainless steel saucepan 20cm", "732393", "pcs", ("4.50", "16.00"), ("0.9", "1.4"), (300, 4000), 12, "CN", "IN"),
    _p("Plastic storage box 30L with lid", "392490", "pcs", ("1.40", "4.80"), ("0.9", "1.3"), (500, 6000), 10, "CN", "VN"),
    _p("Plush toy bear 30cm", "950300", "pcs", ("1.20", "5.50"), ("0.15", "0.30"), (500, 10000), 48, "CN", "VN"),
    _p("Inflatable beach ball 40cm", "950662", "pcs", ("0.25", "0.90"), ("0.06", "0.09"), (2000, 30000), 200, "CN"),
    _p("City bicycle 28in, 7-speed", "871200", "pcs", ("85", "260"), ("15", "18"), (20, 300), 1, "TW", "CN", "KH"),
    _p("Analog quartz wristwatch, steel case", "910211", "pcs", ("4.00", "45.00"), ("0.08", "0.12"), (200, 5000), 50, "CN", "CH", "JP"),
    _p("Polarized sunglasses", "900410", "pcs", ("1.50", "9.00"), ("0.03", "0.05"), (500, 10000), 100, "CN", "IT"),
    _p("Passenger car tyre 205/55R16", "401110", "pcs", ("28", "75"), ("8.4", "9.2"), (100, 1200), 1, "TH", "CN", "DE"),
    _p("Nitrile examination gloves, box of 100", "401512", "sets", ("2.10", "4.20"), ("0.42", "0.50"), (1000, 20000), 10, "MY", "TH", "VN"),
    # food and agriculture
    _p("Green Arabica coffee beans, grade 1", "090111", "kg", ("3.20", "6.80"), ("1", "1"), (5000, 19200), 60, "BR", "CO", "ET", "VN"),
    _p("Roasted coffee beans, whole", "090121", "kg", ("9.00", "18.00"), ("1", "1"), (500, 6000), 10, "IT", "DE", "BR"),
    _p("Black tea, CTC, bulk", "090240", "kg", ("2.40", "5.20"), ("1", "1"), (2000, 18000), 50, "IN", "LK", "KE"),
    _p("Extra virgin olive oil, 1L bottle", "150920", "pcs", ("5.50", "11.00"), ("0.92", "0.92"), (600, 9600), 12, "ES", "IT", "GR"),
    _p("Red wine, 0.75L bottle", "220421", "pcs", ("2.80", "14.00"), ("1.25", "1.30"), (600, 12000), 6, "FR", "IT", "ES", "CL"),
    _p("Canned skipjack tuna in oil, 185g", "160414", "pcs", ("0.55", "1.40"), ("0.20", "0.21"), (5000, 48000), 48, "TH", "EC", "PH"),
    _p("Frozen vannamei shrimp, peeled", "030617", "kg", ("6.00", "11.50"), ("1", "1"), (2000, 18000), 10, "EC", "VN", "IN"),
    _p("Long grain white rice, milled", "100630", "kg", ("0.45", "1.10"), ("1", "1"), (5000, 20000), 25, "IN", "PK", "TH", "VN"),
    _p("Cashew kernels W320", "080132", "kg", ("5.50", "9.50"), ("1", "1"), (2000, 16000), 23, "VN", "IN", "CI"),
    _p("Milk chocolate bars 100g", "180632", "pcs", ("0.60", "1.80"), ("0.10", "0.11"), (5000, 50000), 60, "BE", "CH", "DE"),
    # industrial materials
    _p("Combed cotton yarn Ne 30/1", "520523", "kg", ("2.80", "4.20"), ("1", "1"), (5000, 20000), 50, "IN", "PK", "VN"),
    _p("LDPE film-grade granules", "390110", "kg", ("1.05", "1.65"), ("1", "1"), (5000, 20000), 25, "SA", "US", "KR"),
    _p("Stainless steel sheet 304, cold-rolled 2mm", "721933", "kg", ("2.20", "3.60"), ("1", "1"), (5000, 20000), 1000, "CN", "IN", "KR"),
    _p("Aluminium household foil 12 micron", "760711", "kg", ("3.10", "4.60"), ("1", "1"), (2000, 18000), 20, "CN", "TR"),
    _p("A4 copy paper 80gsm, ream", "480256", "pcs", ("2.10", "3.40"), ("2.49", "2.51"), (2000, 9000), 5, "ID", "BR", "PT"),
    _p("Corrugated shipping carton, 3-ply", "481910", "pcs", ("0.25", "1.10"), ("0.30", "0.60"), (2000, 30000), 50, "CN", "VN"),
)
# fmt: on
