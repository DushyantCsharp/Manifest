"""Currency mentions to ISO 4217 codes: ``"USD"``, ``"€"``, ``"US DOLLARS"``, ``"(GBP)"``.

A bare ``$`` is ambiguous (USD, CAD, AUD, ...): pass ``dollar=`` from context or it raises.
"""

import re

from manifest_domain.normalisers.numbers import AmbiguityError, NormalisationError

# Active ISO 4217 codes.
_ACTIVE = """AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN
    BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP
    GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS
    KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR
    MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF
    SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD
    TZS UAH UGX USD UYU UZS VED VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWG"""
ISO_4217 = frozenset(_ACTIVE.split())

SYMBOLS = {
    "€": "EUR",
    "£": "GBP",
    "₹": "INR",
    "₩": "KRW",
    "₫": "VND",
    "฿": "THB",
    "₺": "TRY",
    "₱": "PHP",
    "R$": "BRL",
    "US$": "USD",
    "C$": "CAD",
    "CA$": "CAD",
    "A$": "AUD",
    "AU$": "AUD",
    "HK$": "HKD",
    "S$": "SGD",
}

NAMES = {
    "US DOLLAR": "USD",
    "DOLLAR US": "USD",
    "EURO": "EUR",
    "POUND STERLING": "GBP",
    "BRITISH POUND": "GBP",
    "CANADIAN DOLLAR": "CAD",
    "AUSTRALIAN DOLLAR": "AUD",
    "JAPANESE YEN": "JPY",
    "SWISS FRANC": "CHF",
    "CHINESE YUAN": "CNY",
    "RENMINBI": "CNY",
    "INDIAN RUPEE": "INR",
}

# Names match with any word pluralised: "US DOLLARS", "POUNDS STERLING", "SWISS FRANCS".
_NAMES = [
    (re.compile(r"\b" + r"\s+".join(f"{word}S?" for word in name.split()) + r"\b"), code)
    for name, code in NAMES.items()
]
_CODE = re.compile(r"\b([A-Z]{3})\b")
# Capitalised words that are also ISO codes ("ALL PRICES IN USD"); ignored next to a real one.
WORD_CODES = frozenset({"ALL", "BOB", "CUP", "GEL", "MOP", "PEN", "SOS", "TOP", "TRY"})


def normalise_currency(text: str, *, dollar: str | None = None) -> str:
    cleaned = " ".join(text.strip().split())
    upper = cleaned.upper()
    if upper in ISO_4217:
        return upper
    if cleaned in SYMBOLS:
        return SYMBOLS[cleaned]
    for pattern, code in _NAMES:
        if pattern.search(upper):
            return code
    codes: set[str] = {code for code in _CODE.findall(cleaned) if code in ISO_4217}
    if len(codes) > 1:
        codes -= WORD_CODES
    if len(codes) == 1:
        return codes.pop()
    if cleaned == "$":
        if dollar is None:
            raise AmbiguityError("'$' is ambiguous without context")
        return normalise_currency(dollar)
    raise NormalisationError(f"{text!r} is not a currency")
