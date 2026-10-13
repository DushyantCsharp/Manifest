"""Optimal one-to-one alignment of line items between two documents (Hungarian algorithm).

An invoice and its packing list describe the same goods, but lines can be reordered, missing or
extra. ``align`` pairs lines so the total distance is minimal. Pairing two lines must beat
leaving both unpaired at ``unmatched`` each, so a line missing from one document comes back
unpaired instead of being forced onto the wrong partner. Costs are Decimal, so ties and sums
are exact.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

UNMATCHED = Decimal("0.35")  # lines pair only when their distance is below 0.7
DESCRIPTION_WEIGHT = Decimal("0.7")
QUANTITY_WEIGHT = Decimal("0.3")


def assign(cost: Sequence[Sequence[Decimal]]) -> list[int]:
    """Minimum-cost assignment for an n x m matrix with n <= m: the column for each row.

    Kuhn-Munkres with row/column potentials and shortest augmenting paths, O(n^2 m).
    """
    rows = len(cost)
    columns = len(cost[0]) if rows else 0
    if rows > columns:
        raise ValueError(f"{rows} rows need at least as many columns, got {columns}")
    infinity = Decimal("Infinity")
    u = [Decimal(0)] * (rows + 1)  # row potentials (1-based)
    v = [Decimal(0)] * (columns + 1)  # column potentials; column 0 is the augmenting root
    owner = [0] * (columns + 1)  # owner[j]: row assigned to column j, 0 when free
    way = [0] * (columns + 1)  # previous column on the shortest path to j
    for row in range(1, rows + 1):
        owner[0] = row
        column = 0
        reach = [infinity] * (columns + 1)
        used = [False] * (columns + 1)
        while owner[column]:
            used[column] = True
            current, delta, nearest = owner[column], infinity, 0
            for j in range(1, columns + 1):
                if used[j]:
                    continue
                reduced = cost[current - 1][j - 1] - u[current] - v[j]
                if reduced < reach[j]:
                    reach[j], way[j] = reduced, column
                if reach[j] < delta:
                    delta, nearest = reach[j], j
            for j in range(columns + 1):
                if used[j]:
                    u[owner[j]] += delta
                    v[j] -= delta
                else:
                    reach[j] -= delta
            column = nearest
        while column:  # flip the augmenting path
            previous = way[column]
            owner[column] = owner[previous]
            column = previous
    assigned = [0] * rows
    for j in range(1, columns + 1):
        if owner[j]:
            assigned[owner[j] - 1] = j - 1
    return assigned


@dataclass(frozen=True)
class Alignment:
    pairs: tuple[tuple[int, int], ...]  # (left index, right index), in left order
    left_only: tuple[int, ...]
    right_only: tuple[int, ...]
    cost: Decimal  # paired distances plus ``unmatched`` per unpaired line


def align[L, R](
    left: Sequence[L],
    right: Sequence[R],
    distance: Callable[[L, R], Decimal],
    *,
    unmatched: Decimal = UNMATCHED,
) -> Alignment:
    """Pair ``left`` and ``right`` one-to-one at minimum total cost, leaving lines unpaired.

    Solved as a square assignment over both lists plus one "unpaired" slot per line: left line
    ``i`` either takes a right line or its own slot, right line ``j`` likewise, and unused
    slots absorb each other at no cost.
    """
    if unmatched < 0:
        raise ValueError("unmatched must not be negative")
    n, m = len(left), len(right)
    distances = [[distance(a, b) for b in right] for a in left]
    forbidden = (n + m) * unmatched + 1  # dearer than leaving everything unpaired
    size = n + m
    matrix = [[forbidden] * size for _ in range(size)]
    for i in range(n):
        matrix[i][:m] = distances[i]
        matrix[i][m + i] = unmatched
    for j in range(m):
        matrix[n + j][j] = unmatched
        matrix[n + j][m:] = [Decimal(0)] * n
    assigned = assign(matrix)
    pairs = tuple((i, assigned[i]) for i in range(n) if assigned[i] < m)
    paired_right = {j for _, j in pairs}
    left_only = tuple(i for i in range(n) if assigned[i] >= m)
    right_only = tuple(j for j in range(m) if j not in paired_right)
    total = sum((distances[i][j] for i, j in pairs), Decimal(0))
    total += unmatched * (len(left_only) + len(right_only))
    return Alignment(pairs, left_only, right_only, total)


def _tokens(text: str | None) -> frozenset[str]:
    return frozenset("".join(c if c.isalnum() else " " for c in (text or "").lower()).split())


def _same_quantity(a: str | None, b: str | None) -> Decimal:
    if a is None or b is None:
        return Decimal("0.5")
    try:
        return Decimal(0) if Decimal(a) == Decimal(b) else Decimal(1)
    except InvalidOperation:
        return Decimal(0) if a == b else Decimal(1)


def line_distance(a: Mapping[str, str | None], b: Mapping[str, str | None]) -> Decimal:
    """0 (same goods) to 1 (nothing in common): description token overlap, then quantity.

    A quantity that differs alone keeps lines paired (that's a discrepancy to report, not a
    different line); a different description alone doesn't.
    """
    left, right = _tokens(a.get("description")), _tokens(b.get("description"))
    union = left | right
    overlap = Decimal(len(left & right)) / Decimal(len(union)) if union else Decimal(0)
    quantity = _same_quantity(a.get("quantity"), b.get("quantity"))
    return DESCRIPTION_WEIGHT * (1 - overlap) + QUANTITY_WEIGHT * quantity
