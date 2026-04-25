"""Blob storage abstraction (T100, T105).

`LocalBlobStore` is the filesystem-backed v1 implementation.
`R2BlobStore` (T105) adds Cloudflare R2 / S3-compatible storage behind
the same `BlobStoreProtocol`. T106 wires R2 into the ingest endpoint.
"""

from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.client import Config as BotoConfig


class BlobStoreProtocol(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...


class LocalBlobStore:
    """Stores blobs as files under `root`. Keys are path fragments, but
    must resolve strictly inside `root` — a manifest from a hostile
    client cannot escape the storage root via `..` or absolute paths.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve_safe(self, key: str) -> Path:
        if not key:
            raise ValueError("key must not be empty")
        if key.startswith("/") or key.startswith("\\"):
            raise ValueError("key must be relative")
        target = (self._root / key).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("key escapes storage root") from exc
        return target

    def put(self, key: str, data: bytes) -> None:
        path = self._resolve_safe(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self._resolve_safe(key)
        if not path.exists():
            return None
        return path.read_bytes()


class R2BlobStore:
    """S3-compatible blob store backed by Cloudflare R2.

    R2 speaks the S3 API, so production uses boto3 with R2's
    `endpoint_url`. Tests run under moto's `mock_aws` and pass
    `endpoint_url=None` so boto3 hits the moto-mocked AWS S3 endpoint
    instead — same code path, no real network.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        access_key_id: str,
        secret_access_key: str,
        region_name: str = "auto",
    ) -> None:
        self._bucket = bucket
        # SigV4 is the only signature R2 supports; pinning here keeps
        # both real R2 and moto-mocked S3 happy.
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region_name,
            config=BotoConfig(signature_version="s3v4"),
        )

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes | None:
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            return None
        body: bytes = obj["Body"].read()
        return body

    def sign_url(self, key: str, *, expires_in: int) -> str:
        if expires_in <= 0:
            raise ValueError("expires_in must be positive")
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        return url
