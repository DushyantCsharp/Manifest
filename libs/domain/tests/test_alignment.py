"""Hungarian line-item alignment: optimal against brute force, and sensible on real lines."""

import itertools
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from manifest_domain.alignment import Alignment, align, assign, line_distance

costs = st.decimals(min_value=0, max_value=10, places=2, allow_nan=False, allow_infinity=False)


@st.composite
def matrices(draw: st.DrawFn, max_side: int = 5) -> list[list[Decimal]]:
    rows = draw(st.integers(1, max_side))
    columns = draw(st.integers(rows, max_side))
    return [draw(st.lists(costs, min_size=columns, max_size=columns)) for _ in range(rows)]


def cheapest_assignment(cost: list[list[Decimal]]) -> Decimal:
    columns = range(len(cost[0]))
    return min(
        sum((cost[i][j] for i, j in enumerate(chosen)), Decimal(0))
        for chosen in itertools.permutations(columns, len(cost))
    )


def cheapest_alignment(distances: list[list[Decimal]], m: int, unmatched: Decimal) -> Decimal:
    """Every partial one-to-one matching, tried exhaustively."""

    def best(i: int, taken: frozenset[int]) -> Decimal:
        if i == len(distances):
            return unmatched * (m - len(taken))
        options = [unmatched + best(i + 1, taken)]
        options += [distances[i][j] + best(i + 1, taken | {j}) for j in range(m) if j not in taken]
        return min(options)

    return best(0, frozenset())


@given(matrices())
def test_assignment_is_optimal(cost: list[list[Decimal]]) -> None:
    assigned = assign(cost)
    assert len(set(assigned)) == len(cost)  # one column per row, no column twice
    total = sum((cost[i][j] for i, j in enumerate(assigned)), Decimal(0))
    assert total == cheapest_assignment(cost)


def test_assignment_needs_enough_columns() -> None:
    with pytest.raises(ValueError, match="columns"):
        assign([[Decimal(1)], [Decimal(2)]])
    assert assign([]) == []


@st.composite
def alignments(draw: st.DrawFn) -> tuple[list[list[Decimal]], int, Decimal]:
    n, m = draw(st.integers(0, 4)), draw(st.integers(0, 4))
    distances = [draw(st.lists(costs, min_size=m, max_size=m)) for _ in range(n)]
    unmatched = draw(st.decimals(min_value=0, max_value=6, places=2, allow_nan=False))
    return distances, m, unmatched


def run(distances: list[list[Decimal]], m: int, unmatched: Decimal) -> Alignment:
    return align(
        list(range(len(distances))),
        list(range(m)),
        lambda i, j: distances[i][j],
        unmatched=unmatched,
    )


@given(alignments())
def test_alignment_is_optimal_and_accounts_for_every_line(
    case: tuple[list[list[Decimal]], int, Decimal],
) -> None:
    distances, m, unmatched = case
    result = run(distances, m, unmatched)
    left = [i for i, _ in result.pairs] + list(result.left_only)
    right = [j for _, j in result.pairs] + list(result.right_only)
    assert sorted(left) == list(range(len(distances)))
    assert sorted(right) == list(range(m))
    recomputed = sum((distances[i][j] for i, j in result.pairs), Decimal(0))
    recomputed += unmatched * (len(result.left_only) + len(result.right_only))
    assert result.cost == recomputed
    assert result.cost == cheapest_alignment(distances, m, unmatched)


@given(alignments())
def test_a_pair_never_costs_more_than_leaving_both_unpaired(
    case: tuple[list[list[Decimal]], int, Decimal],
) -> None:
    distances, m, unmatched = case
    result = run(distances, m, unmatched)
    assert all(distances[i][j] <= 2 * unmatched for i, j in result.pairs)


@given(alignments(), st.randoms())
def test_reordering_either_side_changes_nothing_but_indices(
    case: tuple[list[list[Decimal]], int, Decimal], random: object
) -> None:
    distances, m, unmatched = case
    order = list(range(m))
    random.shuffle(order)  # type: ignore[attr-defined]
    shuffled = [[row[j] for j in order] for row in distances]
    assert run(shuffled, m, unmatched).cost == run(distances, m, unmatched).cost


# ---------------------------------------------------------------- real line items

INVOICE = [
    {"description": "LED television 55in 4K", "quantity": "116"},
    {"description": "Leather handbag with shoulder strap", "quantity": "602"},
    {"description": "Laptop computer 14in, 16GB RAM", "quantity": "125"},
    {"description": "Rubber flip-flops", "quantity": "3834"},
]


def test_packing_list_in_another_order_with_one_line_missing_and_a_quantity_off() -> None:
    packing = [
        {"description": "Rubber flip-flops", "quantity": "3834"},
        {"description": "LED TELEVISION 55IN 4K", "quantity": "115"},  # short-shipped
        {"description": "Laptop computer 14in 16GB RAM", "quantity": "125.0"},
    ]
    result = align(INVOICE, packing, line_distance)
    assert result.pairs == ((0, 1), (2, 2), (3, 0))
    assert result.left_only == (1,)  # the handbags are missing from the packing list
    assert result.right_only == ()


def test_unrelated_goods_with_the_same_quantity_stay_unpaired() -> None:
    other = [{"description": "Stainless steel saucepan 20cm", "quantity": "116"}]
    result = align(INVOICE[:1], other, line_distance)
    assert result.pairs == ()
    assert (result.left_only, result.right_only) == ((0,), (0,))


@given(st.text(max_size=40), st.sampled_from(["1", "2.5", None, "lots"]))
def test_line_distance_bounds(description: str, quantity: str | None) -> None:
    line = {"description": description, "quantity": quantity}
    other = {"description": "Rubber flip-flops", "quantity": "3834"}
    assert Decimal(0) <= line_distance(line, other) <= Decimal(1)
    assert line_distance(line, other) == line_distance(other, line)
    if description.strip() and any(c.isalnum() for c in description) and quantity == "2.5":
        assert line_distance(line, dict(line, quantity="2.50")) == 0
