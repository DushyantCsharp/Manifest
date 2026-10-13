"""The evidence post-check: is the quoted span on the page, and does it show the value?

A value is ``verified`` when:

1. its evidence span occurs in the page text, character for character except whitespace (runs
   of any Unicode whitespace, line breaks included, count as one space: text layers and OCR
   break lines and pad cells arbitrarily, and narrow no-break spaces group digits); and
2. the span shows the value: as one contiguous piece for numbers, dates and codes, or, for
   free text, as its words in order (a table cell can wrap around a neighbouring column in
   the text layer, so "denim / 1,200 pcs / trousers" still shows "denim trousers"), ignoring
   punctuation around words ("529203 Chennai, India" shows "Chennai").

Anything else is ``failed``, and the field's confidence becomes 0 whatever the model said.
Case and punctuation must match: the span is meant to be copied, not paraphrased.
"""

from manifest_domain.extraction import Evidence

TEXT_KINDS = frozenset({"text"})


def collapse(text: str) -> str:
    return " ".join(text.split())


_EDGES = ".,;:()[]{}\"'"


def _words(text: str) -> list[str]:
    """Words without the punctuation around them ("Chennai," shows "Chennai")."""
    return [word.strip(_EDGES) or word for word in text.split()]


def _in_order(words: list[str], within: list[str]) -> bool:
    remaining = iter(within)
    return all(word in remaining for word in words)


_GROUPING = frozenset(".,'\u2019")


def _joins(text: str, edge: int, outward: int) -> bool:
    """Whether the character next to ``edge`` (in direction ``outward``) continues a word or
    number: a letter or digit, or a separator with a digit beyond it ("1.|234,56")."""
    neighbour = edge + outward
    if not 0 <= neighbour < len(text):
        return False
    if text[neighbour].isalnum():
        return True
    beyond = neighbour + outward
    return text[neighbour] in _GROUPING and 0 <= beyond < len(text) and text[beyond].isdigit()


def bounded(value: str, within: str) -> bool:
    """``value`` occurs in ``within`` and isn't part of a longer number or word ("12" in
    "120" or "234,56" in "1.234,56" don't count)."""
    start = within.find(value)
    while start != -1:
        end = start + len(value) - 1
        if not (value[0].isalnum() and _joins(within, start, -1)) and not (
            value[-1].isalnum() and _joins(within, end, 1)
        ):
            return True
        start = within.find(value, start + 1)
    return False


def check(page_text: str, value: str, evidence: str, kind: str) -> Evidence:
    span = collapse(evidence)
    shown = collapse(value)
    if not span or not shown or span not in collapse(page_text):
        return "failed"
    if kind in TEXT_KINDS:
        return "verified" if _in_order(_words(shown), _words(span)) else "failed"
    return "verified" if bounded(shown, span) else "failed"
