from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.document_create import DocumentCreate
from ...models.document_upload import DocumentUpload
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    shipment_id: UUID,
    *,
    body: DocumentCreate,
    idempotency_key: str,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["Idempotency-Key"] = idempotency_key

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/shipments/{shipment_id}/documents".format(
            shipment_id=quote(str(shipment_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DocumentUpload | Problem | None:
    if response.status_code == 201:
        response_201 = DocumentUpload.from_dict(response.json())

        return response_201

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())

        return response_400

    if response.status_code == 404:
        response_404 = Problem.from_dict(response.json())

        return response_404

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


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DocumentUpload | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    shipment_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: DocumentCreate,
    idempotency_key: str,
) -> Response[DocumentUpload | Problem]:
    """Register Document

     Register a document and get presigned URLs for a multipart upload straight to storage.

    Args:
        shipment_id (UUID):
        idempotency_key (str): Client-generated unique key (a UUID is ideal). Retrying with the
            same key and body returns the original response instead of repeating the side effect.
        body (DocumentCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DocumentUpload | Problem]
    """

    kwargs = _get_kwargs(
        shipment_id=shipment_id,
        body=body,
        idempotency_key=idempotency_key,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    shipment_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: DocumentCreate,
    idempotency_key: str,
) -> DocumentUpload | Problem | None:
    """Register Document

     Register a document and get presigned URLs for a multipart upload straight to storage.

    Args:
        shipment_id (UUID):
        idempotency_key (str): Client-generated unique key (a UUID is ideal). Retrying with the
            same key and body returns the original response instead of repeating the side effect.
        body (DocumentCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DocumentUpload | Problem
    """

    return sync_detailed(
        shipment_id=shipment_id,
        client=client,
        body=body,
        idempotency_key=idempotency_key,
    ).parsed


async def asyncio_detailed(
    shipment_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: DocumentCreate,
    idempotency_key: str,
) -> Response[DocumentUpload | Problem]:
    """Register Document

     Register a document and get presigned URLs for a multipart upload straight to storage.

    Args:
        shipment_id (UUID):
        idempotency_key (str): Client-generated unique key (a UUID is ideal). Retrying with the
            same key and body returns the original response instead of repeating the side effect.
        body (DocumentCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DocumentUpload | Problem]
    """

    kwargs = _get_kwargs(
        shipment_id=shipment_id,
        body=body,
        idempotency_key=idempotency_key,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    shipment_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: DocumentCreate,
    idempotency_key: str,
) -> DocumentUpload | Problem | None:
    """Register Document

     Register a document and get presigned URLs for a multipart upload straight to storage.

    Args:
        shipment_id (UUID):
        idempotency_key (str): Client-generated unique key (a UUID is ideal). Retrying with the
            same key and body returns the original response instead of repeating the side effect.
        body (DocumentCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DocumentUpload | Problem
    """

    return (
        await asyncio_detailed(
            shipment_id=shipment_id,
            client=client,
            body=body,
            idempotency_key=idempotency_key,
        )
    ).parsed
