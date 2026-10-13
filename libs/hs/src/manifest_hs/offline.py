"""Offline lexical retrieval: BM25 over the nomenclature's path texts, in pure Python.

For seeding HS recordings without a database or an embedding key, and as a baseline. Ranks
subheadings by BM25 over each code's text weighted with its ancestors' (the subheading's own
text counts twice), with a light plural folding. It ranks through the same fusion as
production retrieval, so scores and ranks look alike; it never sees the right answer.
"""

import math
from collections import Counter, defaultdict

from manifest_hs.nomenclature import Nomenclature, hs2022
from manifest_hs.retrieval import TOP, Candidate, fuse, terms

K1, B = 1.2, 0.75


def stem(word: str) -> str:
    """Fold simple plurals so "shirts" finds "shirt" (not a real stemmer, nor meant to be)."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    return [stem(word) for word in terms(text)]


class Bm25Retriever:
    name = "offline-lexical"

    def __init__(self, nomenclature: Nomenclature | None = None) -> None:
        self._nomenclature = nomenclature or hs2022()
        self._documents: dict[str, Counter[str]] = {}
        self._postings: dict[str, list[str]] = defaultdict(list)
        for entry in self._nomenclature.subheadings():
            words = Counter(tokens(self._nomenclature.path(entry.code)))
            words.update(tokens(entry.description))  # the subheading's own words count twice
            self._documents[entry.code] = words
            for word in words:
                self._postings[word].append(entry.code)
        lengths = [sum(words.values()) for words in self._documents.values()]
        self._average = sum(lengths) / len(lengths)

    def _idf(self, word: str) -> float:
        n, df = len(self._documents), len(self._postings.get(word, ()))
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, limit: int = TOP) -> list[Candidate]:
        scores: dict[str, float] = defaultdict(float)
        for word in set(tokens(query)):
            idf = self._idf(word)
            for code in self._postings.get(word, ()):
                document = self._documents[code]
                frequency = document[word]
                norm = 1 - B + B * sum(document.values()) / self._average
                scores[code] += idf * frequency * (K1 + 1) / (frequency + K1 * norm)
        ranking = sorted(scores, key=lambda code: (-scores[code], code))[: limit * 3]
        fused = fuse({"lexical": ranking})[:limit]
        return [
            Candidate(code, self._nomenclature[code].description, score, ranks)
            for code, score, ranks in fused
        ]
