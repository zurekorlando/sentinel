"""
Storage Provider Interface (SPI) — Module 4
Abstract base class that every storage backend must implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ChunkMeta:
    """Minimal metadata returned by the storage layer when listing chunks."""
    hash_id: str
    size: int


class IStorageProvider(ABC):
    """
    Contract for all storage backends (local filesystem, S3, SFTP, …).

    Every method is synchronous and raises on unrecoverable errors.
    Transient network errors should be retried internally by the
    implementation before propagating.
    """

    # ------------------------------------------------------------------ #
    #  Core CRUD                                                           #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def upload_chunk(self, data: bytes, hash_id: str) -> None:
        """
        Persist *data* under the given *hash_id* key.

        Implementations must be idempotent: uploading a chunk that already
        exists is a no-op (not an error).

        Args:
            data:    Raw encrypted+compressed chunk bytes.
            hash_id: SHA-256 hex digest used as the storage key.
        """
        ...

    @abstractmethod
    def download_chunk(self, hash_id: str) -> bytes:
        """
        Retrieve the chunk identified by *hash_id*.

        Raises:
            KeyError: chunk does not exist in the backend.
        """
        ...

    @abstractmethod
    def exists(self, hash_id: str) -> bool:
        """Return True if the chunk is already present in the backend."""
        ...

    @abstractmethod
    def delete_chunk(self, hash_id: str) -> None:
        """
        Permanently remove the chunk from the backend.

        Must be a no-op (not raise) when the chunk does not exist,
        to allow safe retries from the Garbage Collector.
        """
        ...

    # ------------------------------------------------------------------ #
    #  Bulk / utility                                                      #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def list_chunks(self, prefix: str = "") -> Iterator[ChunkMeta]:
        """
        Yield every stored chunk whose hash_id starts with *prefix*.

        Used by the Scrubber (M&M module) and by disaster-recovery tooling.
        Implementations may return results in any order.
        """
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """
        Verify that the backend is reachable and writable.

        Returns True on success, False (or raises) otherwise.
        """
        ...

    # ------------------------------------------------------------------ #
    #  Optional helpers (override for efficiency)                          #
    # ------------------------------------------------------------------ #

    def upload_catalog(self, data: bytes, name: str) -> None:
        """
        Upload an encrypted catalog snapshot to the backend.

        Default implementation reuses *upload_chunk* with a namespaced key.
        Override for backends that separate catalog storage (e.g. a dedicated
        S3 prefix or database row).
        """
        self.upload_chunk(data, f"catalog/{name}")

    def download_catalog(self, name: str) -> bytes:
        """
        Retrieve an encrypted catalog snapshot from the backend.

        Default implementation reuses *download_chunk*.
        """
        return self.download_chunk(f"catalog/{name}")
