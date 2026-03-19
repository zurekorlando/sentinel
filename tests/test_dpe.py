"""
Tests for the Data Processing Engine (DPE) modules.

Covers:
  - RabinChunker: determinism, min/max bounds, boundary stability (CDC property)
  - ChunkCipher: encrypt/decrypt round-trip, IV uniqueness, tamper detection
  - ProcessingPipeline: single-chunk round-trip, stream ordering, dedup keys
"""

from __future__ import annotations

import hashlib
import os

import pytest

from sentinel.dpe.chunker import RabinChunker
from sentinel.dpe.crypto import ChunkCipher, derive_key
from sentinel.dpe.pipeline import ProcessingPipeline


# ------------------------------------------------------------------ #
#  Fixtures                                                            #
# ------------------------------------------------------------------ #

@pytest.fixture(scope="module")
def cipher() -> ChunkCipher:
    derived = derive_key("test-passphrase")
    return ChunkCipher(derived.key)


@pytest.fixture(scope="module")
def pipeline(cipher) -> ProcessingPipeline:
    return ProcessingPipeline(cipher=cipher, compression_level=1)


@pytest.fixture(scope="module")
def chunker() -> RabinChunker:
    return RabinChunker()


# ------------------------------------------------------------------ #
#  RabinChunker                                                        #
# ------------------------------------------------------------------ #

class TestRabinChunker:

    def test_reassembly_equals_original(self, chunker):
        """Chunks must reassemble to the exact original bytes."""
        data = os.urandom(512 * 1024)  # 512 KB
        chunks = list(chunker.chunkify(data))
        assert b"".join(chunks) == data

    def test_min_chunk_size_respected(self, chunker):
        """No chunk (except possibly the last) should be smaller than min_size."""
        data = os.urandom(1024 * 1024)  # 1 MB
        chunks = list(chunker.chunkify(data))
        for c in chunks[:-1]:
            assert len(c) >= chunker.min_size, (
                f"Chunk smaller than min_size: {len(c)} < {chunker.min_size}"
            )

    def test_max_chunk_size_never_exceeded(self, chunker):
        """No chunk should exceed max_size."""
        # Repeated bytes stress-test the max limit.
        data = b"\x00" * (512 * 1024)
        chunks = list(chunker.chunkify(data))
        for c in chunks:
            assert len(c) <= chunker.max_size, (
                f"Chunk larger than max_size: {len(c)} > {chunker.max_size}"
            )

    def test_deterministic(self, chunker):
        """Same input must always produce identical chunk boundaries."""
        data = os.urandom(256 * 1024)
        run1 = [hashlib.sha256(c).hexdigest() for c in chunker.chunkify(data)]
        run2 = [hashlib.sha256(c).hexdigest() for c in chunker.chunkify(data)]
        assert run1 == run2

    def test_content_defined_boundaries(self, chunker):
        """
        Inserting a byte at the start of a large payload should shift the
        boundary of only the first chunk, not ALL subsequent chunks.
        (Core CDC property — shift invariance after the insertion point.)
        """
        data = os.urandom(512 * 1024)
        modified = b"\xFF" + data

        chunks_original = list(chunker.chunkify(data))
        chunks_modified = list(chunker.chunkify(modified))

        # After the first differing chunk, the tail should eventually re-sync.
        original_hashes = {hashlib.sha256(c).hexdigest() for c in chunks_original}
        modified_hashes = {hashlib.sha256(c).hexdigest() for c in chunks_modified}

        shared = original_hashes & modified_hashes
        # With a 512 KB payload and 64 KB avg chunks (~8 chunks),
        # we expect at least some chunks to be shared.
        assert len(shared) > 0, "CDC should preserve some chunk boundaries after a small prefix insertion"

    def test_stream_equals_in_memory(self, chunker):
        """chunkify_stream must produce the same result as chunkify."""
        import io
        data = os.urandom(256 * 1024)
        from_mem = list(chunker.chunkify(data))
        from_stream = list(chunker.chunkify_stream(io.BytesIO(data)))
        assert b"".join(from_mem) == b"".join(from_stream)

    def test_empty_input(self, chunker):
        assert list(chunker.chunkify(b"")) == []

    def test_small_input_single_chunk(self, chunker):
        """A payload smaller than min_size must be returned as a single chunk."""
        data = b"hello sentinel"
        chunks = list(chunker.chunkify(data))
        assert len(chunks) == 1
        assert chunks[0] == data


# ------------------------------------------------------------------ #
#  ChunkCipher                                                         #
# ------------------------------------------------------------------ #

class TestChunkCipher:

    def test_round_trip(self, cipher):
        """Encrypt then decrypt must return the original plaintext."""
        plaintext = b"the quick brown fox jumps over the lazy dog"
        blob = cipher.encrypt(plaintext)
        recovered = cipher.decrypt(blob.data)
        assert recovered == plaintext

    def test_unique_ivs(self, cipher):
        """Each call to encrypt must produce a distinct IV."""
        ivs = {cipher.encrypt(b"same data").iv for _ in range(50)}
        assert len(ivs) == 50

    def test_tamper_detected(self, cipher):
        """Flipping a byte in the ciphertext must raise an authentication error."""
        from cryptography.exceptions import InvalidTag
        plaintext = b"sensitive data"
        blob = cipher.encrypt(plaintext)
        # Flip the last byte of the blob
        tampered = blob.data[:-1] + bytes([blob.data[-1] ^ 0xFF])
        with pytest.raises(InvalidTag):
            cipher.decrypt(tampered)

    def test_wrong_key_rejected(self):
        """Decrypting with a different key must fail."""
        from cryptography.exceptions import InvalidTag
        key1 = derive_key("password-one")
        key2 = derive_key("password-two")
        c1 = ChunkCipher(key1.key)
        c2 = ChunkCipher(key2.key)
        blob = c1.encrypt(b"secret payload")
        with pytest.raises(InvalidTag):
            c2.decrypt(blob.data)

    def test_key_length_enforced(self):
        with pytest.raises(ValueError):
            ChunkCipher(b"too-short")

    def test_derive_key_empty_passphrase(self):
        with pytest.raises(ValueError):
            derive_key("")

    def test_derive_key_salt_reuse(self):
        """Re-deriving with the same salt must produce the same key."""
        dk1 = derive_key("my passphrase")
        dk2 = derive_key("my passphrase", salt=dk1.salt)
        assert dk1.key == dk2.key

    def test_derive_key_different_passphrases(self):
        salt = os.urandom(32)
        dk1 = derive_key("pass-a", salt=salt)
        dk2 = derive_key("pass-b", salt=salt)
        assert dk1.key != dk2.key


# ------------------------------------------------------------------ #
#  ProcessingPipeline                                                  #
# ------------------------------------------------------------------ #

class TestProcessingPipeline:

    def test_single_chunk_round_trip(self, pipeline):
        """process_chunk then decrypt_chunk must return original bytes."""
        raw = os.urandom(32 * 1024)
        processed = pipeline.process_chunk(raw)
        recovered = pipeline.decrypt_chunk(processed.blob)
        assert recovered == raw

    def test_hash_id_is_sha256_of_raw(self, pipeline):
        raw = b"deterministic data"
        processed = pipeline.process_chunk(raw)
        expected = hashlib.sha256(raw).hexdigest()
        assert processed.hash_id == expected

    def test_identical_chunks_same_hash(self, pipeline):
        """Deduplication relies on identical raw → identical hash_id."""
        raw = b"duplicate content " * 1000
        p1 = pipeline.process_chunk(raw)
        p2 = pipeline.process_chunk(raw)
        assert p1.hash_id == p2.hash_id

    def test_different_blobs_same_hash(self, pipeline):
        """Two encryptions of the same content → same hash but different blobs (unique IV)."""
        raw = b"same raw content " * 500
        p1 = pipeline.process_chunk(raw)
        p2 = pipeline.process_chunk(raw)
        assert p1.hash_id == p2.hash_id
        assert p1.blob != p2.blob  # unique IVs

    def test_stream_preserves_order(self, pipeline):
        """process_stream must yield results in the same order as input chunks."""
        chunks = [os.urandom(32 * 1024) for _ in range(20)]
        expected_hashes = [hashlib.sha256(c).hexdigest() for c in chunks]
        results = list(pipeline.process_stream(iter(chunks), max_workers=4))
        actual_hashes = [r.hash_id for r in results]
        assert actual_hashes == expected_hashes

    def test_size_fields_correct(self, pipeline):
        raw = os.urandom(64 * 1024)
        processed = pipeline.process_chunk(raw)
        assert processed.original_size == len(raw)
        assert processed.compressed_size > 0
        assert processed.stored_size == len(processed.blob)
