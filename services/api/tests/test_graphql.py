"""The generated GraphQL client (clients/graphql) against the reviewer schema."""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from manifest_review_client import (
    Client,
    FieldValueInput,
    GraphQLClientGraphQLMultiError,
    MoneyInput,
    ReviewTaskFilter,
)
from manifest_review_client.enums import (
    FieldStatus,
    ReviewPriority,
    ReviewTaskStatus,
    UserErrorCode,
)


@pytest.fixture
async def gql(app: FastAPI) -> AsyncIterator[Client]:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with Client(url="http://test/graphql", http_client=http) as client:
        yield client


def field_id(fakes, key: str, task_index: int = 0) -> str:
    task = fakes.review.queue()[task_index]
    return str(fakes.review.find_field(key, task).id)


async def test_queue_pages_in_priority_then_age_order(gql: Client, fakes) -> None:
    seen, after = [], None
    while True:
        page = (await gql.review_tasks(first=2, after=after)).review_tasks
        seen += [edge.node.id for edge in page.edges]
        if not page.page_info.has_next_page:
            break
        after = page.page_info.end_cursor
    assert seen == [str(task.id) for task in fakes.review.queue()]


async def test_filter_by_status_and_priority(gql: Client) -> None:
    result = await gql.review_tasks(
        filter_=ReviewTaskFilter(status=[ReviewTaskStatus.OPEN], priority=[ReviewPriority.URGENT])
    )
    nodes = [edge.node for edge in result.review_tasks.edges]
    assert len(nodes) == 2
    assert {(n.status, n.priority) for n in nodes} == {
        (ReviewTaskStatus.OPEN, ReviewPriority.URGENT)
    }
    assert nodes[0].created_at < nodes[1].created_at


async def test_task_carries_evidence_for_each_value(gql: Client, fakes) -> None:
    task_id = str(fakes.review.queue()[0].id)
    task = (await gql.review_task(id=task_id)).review_task
    assert task is not None

    [extraction] = task.extractions
    assert extraction.schema_version == 1
    fields = {f.key: f for f in extraction.fields}
    total = fields["invoice_total"]
    assert total.value is not None
    assert total.value.typename__ == "MoneyValue"
    assert (total.value.amount, total.value.currency) == (Decimal("1250.00"), "USD")
    assert total.confidence == pytest.approx(0.62)
    assert total.source_page == 1
    assert total.bbox is not None
    assert (total.bbox.left, total.bbox.bottom) == (0.1, 0.25)
    assert fields["incoterm"].value is None
    assert fields["incoterm"].status == FieldStatus.MISSING

    suggestion = extraction.line_items[0].hs_suggestion
    assert suggestion is not None
    assert suggestion.code == "847130"
    assert [c.code for c in suggestion.retrieved_candidates] == ["847130", "847141"]

    [discrepancy] = task.discrepancies
    assert [o.source_page for o in discrepancy.observations] == [1, 2]


async def test_correct_field_keeps_money_and_history(gql: Client, fakes) -> None:
    value = FieldValueInput(money=MoneyInput(amount=Decimal("1300.10"), currency="EUR"))
    result = (
        await gql.correct_field(
            field_id=field_id(fakes, "invoice_total"), value=value, reason="Typo on invoice"
        )
    ).correct_field

    assert result.user_errors == []
    assert result.field is not None
    assert result.field.status == FieldStatus.CORRECTED
    assert result.field.value is not None
    assert result.field.value.typename__ == "MoneyValue"
    assert (result.field.value.amount, result.field.value.currency) == (Decimal("1300.10"), "EUR")
    correction = result.field.correction
    assert correction is not None
    assert correction.reason == "Typo on invoice"
    assert correction.previous_value is not None
    assert correction.previous_value.typename__ == "MoneyValue"
    assert correction.previous_value.amount == Decimal("1250.00")
    assert fakes.review.queue()[0].status.name == "IN_PROGRESS"


async def test_correct_date_field(gql: Client, fakes) -> None:
    result = (
        await gql.correct_field(
            field_id=field_id(fakes, "invoice_date"),
            value=FieldValueInput(date=date(2026, 9, 29)),
            reason="Date misread",
        )
    ).correct_field
    assert result.user_errors == []
    assert result.field is not None
    assert result.field.value is not None
    assert result.field.value.typename__ == "DateValue"
    assert result.field.value.date == date(2026, 9, 29)


async def test_bad_currency_is_a_user_error(gql: Client, fakes) -> None:
    value = FieldValueInput(money=MoneyInput(amount=Decimal("1"), currency="usd"))
    result = (
        await gql.correct_field(field_id=field_id(fakes, "invoice_total"), value=value, reason="x")
    ).correct_field
    assert result.field is None
    [error] = result.user_errors
    assert error.code == UserErrorCode.INVALID_VALUE
    assert error.field == ["value", "money", "currency"]


async def test_wrong_value_type_is_a_user_error(gql: Client, fakes) -> None:
    result = (
        await gql.correct_field(
            field_id=field_id(fakes, "invoice_total"),
            value=FieldValueInput(text="twelve hundred"),
            reason="x",
        )
    ).correct_field
    [error] = result.user_errors
    assert error.code == UserErrorCode.INVALID_VALUE
    assert error.field == ["value"]


async def test_blank_reason_is_a_user_error(gql: Client, fakes) -> None:
    result = (
        await gql.correct_field(
            field_id=field_id(fakes, "invoice_number"),
            value=FieldValueInput(text="INV-1002"),
            reason="   ",
        )
    ).correct_field
    assert [e.field for e in result.user_errors] == [["reason"]]


async def test_value_input_must_set_exactly_one_member(gql: Client, fakes) -> None:
    both = FieldValueInput(text="x", money=MoneyInput(amount=Decimal("1"), currency="USD"))
    with pytest.raises(GraphQLClientGraphQLMultiError):
        await gql.correct_field(field_id=field_id(fakes, "invoice_total"), value=both, reason="x")


async def test_approve_field_then_task_then_further_edits_conflict(gql: Client, fakes) -> None:
    task = fakes.review.queue()[0]
    approved = (await gql.approve_field(field_id=field_id(fakes, "invoice_number"))).approve_field
    assert approved.field is not None
    assert approved.field.status == FieldStatus.APPROVED

    closed = (await gql.approve_task(task_id=str(task.id))).approve_task
    assert closed.task is not None
    assert closed.task.status == ReviewTaskStatus.APPROVED

    again = (await gql.approve_field(field_id=field_id(fakes, "gross_weight"))).approve_field
    assert [e.code for e in again.user_errors] == [UserErrorCode.CONFLICT]


async def test_unknown_ids_are_user_errors(gql: Client) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    field = (await gql.approve_field(field_id=missing)).approve_field
    assert [(e.code, e.field) for e in field.user_errors] == [
        (UserErrorCode.NOT_FOUND, ["fieldId"])
    ]
    task = (await gql.approve_task(task_id=missing)).approve_task
    assert [e.code for e in task.user_errors] == [UserErrorCode.NOT_FOUND]


@pytest.mark.parametrize(("first", "after"), [(0, None), (101, None), (10, "not-a-cursor")])
async def test_bad_pagination_arguments_are_errors(gql: Client, first: int, after: str | None):
    with pytest.raises(GraphQLClientGraphQLMultiError):
        await gql.review_tasks(first=first, after=after)
