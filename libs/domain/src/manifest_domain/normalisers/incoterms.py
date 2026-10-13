"""Delivery terms to Incoterms 2020: ``"FOB Ningbo"``, ``"CIF Rotterdam (Incoterms 2020)"``,
``"Incoterms® 2020: DAP Leeds"``, ``"Free on Board Ningbo"``, and 2010's ``DAT`` (now ``DPU``).
"""

import re
from dataclasses import dataclass

from manifest_domain.normalisers.numbers import NormalisationError

INCOTERMS_2020 = {
    "EXW": "Ex Works",
    "FCA": "Free Carrier",
    "CPT": "Carriage Paid To",
    "CIP": "Carriage and Insurance Paid To",
    "DAP": "Delivered at Place",
    "DPU": "Delivered at Place Unloaded",
    "DDP": "Delivered Duty Paid",
    "FAS": "Free Alongside Ship",
    "FOB": "Free on Board",
    "CFR": "Cost and Freight",
    "CIF": "Cost Insurance and Freight",
}
SUPERSEDED = {"DAT": "DPU", "C&F": "CFR", "CNF": "CFR", "CANDF": "CFR"}

_NOISE = re.compile(r"\(?\s*incoterms?\s*®?\s*(?:20\d\d)?\s*\)?\s*:?", re.IGNORECASE)
# Spelled-out names, longest first ("Delivered at Place Unloaded" before "... at Place"),
# with any commas or spacing between the words: "Cost, Insurance and Freight".
_NAMES = [
    (code, re.compile(r"[\s,]+".join(name.split()) + r"\b(.*)", re.IGNORECASE))
    for code, name in sorted(INCOTERMS_2020.items(), key=lambda item: -len(item[1]))
]
_CODE = re.compile(r"(C\s*(?:&|AND)\s*F|[A-Z]{3})(?=$|[\s,;:/-])[\s,;:/-]*(.*)", re.IGNORECASE)


@dataclass(frozen=True)
class Incoterm:
    code: str
    place: str | None


def _place(rest: str) -> str | None:
    return " ".join(rest.strip(" ,;:-/").split()) or None


def normalise_incoterm(text: str) -> Incoterm:
    cleaned = " ".join(_NOISE.sub(" ", text).split())
    for code, name in _NAMES:
        if spelled := name.match(cleaned):
            return Incoterm(code, _place(spelled.group(1)))
    match = _CODE.fullmatch(cleaned)
    if match is None:
        raise NormalisationError(f"{text!r} is not an Incoterm")
    code = re.sub(r"\s", "", match.group(1)).upper()
    code = SUPERSEDED.get(code, code)
    if code not in INCOTERMS_2020:
        raise NormalisationError(f"{code!r} is not an Incoterms 2020 rule")
    return Incoterm(code, _place(match.group(2)))
