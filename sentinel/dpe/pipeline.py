"""
Data Processing Engine (DPE) — processing pipeline.

Pipeline order (as specified in the architecture doc):
    raw chunk
        ↓  1. Hash  (SHA-256 of the raw chunk → deduplication key)
        ↓  2. Compress (Zstd, default level 3)
        ↓  3. Encrypt  (AES-256-GCM with per-chunk random IV)
        ↓
    ProcessedChunk (ready for SPI upload + catalog insertion)

Thread-safety
-------------
The pipeline is intentionally stateless per processed chunk.  A
``ProcessingPipeline`` instance can be shared across threads; the only
shared resource is the ``ChunkCipher`` object, which is itself thread-safe
because AES-GCM produces the IV internally on each call.

A new ``ZstdCompressor`` is created per-call because the zstandard library
documents compressor objects as not thread-safe for concurrent use on the
same instance.

Concurrency
-----------
``process_stream()`` uses a sliding-window of ``ThreadPoolExecutor`` futures
so that up to ``max_workers`` chunks are in-flight simultaneously while
preserving the original chunk order and bounding memory to
``max_workers * 2`` chunks at a time.
"""

from __future__ import annotations

import collections
import hashlib
import itertools
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Deque, Iterator

import zstandard as zstd

from sentinel.dpe.crypto import ChunkCipher

log = logging.getLogger(__name__)

DEFAULT_COMPRESSION_LEVEL = 3
DEFAULT_MAX_WORKERS = 4


# ------------------------------------------------------------------ #
#  Data types                                                          #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class ProcessedChunk:
    """
    The result of running one raw chunk through the full DPE pipeline.

    Attributes:
        hash_id:         SHA-256 hex digest of the *raw* (pre-compression)
                         chunk.  Used as the deduplication key in the catalog.
        original_size:   Size of the raw chunk in bytes.
        compressed_size: Size after Zstd compression but before encryption.
        blob:            AES-256-GCM encrypted payload:  ``IV || ciphertext``.
        iv:              The 12-byte IV, also embedded in *blob* but stored
                         separately for catalog lookups.
    """
    hash_id: str
    original_size: int
    compressed_size: int
    blob: bytes
    iv: bytes

    @property
    def stored_size(self) -> int:
        """Total bytes that will be written to the storage backend."""
        return len(self.blob)


# ------------------------------------------------------------------ #
#  Pipeline                                                            #
# ------------------------------------------------------------------ #

class ProcessingPipeline:
    """
    Thread-safe pipeline: raw bytes → :class:`ProcessedChunk`.

    Args:
        cipher:            A :class:`~sentinel.dpe.crypto.ChunkCipher`
                           initialised with the job's encryption key.
        compression_level: Zstd compression level (1-22; default 3).
    """

    def __init__(
        self,
        cipher: ChunkCipher,
        compression_level: int = DEFAULT_COMPRESSION_LEVEL,
    ) -> None:
        self._cipher = cipher
        self._compression_level = compression_level

    # ------------------------------------------------------------------ #
    #  Single-chunk processing                                             #
    # ------------------------------------------------------------------ #

    def process_chunk(self, raw: bytes) -> ProcessedChunk:
        """
        Run *raw* bytes through the full pipeline.

        Steps:
          1. SHA-256 hash of the raw chunk (deduplication key).
          2. Zstd compress (a fresh compressor per call → thread-safe).
          3. AES-256-GCM encrypt with a random IV.

        This method is safe to call concurrently from multiple threads.
        """
        # 1 — Hash (on raw bytes, before any transformation)
        hash_id = hashlib.sha256(raw).hexdigest()
        original_size = len(raw)

        # 2 — Compress (fresh compressor for thread-safety)
        compressor = zstd.ZstdCompressor(level=self._compression_level)
        compressed = compressor.compress(raw)
        compressed_size = len(compressed)

        # 3 — Encrypt
        blob = self._cipher.encrypt(compressed)

        log.debug(
            "Chunk %s… raw=%d compressed=%d stored=%d (ratio=%.2f)",
            hash_id[:8],
            original_size,
            compressed_size,
            len(blob.data),
            original_size / compressed_size if compressed_size else 0,
        )

        return ProcessedChunk(
            hash_id=hash_id,
            original_size=original_size,
            compressed_size=compressed_size,
            blob=blob.data,
            iv=blob.iv,
        )

    # ------------------------------------------------------------------ #
    #  Stream processing                                                   #
    # ------------------------------------------------------------------ #

    def process_stream(
        self,
        chunks: Iterator[bytes],
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> Iterator[ProcessedChunk]:
        """
        Process an iterator of raw chunks in parallel, yielding results in
        the same order as the input.

        Uses a sliding window of futures so that at most
        ``max_workers * 2`` chunks are buffered in RAM at any given time.

        Args:
            chunks:      Iterator of raw chunk byte-strings.
            max_workers: Number of parallel worker threads.

        Yields:
            :class:`ProcessedChunk` instances in input order.
        """
        window: int = max_workers * 2
        pending: Deque[Future[ProcessedChunk]] = collections.deque()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            chunks_iter = iter(chunks)

            # Pre-fill the window
            for raw in itertools.islice(chunks_iter, window):
                pending.append(executor.submit(self.process_chunk, raw))

            while pending:
                # Yield the oldest result (in order)
                yield pending.popleft().result()

                # Enqueue the next chunk to keep the window full
                try:
                    raw = next(chunks_iter)
                    pending.append(executor.submit(self.process_chunk, raw))
                except StopIteration:
                    pass

    # ------------------------------------------------------------------ #
    #  Decryption helpers (for restore path)                               #
    # ------------------------------------------------------------------ #

    def decrypt_chunk(self, blob: bytes) -> bytes:
        """
        Decrypt and decompress a stored blob back to raw bytes.

        Args:
            blob: The ``IV || ciphertext`` blob returned by the SPI.

        Returns:
            The original, uncompressed chunk bytes.
        """
        compressed = self._cipher.decrypt(blob)
        decompressor = zstd.ZstdDecompressor()
        return decompressor.decompress(compressed)
