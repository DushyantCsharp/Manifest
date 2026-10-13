"""The HS classifier the pipeline uses: retrieval plus the chapter/heading/subheading walk.

An adapter from ``manifest_hs.walk.HsWalker`` to the worker's ``HsClassifier`` port. The walk
returns no code when the model finds no option that fits (or retrieval found nothing); that is
a content problem for this line, not a failure of the stage: the line keeps no suggestion and
the shipment goes to review as HS_UNCERTAIN.
"""

from collections.abc import Iterator, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.models import HsNote
from manifest_hs.walk import HsWalker, Note
from manifest_worker.errors import ContentError
from manifest_worker.ports import HsResult, LineToClassify


class NoSuggestionError(ContentError):
    """No HS subheading among the candidates covers the goods."""


class WalkClassifier:
    def __init__(self, walker: HsWalker) -> None:
        self._walker = walker

    def classify(self, line: LineToClassify) -> HsResult:
        if not line.description:
            raise NoSuggestionError("the line has no goods description")
        walk = self._walker.classify(line.description, line.country_of_origin)
        if walk.code is None:
            raise NoSuggestionError(f"no HS subheading fits {line.description!r}")
        candidates = [
            {"code": c.code, "description": c.description, "score": str(c.score), "ranks": c.ranks}
            for c in walk.candidates
        ]
        return HsResult(
            walk.code,
            walk.raw,
            walk.rationale or "single candidate",
            walk.model,
            candidates,
            walk.record(),
        )


def load_notes(sessions: sessionmaker[Session]) -> dict[str, Sequence[Note]]:
    """Legal notes by the code (or section) they belong to; empty until notes are vendored."""
    notes: dict[str, list[Note]] = {}
    with sessions() as session:
        for row in session.scalars(select(HsNote).order_by(HsNote.ref, HsNote.number)):
            notes.setdefault(row.ref, []).append(Note(row.scope, row.ref, row.number, row.text))
    return dict(notes)


class LazyNotes(Mapping[str, Sequence[Note]]):
    """``load_notes`` on first use, so building the runtime connects to nothing."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions
        self._notes: dict[str, Sequence[Note]] | None = None

    def _loaded(self) -> dict[str, Sequence[Note]]:
        if self._notes is None:
            self._notes = load_notes(self._sessions)
        return self._notes

    def __getitem__(self, key: str) -> Sequence[Note]:
        return self._loaded()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._loaded())

    def __len__(self) -> int:
        return len(self._loaded())
