"""Presigned multipart uploads straight to S3 (MinIO locally): the API never handles bytes.

``start`` opens a multipart upload and presigns one ``PUT`` per part against the public
endpoint; the client uploads and keeps each part's ETag; ``complete`` assembles the parts.
boto3 is synchronous, so every network call runs in a thread.
"""

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from manifest_api.adapters.settings import ApiSettings
from manifest_api.errors import ConflictError

# Codes S3 answers when the client's part list doesn't match what it uploaded.
_BAD_PARTS = {"InvalidPart", "InvalidPartOrder", "EntityTooSmall", "NoSuchUpload"}


@dataclass(frozen=True)
class PresignedPart:
    number: int
    url: str
    offset: int
    size: int


@dataclass(frozen=True)
class Started:
    upload_id: str
    part_size: int
    parts: list[PresignedPart]


def _client(endpoint: str, settings: ApiSettings) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


class S3Uploads:
    def __init__(self, settings: ApiSettings) -> None:
        self._bucket = settings.s3_bucket
        self._part_size = settings.upload_part_size
        self.url_ttl = settings.upload_url_ttl_seconds
        self._s3 = _client(settings.s3_endpoint_url, settings)
        public = settings.s3_public_endpoint_url or settings.s3_endpoint_url
        self._presigner = _client(public, settings)

    async def start(self, key: str, size: int, content_type: str) -> Started:
        created = await asyncio.to_thread(
            self._s3.create_multipart_upload, Bucket=self._bucket, Key=key, ContentType=content_type
        )
        upload_id = str(created["UploadId"])
        parts = []
        for number, offset in enumerate(range(0, size, self._part_size), start=1):
            url = self._presigner.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": number,
                },
                ExpiresIn=self.url_ttl,
            )
            parts.append(PresignedPart(number, url, offset, min(self._part_size, size - offset)))
        return Started(upload_id, self._part_size, parts)

    async def complete(self, key: str, parts: Sequence[tuple[int, str]], size: int) -> None:
        """Assemble the uploaded parts. A retry after a completed upload is a no-op as long as
        the object is there with the registered size."""
        upload_id = await asyncio.to_thread(self._upload_id, key)
        if upload_id is None:
            if await asyncio.to_thread(self._size, key) == size:
                return  # completed by an earlier attempt
            raise ConflictError("no upload is in progress for this document")
        try:
            await asyncio.to_thread(
                self._s3.complete_multipart_upload,
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": [{"PartNumber": number, "ETag": etag} for number, etag in parts]
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _BAD_PARTS:
                raise ConflictError(f"the parts don't match the upload: {exc}") from exc
            raise
        if await asyncio.to_thread(self._size, key) != size:
            raise ConflictError("the uploaded size differs from the registered size")

    async def sha256(self, key: str) -> str:
        return await asyncio.to_thread(self._sha256, key)

    def _upload_id(self, key: str) -> str | None:
        found = self._s3.list_multipart_uploads(Bucket=self._bucket, Prefix=key)
        for upload in found.get("Uploads", []):
            if upload["Key"] == key:
                return str(upload["UploadId"])
        return None

    def _size(self, key: str) -> int | None:
        try:
            head = self._s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError:
            return None
        return int(head["ContentLength"])

    def _sha256(self, key: str) -> str:
        body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"]
        digest = hashlib.sha256()
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()
