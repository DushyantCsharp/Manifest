"""Render pack documents to PDF: Jinja2 templates -> HTML -> WeasyPrint.

WeasyPrint needs Pango at runtime (``brew install pango`` / ``apt install libpango-1.0-0
libpangoft2-1.0-0``). It's imported lazily so generation and ground truth work without it.
"""

from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from manifest_domain.lifecycle import DocumentKind
from manifest_evals.synth.layouts import Layout
from manifest_evals.synth.model import Pack
from manifest_evals.synth.truth import goods_description

TEMPLATES = Path(__file__).parent / "templates"

ENV = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html", "j2"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)

COUNTRY_NAMES = {
    "AU": "Australia", "BD": "Bangladesh", "BE": "Belgium", "BR": "Brazil", "CA": "Canada",
    "CH": "Switzerland", "CI": "Côte d'Ivoire", "CL": "Chile", "CN": "China", "CO": "Colombia",
    "DE": "Germany", "EC": "Ecuador", "ES": "Spain", "ET": "Ethiopia", "FR": "France",
    "GB": "United Kingdom", "GR": "Greece", "ID": "Indonesia", "IN": "India", "IT": "Italy",
    "JP": "Japan", "KE": "Kenya", "KH": "Cambodia", "KR": "South Korea", "LK": "Sri Lanka",
    "MX": "Mexico", "MY": "Malaysia", "NL": "Netherlands", "PH": "Philippines",
    "PK": "Pakistan", "PL": "Poland", "PT": "Portugal", "SA": "Saudi Arabia", "TH": "Thailand",
    "TR": "Turkey", "TW": "Taiwan", "US": "United States", "VN": "Vietnam", "ZA": "South Africa",
}  # fmt: skip

_CURRENCY_WORDS = {
    "USD": ("US DOLLARS", "CENTS"),
    "EUR": ("EUROS", "CENTS"),
    "GBP": ("POUNDS STERLING", "PENCE"),
    "CAD": ("CANADIAN DOLLARS", "CENTS"),
}
_ONES = ("ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE", "TEN",
         "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN", "FIFTEEN", "SIXTEEN", "SEVENTEEN",
         "EIGHTEEN", "NINETEEN")  # fmt: skip
_TENS = ("", "", "TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY", "EIGHTY", "NINETY")
_SCALES = (
    (1_000_000_000, "BILLION"),
    (1_000_000, "MILLION"),
    (1_000, "THOUSAND"),
    (100, "HUNDRED"),
)


def _words(number: int) -> str:
    for size, name in _SCALES:
        if number >= size:
            head, rest = divmod(number, size)
            return f"{_words(head)} {name}" + (f" {_words(rest)}" if rest else "")
    if number >= len(_ONES):
        tens, ones = divmod(number, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    return _ONES[number]


def amount_in_words(amount: Decimal, currency: str) -> str:
    """``SAY TOTAL US DOLLARS ONE HUNDRED AND CENTS FIVE ONLY``, as exporters print it."""
    major, minor = _CURRENCY_WORDS[currency]
    whole = int(amount)
    cents = int((amount - whole) * 100)
    text = f"{major} {_words(whole)}" + (f" AND {minor} {_words(cents)}" if cents else "")
    return f"SAY TOTAL {text} ONLY"


def render_html(pack: Pack, kind: DocumentKind, layout: Layout) -> str:
    template = layout.template if kind is DocumentKind.COMMERCIAL_INVOICE else f"{kind}.html.j2"
    view = pack.views[kind]
    return ENV.get_template(template).render(
        doc=view,
        layout=layout,
        country=COUNTRY_NAMES.get,
        words=amount_in_words,
        goods=goods_description(view),
    )


def render_pdf(pack: Pack, kind: DocumentKind, layout: Layout) -> bytes:
    from weasyprint import HTML  # optional system dependency; see the module docstring

    pdf: bytes = HTML(string=render_html(pack, kind, layout), base_url=str(TEMPLATES)).write_pdf()
    return pdf
