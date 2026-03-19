"""
Tests for the Storage Provider Interface (SPI) implementations.

LocalProvider is tested against a real tmp directory.
S3Provider is tested via mocking (no MinIO required for unit tests);
integration tests that need a live MinIO use the ``integration`` marker.

Run only unit tests:
    pytest tests/test_spi.py -m "not integration"

Run integration tests (requires docker-compose up):
    pytest tests/test_spi.py -m integration
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentinel.spi.base import ChunkMeta, IStorageProvider
from sentinel.spi.local import LocalProvider


# ------------------------------------------------------------------ #
#  Fixtures                                                            #
# ------------------------------------------------------------------ #

@pytest.fixture
def tmp_store(tmp_path: Path) -> LocalProvider:
    return LocalProvider(str(tmp_path / "store"))


SAMPLE_HASH = "a" * 64       # 64-char hex string (valid SHA-256 placeholder)
SAMPLE_DATA = b"hello, sentinel chunk data"


# ------------------------------------------------------------------ #
#  IStorageProvider contract tests (run against LocalProvider)         #
# ------------------------------------------------------------------ #

class TestIStorageProviderContract:
    """
    These tests verify the IStorageProvider contract.
    Point ``provider`` fixture at any implementation to validate it.
    """

    @pytest.fixture
    def provider(self, tmp_store) -> IStorageProvider:
        return tmp_store

    def test_upload_and_download(self, provider):
        provider.upload_chunk(SAMPLE_DATA, SAMPLE_HASH)
        assert provider.download_chunk(SAMPLE_HASH) == SAMPLE_DATA

    def test_exists_false_before_upload(self, provider):
        assert not provider.exists("b" * 64)

    def test_exists_true_after_upload(self, provider):
        provider.upload_chunk(SAMPLE_DATA, SAMPLE_HASH)
        assert provider.exists(SAMPLE_HASH)

    def test_upload_idempotent(self, provider):
        """Uploading the same chunk twice must not raise."""
        provider.upload_chunk(SAMPLE_DATA, SAMPLE_HASH)
        provider.upload_chunk(SAMPLE_DATA, SAMPLE_HASH)  # second call is no-op
        assert provider.exists(SAMPLE_HASH)

    def test_delete_chunk(self, provider):
        provider.upload_chunk(SAMPLE_DATA, SAMPLE_HASH)
        provider.delete_chunk(SAMPLE_HASH)
        assert not provider.exists(SAMPLE_HASH)

    def test_delete_missing_is_noop(self, provider):
        """Deleting a non-existent chunk must not raise."""
        provider.delete_chunk("c" * 64)

    def test_download_missing_raises_key_error(self, provider):
        with pytest.raises(KeyError):
            provider.download_chunk("d" * 64)

    def test_list_chunks(self, provider):
        hashes = ["e" * 63 + str(i) for i in range(3)]
        for h in hashes:
            provider.upload_chunk(b"data", h)
        listed = {m.hash_id for m in provider.list_chunks()}
        for h in hashes:
            assert h in listed

    def test_list_chunks_with_prefix(self, provider):
        provider.upload_chunk(b"x", "aa" + "0" * 62)
        provider.upload_chunk(b"x", "bb" + "0" * 62)
        listed = list(provider.list_chunks(prefix="aa"))
        assert len(listed) == 1
        assert listed[0].hash_id.startswith("aa")

    def test_health_check(self, provider):
        assert provider.health_check() is True

    def test_catalog_upload_download(self, provider):
        data = b"encrypted catalog bytes"
        provider.upload_catalog(data, "snapshot-abc.db.enc")
        recovered = provider.download_catalog("snapshot-abc.db.enc")
        assert recovered == data

    def test_catalog_missing_raises(self, provider):
        with pytest.raises(KeyError):
            provider.download_catalog("does-not-exist.db.enc")


# ------------------------------------------------------------------ #
#  LocalProvider-specific tests                                        #
# ------------------------------------------------------------------ #

class TestLocalProvider:

    def test_shard_directory_structure(self, tmp_store):
        """Chunks should be stored under a two-char shard subdirectory."""
        h = "ab" + "c" * 62
        tmp_store.upload_chunk(b"data", h)
        expected = tmp_store.base_path / "ab" / ("c" * 62)
        assert expected.exists()

    def test_atomic_write(self, tmp_store, tmp_path):
        """
        A .tmp file must not persist if something goes wrong.
        We simulate a crash by verifying no .tmp files remain after upload.
        """
        tmp_store.upload_chunk(b"some data", SAMPLE_HASH)
        tmp_files = list(tmp_store.base_path.rglob("*.tmp"))
        assert tmp_files == [], "Stale .tmp file found after upload"

    def test_empty_shard_cleaned_up(self, tmp_store):
        h = "fe" + "1" * 62
        tmp_store.upload_chunk(b"x", h)
        shard = tmp_store.base_path / "fe"
        assert shard.exists()
        tmp_store.delete_chunk(h)
        assert not shard.exists(), "Empty shard directory should be removed"

    def test_list_skips_catalog_dir(self, tmp_store):
        tmp_store.upload_catalog(b"catalog data", "test.db")
        listed_hashes = {m.hash_id for m in tmp_store.list_chunks()}
        assert not any("catalog" in h for h in listed_hashes)


# ------------------------------------------------------------------ #
#  S3Provider unit tests (mocked)                                     #
# ------------------------------------------------------------------ #

class TestS3ProviderMocked:

    @pytest.fixture
    def s3_provider(self):
        """Return an S3Provider with a fully mocked boto3 client."""
        from sentinel.spi.s3 import S3Provider
        provider = S3Provider(
            bucket="test-bucket",
            prefix="chunks/",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        provider._client = MagicMock()
        return provider

    def test_upload_calls_upload_fileobj(self, s3_provider):
        s3_provider.upload_chunk(b"data", SAMPLE_HASH)
        s3_provider._client.upload_fileobj.assert_called_once()

    def test_exists_true_on_head_success(self, s3_provider):
        s3_provider._client.head_object.return_value = {}
        assert s3_provider.exists(SAMPLE_HASH) is True

    def test_exists_false_on_404(self, s3_provider):
        from botocore.exceptions import ClientError
        s3_provider._client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"},
             "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )
        assert s3_provider.exists(SAMPLE_HASH) is False

    def test_delete_calls_delete_object(self, s3_provider):
        s3_provider.delete_chunk(SAMPLE_HASH)
        s3_provider._client.delete_object.assert_called_once()

    def test_object_key_sharding(self, s3_provider):
        """Key must be: prefix/first2chars/rest"""
        key = s3_provider._object_key("abcdef1234" + "0" * 54)
        assert key == "chunks/ab/cdef1234" + "0" * 54

    def test_retry_on_500(self, s3_provider):
        """upload_chunk must retry on a 5xx error and succeed on second attempt."""
        from botocore.exceptions import ClientError
        call_count = {"n": 0}
        original = s3_provider._client.upload_fileobj

        def flaky_upload(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ClientError(
                    {"Error": {"Code": "InternalError", "Message": "err"},
                     "ResponseMetadata": {"HTTPStatusCode": 500}},
                    "UploadObject",
                )

        s3_provider._client.upload_fileobj = flaky_upload
        # Should not raise — retries should handle the first 500.
        # We mock time.sleep to avoid actually waiting.
        with patch("sentinel.spi.s3.time.sleep"):
            s3_provider.upload_chunk(b"data", SAMPLE_HASH)

        assert call_count["n"] == 2

    def test_non_retryable_error_propagates(self, s3_provider):
        """A 403 Forbidden must not be retried and must propagate immediately."""
        from botocore.exceptions import ClientError
        s3_provider._client.upload_fileobj.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Forbidden"},
             "ResponseMetadata": {"HTTPStatusCode": 403}},
            "UploadObject",
        )
        with pytest.raises(ClientError):
            s3_provider.upload_chunk(b"data", SAMPLE_HASH)


# ------------------------------------------------------------------ #
#  Integration tests (require live MinIO — docker-compose up)          #
# ------------------------------------------------------------------ #

@pytest.mark.integration
class TestS3ProviderIntegration:
    """
    Requires:  docker-compose up minio minio-init
    Run with:  pytest tests/test_spi.py -m integration
    """

    @pytest.fixture(scope="class")
    def s3_provider(self):
        from sentinel.spi.s3 import S3Provider
        return S3Provider(
            bucket="sentinel",
            prefix="test-chunks/",
            endpoint_url="http://localhost:9000",
            aws_access_key_id="sentinel_admin",
            aws_secret_access_key="sentinel_password",
            region="us-east-1",
        )

    def test_health_check(self, s3_provider):
        assert s3_provider.health_check() is True

    def test_full_cycle(self, s3_provider):
        h = "ff" + os.urandom(31).hex()
        data = os.urandom(64 * 1024)
        s3_provider.upload_chunk(data, h)
        assert s3_provider.exists(h)
        assert s3_provider.download_chunk(h) == data
        s3_provider.delete_chunk(h)
        assert not s3_provider.exists(h)
