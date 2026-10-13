"""The HS 2022 nomenclature as a tree: chapters (2 digits), headings (4) and subheadings (6).

Read from the same vendored, checksummed file migration 0002 loads into ``hs_codes``, so the
classifier's view of the tree and the database's agree. ``path`` gives a code's text with its
ancestors': a subheading such as "Of cotton" only means something under its heading.
"""

import csv
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import manifest_db

SOURCE = Path(manifest_db.__file__).parent / "migrations/data/hs2022/harmonized-system.csv"
SHA256 = "e77ee9e1cfd15a42d1fadeb2c0618d10f6b9ed9aec0e5d66826dc7ef9d3126a5"  # as 0002 pins it
LEVEL_NAMES = {2: "Chapter", 4: "Heading", 6: "Subheading"}


@dataclass(frozen=True)
class Entry:
    code: str
    level: int
    parent: str | None
    section: str
    description: str


@dataclass(frozen=True)
class Nomenclature:
    entries: dict[str, Entry]
    children: dict[str | None, tuple[str, ...]] = field(default_factory=dict)

    def __getitem__(self, code: str) -> Entry:
        return self.entries[code]

    def __contains__(self, code: object) -> bool:
        return code in self.entries

    def subheadings(self) -> Iterator[Entry]:
        return (entry for entry in self.entries.values() if entry.level == 6)

    def ancestors(self, code: str) -> list[Entry]:
        """The code's chapter, heading and itself (as far as its level goes), top first."""
        return [self.entries[code[:n]] for n in (2, 4, 6) if n <= len(code)]

    def path(self, code: str) -> str:
        """The code's text with its ancestors', e.g. for embedding or showing a model."""
        parts = [f"{LEVEL_NAMES[e.level]} {e.code}: {e.description}" for e in self.ancestors(code)]
        return f"Section {self.entries[code].section}. " + ". ".join(parts)


def read(path: Path = SOURCE, sha256: str | None = SHA256) -> Nomenclature:
    data = path.read_bytes()
    if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"{path} is not the pinned HS 2022 file")
    entries: dict[str, Entry] = {}
    children: dict[str | None, list[str]] = {}
    for row in csv.DictReader(data.decode("utf-8").splitlines()):
        if row["hscode"] == "TOTAL":
            continue
        parent = None if row["parent"] == "TOTAL" else row["parent"]
        entry = Entry(row["hscode"], int(row["level"]), parent, row["section"], row["description"])
        entries[entry.code] = entry
        children.setdefault(parent, []).append(entry.code)
    return Nomenclature(entries, {k: tuple(v) for k, v in children.items()})


@cache
def hs2022() -> Nomenclature:
    return read()
