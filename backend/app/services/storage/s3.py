"""S3-compatible storage backend (MinIO/Silo, AWS S3, …)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.services.storage.base import (
    CHUNK_SIZE,
    ObjectNotFoundError,
    StorageBackend,
    validate_key,
)

# Error codes an S3 endpoint uses for "this object/key is not there".
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _is_not_found(error: ClientError) -> bool:
    response = error.response or {}
    code = str(response.get("Error", {}).get("Code", ""))
    if code in _NOT_FOUND_CODES:
        return True
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 404


class S3Storage(StorageBackend):
    """
    Stores objects in a single bucket, keyed by the same relative path the
    filesystem backend would use. One client is created lazily and reused.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        region: str = "us-east-1",
        access_key: str,
        secret_key: str,
        force_path_style: bool = True,
    ):
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.region = region
        self.force_path_style = force_path_style
        self._access_key = access_key
        self._secret_key = secret_key
        self._session = aioboto3.Session()
        self._client: Any = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    def _client_kwargs(self) -> dict[str, Any]:
        return {
            "endpoint_url": self.endpoint_url,
            "region_name": self.region,
            "aws_access_key_id": self._access_key,
            "aws_secret_access_key": self._secret_key,
            "config": Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if self.force_path_style else "auto"},
            ),
        }

    async def client(self) -> Any:
        """The shared S3 client, created on first use."""
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    stack = AsyncExitStack()
                    client = await stack.enter_async_context(
                        self._session.client("s3", **self._client_kwargs())
                    )
                    self._stack = stack
                    self._client = client
        return self._client

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._client = None

    async def put(self, key: str, data: bytes) -> None:
        client = await self.client()
        await client.put_object(Bucket=self.bucket, Key=validate_key(key), Body=data)

    async def get(self, key: str) -> bytes:
        key = validate_key(key)
        client = await self.client()
        try:
            response = await client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if _is_not_found(e):
                raise ObjectNotFoundError(key) from e
            raise
        async with response["Body"] as body:
            return await body.read()

    async def open_stream(self, key: str, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        key = validate_key(key)
        client = await self.client()
        try:
            response = await client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if _is_not_found(e):
                raise ObjectNotFoundError(key) from e
            raise

        body = response["Body"]

        async def _iterate() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await body.read(chunk_size)
                    if not chunk:
                        return
                    yield chunk
            finally:
                close = getattr(body, "close", None)
                if close is not None:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result

        return _iterate()

    async def delete(self, key: str) -> None:
        client = await self.client()
        await client.delete_object(Bucket=self.bucket, Key=validate_key(key))

    async def exists(self, key: str) -> bool:
        key = validate_key(key)
        client = await self.client()
        try:
            await client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if _is_not_found(e):
                return False
            raise
        return True

    async def copy(self, src_key: str, dst_key: str) -> None:
        src_key = validate_key(src_key)
        dst_key = validate_key(dst_key)
        client = await self.client()
        try:
            await client.copy_object(
                Bucket=self.bucket,
                Key=dst_key,
                CopySource={"Bucket": self.bucket, "Key": src_key},
            )
        except ClientError as e:
            if _is_not_found(e):
                raise ObjectNotFoundError(src_key) from e
            raise
