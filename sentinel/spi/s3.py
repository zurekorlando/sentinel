"""
Amazon S3 / S3-Compatible Storage Provider (e.g. MinIO).

Features:
  - Automatic multipart upload for chunks larger than MULTIPART_THRESHOLD.
  - Exponential backoff with full jitter on transient 5xx / timeout errors
    (max 5 retries, configurable).
  - All credentials are injected at construction time; no implicit env-var
    fallback is assumed (though boto3 will still honour its standard chain
    if you pass None values).
"""

from __future__ import annotations

import io
import logging
import random
import time
from typing import Any, Callable, Iterator

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

from sentinel.spi.base import ChunkMeta, IStorageProvider

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Constants                                                           #
# ------------------------------------------------------------------ #
MULTIPART_THRESHOLD = 8 * 1024 * 1024   # 8 MB
MAX_RETRIES = 5
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 32.0

_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_ERROR_CODES = {
    "RequestTimeout",
    "SlowDown",
    "ServiceUnavailable",
    "InternalError",
}


class S3Provider(IStorageProvider):
    """
    Storage provider for Amazon S3 or any S3-compatible service (MinIO, etc.).

    Args:
        bucket:               Target bucket name.
        prefix:               Key prefix for all chunk objects (default "chunks/").
        endpoint_url:         Override endpoint for S3-compatible services.
        aws_access_key_id:    Access key (None → boto3 chain).
        aws_secret_access_key: Secret key (None → boto3 chain).
        region:               AWS region name.
        max_retries:          Max retry attempts on transient errors.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "chunks/",
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region: str = "us-east-1",
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"
        self.max_retries = max_retries

        session = boto3.Session(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region,
        )
        self._client = session.client("s3", endpoint_url=endpoint_url)
        self._transfer_config = boto3.s3.transfer.TransferConfig(
            multipart_threshold=MULTIPART_THRESHOLD,
            multipart_chunksize=MULTIPART_THRESHOLD,
            max_concurrency=4,
            use_threads=True,
        )

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _object_key(self, hash_id: str) -> str:
        """Shard into 256 sub-prefixes to avoid hot-key effects."""
        return f"{self.prefix}{hash_id[:2]}/{hash_id[2:]}"

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, ClientError):
            status = exc.response["ResponseMetadata"].get("HTTPStatusCode", 0)
            code = exc.response["Error"].get("Code", "")
            return status in _RETRYABLE_HTTP_CODES or code in _RETRYABLE_ERROR_CODES
        return isinstance(exc, (EndpointConnectionError, ConnectionError, TimeoutError))

    def _with_retry(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Execute *func* with exponential backoff + full jitter on transient errors.

        Raises the last exception if all retries are exhausted.
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                if not self._is_retryable(exc):
                    raise
                last_exc = exc
                sleep = min(
                    BASE_BACKOFF_S * (2 ** attempt) + random.uniform(0, 1),
                    MAX_BACKOFF_S,
                )
                log.warning(
                    "S3 transient error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, self.max_retries, sleep, exc,
                )
                time.sleep(sleep)
        raise RuntimeError(
            f"S3 operation failed after {self.max_retries} retries"
        ) from last_exc

    # ------------------------------------------------------------------ #
    #  IStorageProvider                                                    #
    # ------------------------------------------------------------------ #

    def upload_chunk(self, data: bytes, hash_id: str) -> None:
        key = self._object_key(hash_id)
        buf = io.BytesIO(data)

        def _do_upload() -> None:
            buf.seek(0)
            self._client.upload_fileobj(
                buf,
                self.bucket,
                key,
                Config=self._transfer_config,
            )

        self._with_retry(_do_upload)
        log.debug("Uploaded chunk %s → s3://%s/%s", hash_id[:8], self.bucket, key)

    def download_chunk(self, hash_id: str) -> bytes:
        key = self._object_key(hash_id)
        buf = io.BytesIO()

        def _do_download() -> None:
            self._client.download_fileobj(self.bucket, key, buf)

        try:
            self._with_retry(_do_download)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                raise KeyError(f"Chunk not found in S3: {hash_id}") from exc
            raise
        return buf.getvalue()

    def exists(self, hash_id: str) -> bool:
        key = self._object_key(hash_id)
        try:
            self._with_retry(
                self._client.head_object, Bucket=self.bucket, Key=key
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def delete_chunk(self, hash_id: str) -> None:
        key = self._object_key(hash_id)
        # delete_object is a no-op for missing keys per S3 spec.
        self._with_retry(
            self._client.delete_object, Bucket=self.bucket, Key=key
        )

    def list_chunks(self, prefix: str = "") -> Iterator[ChunkMeta]:
        paginator = self._client.get_paginator("list_objects_v2")
        s3_prefix = self._object_key(prefix) if prefix else self.prefix

        pages = paginator.paginate(Bucket=self.bucket, Prefix=s3_prefix)
        for page in pages:
            for obj in page.get("Contents", []):
                # Reconstruct hash_id from the key: strip our prefix and the shard "/"
                relative = obj["Key"][len(self.prefix):]  # e.g. "ab/cdef…"
                hash_id = relative.replace("/", "", 1)
                yield ChunkMeta(hash_id=hash_id, size=obj["Size"])

    def health_check(self) -> bool:
        try:
            self._with_retry(self._client.head_bucket, Bucket=self.bucket)
            return True
        except Exception as exc:
            log.error("S3 health check failed: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    #  Catalog overrides                                                   #
    # ------------------------------------------------------------------ #

    def upload_catalog(self, data: bytes, name: str) -> None:
        key = f"{self.prefix}catalog/{name}"
        buf = io.BytesIO(data)
        self._with_retry(
            self._client.upload_fileobj, buf, self.bucket, key
        )

    def download_catalog(self, name: str) -> bytes:
        key = f"{self.prefix}catalog/{name}"
        buf = io.BytesIO()
        try:
            self._with_retry(
                self._client.download_fileobj, self.bucket, key, buf
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                raise KeyError(f"Catalog snapshot not found: {name}") from exc
            raise
        return buf.getvalue()

    def __repr__(self) -> str:
        return (
            f"S3Provider(bucket={self.bucket!r}, prefix={self.prefix!r})"
        )
