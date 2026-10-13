"""Reviewer GraphQL API: the review queue and field-level review mutations.

Expected failures (unknown id, task already approved, bad value) come back as ``userErrors``
on the payload. GraphQL errors are reserved for malformed requests and server faults.
"""

import re
from typing import Annotated
from uuid import UUID

import strawberry
from strawberry.fastapi import BaseContext, GraphQLRouter

from manifest_api.errors import ConflictError, InvalidValueError, NotFoundError
from manifest_api.graphql.types import (
    ApproveFieldPayload,
    ApproveTaskPayload,
    CorrectFieldPayload,
    DateValue,
    FieldValueInput,
    FieldValueType,
    MoneyValue,
    PageInfo,
    QuantityValue,
    ReviewTask,
    ReviewTaskConnection,
    ReviewTaskEdge,
    ReviewTaskFilter,
    TextValue,
    UserError,
    UserErrorCode,
)
from manifest_api.pagination import ReviewTaskCursor
from manifest_api.ports import PortsDep, ReviewService

MAX_PAGE_SIZE = 100
MAX_REASON_LENGTH = 2000
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")


class Context(BaseContext):
    def __init__(self, review: ReviewService) -> None:
        super().__init__()
        self.review = review


async def get_context(ports: PortsDep) -> Context:
    return Context(review=ports.review)


Info = strawberry.Info[Context, None]


class _BadArgumentError(Exception):
    def __init__(self, path: list[str], message: str) -> None:
        super().__init__(message)
        self.path = path


def _member[T](value: T | None) -> T | None:
    """Omitted oneOf members arrive as UNSET; treat them as None."""
    return None if value is strawberry.UNSET else value


def _parse_value(value: FieldValueInput) -> FieldValueType:
    text, money, quantity, date = (
        _member(value.text),
        _member(value.money),
        _member(value.quantity),
        _member(value.date),
    )
    if text is not None:
        if not text.strip():
            raise _BadArgumentError(["value", "text"], "must not be blank")
        return TextValue(text=text.strip())
    if money is not None:
        if not money.amount.is_finite():
            raise _BadArgumentError(["value", "money", "amount"], "must be a finite number")
        if not _CURRENCY_CODE.fullmatch(money.currency):
            raise _BadArgumentError(
                ["value", "money", "currency"], "must be an ISO 4217 code such as USD"
            )
        return MoneyValue(amount=money.amount, currency=money.currency)
    if quantity is not None:
        if not quantity.amount.is_finite():
            raise _BadArgumentError(["value", "quantity", "amount"], "must be a finite number")
        if not quantity.unit.strip():
            raise _BadArgumentError(["value", "quantity", "unit"], "must not be blank")
        return QuantityValue(amount=quantity.amount, unit=quantity.unit.strip())
    if date is not None:
        return DateValue(date=date)
    raise _BadArgumentError(["value"], "exactly one member must be set")


def _parse_reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise _BadArgumentError(["reason"], "must not be blank")
    if len(reason) > MAX_REASON_LENGTH:
        raise _BadArgumentError(["reason"], f"must be at most {MAX_REASON_LENGTH} characters")
    return reason


def _user_error(exc: Exception, id_argument: str) -> UserError:
    if isinstance(exc, NotFoundError):
        return UserError(code=UserErrorCode.NOT_FOUND, message=str(exc), field=[id_argument])
    if isinstance(exc, _BadArgumentError):
        return UserError(code=UserErrorCode.INVALID_VALUE, message=str(exc), field=exc.path)
    if isinstance(exc, InvalidValueError):
        return UserError(code=UserErrorCode.INVALID_VALUE, message=str(exc), field=["value"])
    return UserError(code=UserErrorCode.CONFLICT, message=str(exc), field=None)


def _cursor(task: ReviewTask) -> str:
    return ReviewTaskCursor(int(task.priority), task.created_at, UUID(str(task.id))).encode()


@strawberry.type
class Query:
    @strawberry.field(description="Liveness probe.")
    def health(self) -> str:
        return "ok"

    @strawberry.field(description="The review queue: most urgent first, then oldest first.")
    async def review_tasks(
        self,
        info: Info,
        filter_: Annotated[ReviewTaskFilter | None, strawberry.argument(name="filter")] = None,
        first: Annotated[int, strawberry.argument(description="Page size, 1-100.")] = 25,
        after: Annotated[
            str | None, strawberry.argument(description="`endCursor` of the previous page.")
        ] = None,
    ) -> ReviewTaskConnection:
        if not 1 <= first <= MAX_PAGE_SIZE:
            raise ValueError(f"first must be between 1 and {MAX_PAGE_SIZE}")
        rows = await info.context.review.list_tasks(
            status=filter_.status if filter_ else None,
            priority=filter_.priority if filter_ else None,
            first=first + 1,  # one extra row says whether there is a next page
            after=ReviewTaskCursor.decode(after) if after is not None else None,
        )
        edges = [ReviewTaskEdge(cursor=_cursor(task), node=task) for task in rows[:first]]
        return ReviewTaskConnection(
            edges=edges,
            page_info=PageInfo(
                has_next_page=len(rows) > first,
                has_previous_page=False,
                start_cursor=edges[0].cursor if edges else None,
                end_cursor=edges[-1].cursor if edges else None,
            ),
        )

    @strawberry.field
    async def review_task(self, info: Info, id: strawberry.ID) -> ReviewTask | None:  # noqa: A002
        return await info.context.review.get_task(str(id))


@strawberry.type
class Mutation:
    @strawberry.mutation(description="Accept an extracted value as correct.")
    async def approve_field(self, info: Info, field_id: strawberry.ID) -> ApproveFieldPayload:
        try:
            field = await info.context.review.approve_field(str(field_id))
        except (NotFoundError, ConflictError) as exc:
            return ApproveFieldPayload(field=None, user_errors=[_user_error(exc, "fieldId")])
        return ApproveFieldPayload(field=field, user_errors=[])

    @strawberry.mutation(
        description="Replace a value. The reason is stored with the correction and feeds evals."
    )
    async def correct_field(
        self, info: Info, field_id: strawberry.ID, value: FieldValueInput, reason: str
    ) -> CorrectFieldPayload:
        try:
            field = await info.context.review.correct_field(
                str(field_id), _parse_value(value), _parse_reason(reason)
            )
        except (_BadArgumentError, NotFoundError, ConflictError, InvalidValueError) as exc:
            return CorrectFieldPayload(field=None, user_errors=[_user_error(exc, "fieldId")])
        return CorrectFieldPayload(field=field, user_errors=[])

    @strawberry.mutation(description="Approve the whole task, closing it.")
    async def approve_task(self, info: Info, task_id: strawberry.ID) -> ApproveTaskPayload:
        try:
            task = await info.context.review.approve_task(str(task_id))
        except (NotFoundError, ConflictError) as exc:
            return ApproveTaskPayload(task=None, user_errors=[_user_error(exc, "taskId")])
        return ApproveTaskPayload(task=task, user_errors=[])


schema = strawberry.Schema(query=Query, mutation=Mutation)


def build_graphql_router() -> GraphQLRouter[Context, None]:
    return GraphQLRouter(schema, context_getter=get_context)
