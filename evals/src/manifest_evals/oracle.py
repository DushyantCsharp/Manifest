"""A stand-in model that answers page-extraction requests from ground truth.

It seeds cassettes for the eval corpus without an API key. Like a model, it reads which fields a
request asks for from the request's JSON schema, and answers each with the value *as printed*
(the true value formatted in the pack layout's conventions: ``1.234,56``, ``30.09.2026``,
``2,388.9 lb``) together with an evidence span copied from the page text.

On a text-layer page every printed value is found verbatim. On an OCR page (``ocr=True``) the
oracle copies what OCR actually produced: it finds the page text closest to the true value and
answers with that (``Sener Sti.`` for ``Şener Şti.``), less confidently the further it is from
the truth, or answers "not printed" when nothing is close. So scanned packs measure OCR damage
as a literal copier would suffer it; a real model may do better (or worse), which is what
recording with one (``make eval-record``) is for.

Its confidences are fixed rules, not a model's beliefs: a calibrator fitted to them describes
the oracle, which is why such calibrators are never trusted for routing. Token counts are
estimates and latency is nil. Recordings say ``recorded_with: ground-truth oracle``.
"""

import difflib
import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

from manifest_domain.evidence import bounded
from manifest_evals.synth.layouts import Layout
from manifest_evals.synth.render import COUNTRY_NAMES
from manifest_llm.gateway import JsonRequest, JsonResponse

CONFIDENCE = Decimal("0.97")  # a value found verbatim: right, but no reading is certain
MIN_SIMILARITY = 0.6  # below this, OCR lost the value: answer "not printed"
NAME = "ground-truth oracle"
_PARTIES = {"seller": "seller_name", "buyer": "buyer_name", "notify_party": "notify_party_name"}
_DATES = frozenset({"invoice_date", "date", "shipped_on_board"})


def printed_fields(truth: dict[str, Any], layout: Layout) -> dict[str, str | None]:
    """Each header field of a document's truth, as the document prints it."""
    printed: dict[str, str | None] = {}
    for key, value in truth["fields"].items():
        if key in _PARTIES:
            printed[_PARTIES[key]] = value["name"] if value else None
        elif value is None:
            printed[key] = None
        elif key in _DATES:
            printed[key] = layout.date(date.fromisoformat(value))
        elif key.endswith("_kg"):
            printed[key] = layout.weight(Decimal(value))
        elif key == "total_amount":
            printed[key] = layout.money(Decimal(value))
        else:
            printed[key] = str(value)
    if truth["fields"].get("incoterm"):
        printed["incoterm"] = f"{truth['fields']['incoterm']} {truth['fields']['incoterm_place']}"
    return printed


def printed_line(line: dict[str, Any], layout: Layout) -> dict[str, str | None]:
    printers = {
        "quantity": layout.quantity,
        "unit_price": layout.price,
        "amount": layout.money,
        "net_weight_kg": layout.weight,
        "gross_weight_kg": layout.weight,
    }
    return {
        key: (printers[key](Decimal(value)) if key in printers and value is not None else value)
        for key, value in line.items()
    }


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _blank() -> dict[str, Any]:
    return {"value": "", "evidence": "", "confidence": str(CONFIDENCE)}


def _requested(schema: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Header field names, and the line-item field names (empty when the kind has no lines)."""
    header = [name for name in schema["properties"] if name != "line_items"]
    items = schema["properties"].get("line_items")
    if items is None:
        return header, []
    ref = items["items"]["$ref"].rsplit("/", 1)[1]
    return header, list(schema["$defs"][ref]["properties"])


class _Page:
    """One page's text, searched the way a careful copier would."""

    def __init__(self, text: str, *, ocr: bool) -> None:
        self.text = _collapse(text)
        self.lines = [_collapse(line) for line in text.splitlines() if line.strip()]
        self.words = self.text.split()
        self.ocr = ocr

    def _find(self, value: str) -> str | None:
        """Evidence for ``value``: the first page line showing it as a whole token (not "69"
        in "6903"), or the value itself when it wraps across lines; None if it isn't shown."""
        line = next((line for line in self.lines if bounded(value, line)), None)
        if line is not None:
            return line
        return value if bounded(value, self.text) else None

    def _closest(self, value: str) -> tuple[str, float]:
        size = len(value.split())
        best, best_ratio = "", 0.0
        for width in sorted({max(1, size - 1), size, size + 1}):
            for start in range(len(self.words) - width + 1):
                window = " ".join(self.words[start : start + width])
                ratio = difflib.SequenceMatcher(None, window, value).ratio()
                if ratio > best_ratio:
                    best, best_ratio = window, ratio
        return best, best_ratio

    def read(self, candidates: list[str]) -> dict[str, Any]:
        """A reading of the first candidate spelling printed on the page (verbatim), or on an
        OCR page the closest text to it; "not printed" when there's nothing."""
        printed = [_collapse(c) for c in candidates if _collapse(c)]
        for value in printed:
            evidence = self._find(value)
            if evidence is not None:
                return {"value": value, "evidence": evidence, "confidence": str(CONFIDENCE)}
        if not self.ocr or not printed:
            return _blank()
        window, ratio = max((self._closest(value) for value in printed), key=lambda f: f[1])
        if ratio < MIN_SIMILARITY:
            return _blank()
        confidence = (Decimal(str(ratio)) * Decimal("0.9")).quantize(Decimal("0.01"))
        evidence = self._find(window) or window
        return {"value": window, "evidence": evidence, "confidence": str(confidence)}


def _spellings(value: str | None) -> list[str]:
    """How a layout may print ``value``: as is, or in capitals (the exporter grid)."""
    return [value, value.upper()] if value else []


def _country_spellings(code: str | None) -> list[str]:
    if not code:
        return []
    name = COUNTRY_NAMES.get(code, code)
    return [name, name.upper(), code]


class OracleProvider:
    """Answers for one pack, whose documents' truths are keyed by kind (``packing_list``)."""

    name = NAME

    def __init__(
        self, truths: dict[str, dict[str, Any]], layout: Layout, *, ocr: bool = False
    ) -> None:
        self._truths = truths
        self._layout = layout
        self._ocr = ocr

    def _line(self, truth_line: dict[str, Any], names: list[str], page: _Page) -> dict[str, Any]:
        line = printed_line(truth_line, self._layout)
        reading = {}
        for name in names:
            if name == "country_of_origin":
                reading[name] = page.read(_country_spellings(line.get(name)))
            else:
                reading[name] = page.read(_spellings(line.get(name)))
        return reading

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        found = re.search(r"^Document: (.+)$", request.prompt, re.MULTILINE)
        text = re.search(r"<<<\n(.*)\n>>>", request.prompt, re.DOTALL)
        if found is None or text is None:
            raise ValueError("not a page-extraction request")
        truth = self._truths[found.group(1).replace(" ", "_")]
        page = _Page(text.group(1), ocr=self._ocr)
        header_names, line_names = _requested(request.schema)
        header = printed_fields(truth, self._layout)
        data = {name: page.read(_spellings(header.get(name))) for name in header_names}
        if line_names:
            lines = (self._line(line, line_names, page) for line in truth["line_items"])
            # A line whose description isn't on this page belongs to another page.
            data["line_items"] = [line for line in lines if line["description"]["value"]]
        encoded = json.dumps(data)
        return JsonResponse(data, len(request.system + request.prompt) // 4, len(encoded) // 4, 0)
