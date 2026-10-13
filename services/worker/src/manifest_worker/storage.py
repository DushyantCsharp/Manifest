"""Object storage (S3; MinIO locally). Timeouts and 5xx become ``TransientStorageError``."""

from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from manifest_worker.errors import ContentError, TransientStorageError

_TRANSIENT_CODES = {"RequestTimeout", "SlowDown", "InternalError", "ServiceUnavailable"}
_NETWORK = (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError, ConnectionClosedError)


class Storage(Protocol):
    def get(self, key: str) -> bytes: ...

    def put(self, key: str, data: bytes, content_type: str) -> None: ...


def page_key(document_id: object, page_no: int) -> str:
    return f"pages/{document_id}/{page_no:04d}.pdf"


class S3Storage:
    def __init__(
        self, endpoint_url: str, bucket: str, access_key: str, secret_key: str, region: str
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            # One attempt per call: retries belong to the task, with backoff the broker can see.
            config=Config(
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 1, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )

    def get(self, key: str) -> bytes:
        try:
            body: bytes = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except _NETWORK as exc:
            raise TransientStorageError(f"s3 get {key}: {exc}") from exc
        except ClientError as exc:
            raise self._classify(exc, key) from exc
        return body

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
        except _NETWORK as exc:
            raise TransientStorageError(f"s3 put {key}: {exc}") from exc
        except ClientError as exc:
            raise self._classify(exc, key) from exc

    @staticmethod
    def _classify(exc: ClientError, key: str) -> Exception:
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code in _TRANSIENT_CODES or status >= 500:
            return TransientStorageError(f"s3 {code or status} for {key}")
        return ContentError(f"s3 {code or status} for {key}")
