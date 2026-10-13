from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.shipment import Shipment
from ...models.shipment_create import ShipmentCreate
from ...types import Response


def _get_kwargs(
    *,
    body: ShipmentCreate,
    idempotency_key: str,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["Idempotency-Key"] = idempotency_key

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/shipments",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Problem | Shipment | None:
    if response.status_code == 201:
        response_201 = Shipment.from_dict(response.json())

        return response_201

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())

        return response_400

    if response.status_code == 409:
        response_409 = Problem.from_dict(response.json())

        return response_409

    if response.status_code == 422:
        response_422 = Problem.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[Problem | Shipment]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ShipmentCreate,
    idempotency_key: str,
) -> Response[Problem | Shipment]:
    """Create Shipment

     Create an empty shipment to attach documents to.

    Args:
        idempotency_key (str): Client-generated unique key (a UUID is ideal). Retrying with the
            same key and body returns the original response instead of repeating the side effect.
        body (ShipmentCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | Shipment]
    """

    kwargs = _get_kwargs(
        body=body,
        idempotency_key=idempotency_key,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: ShipmentCreate,
    idempotency_key: str,
) -> Problem | Shipment | None:
    """Create Shipment

     Create an empty shipment to attach documents to.

    Args:
        idempotency_key (str): Client-generated unique key (a UUID is ideal). Retrying with the
            same key and body returns the original response instead of repeating the side effect.
        body (ShipmentCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | Shipment
    """

    return sync_detailed(
        client=client,
        body=body,
        idempotency_key=idempotency_key,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ShipmentCreate,
    idempotency_key: str,
) -> Response[Problem | Shipment]:
    """Create Shipment

     Create an empty shipment to attach documents to.

    Args:
        idempotency_key (str): Client-generated unique key (a UUID is ideal). Retrying with the
            same key and body returns the original response instead of repeating the side effect.
        body (ShipmentCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | Shipment]
    """

    kwargs = _get_kwargs(
        body=body,
        idempotency_key=idempotency_key,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ShipmentCreate,
    idempotency_key: str,
) -> Problem | Shipment | None:
    """Create Shipment

     Create an empty shipment to attach documents to.

    Args:
        idempotency_key (str): Client-generated unique key (a UUID is ideal). Retrying with the
            same key and body returns the original response instead of repeating the side effect.
        body (ShipmentCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | Shipment
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
            idempotency_key=idempotency_key,
        )
    ).parsed
