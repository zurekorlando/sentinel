"""
Local Filesystem Storage Provider.

Stores chunks as individual files under a configurable base directory.
Chunks are sharded into 256 two-character sub-directories (first two hex
digits of the hash) to avoid filesystem limits on directory entry counts.

Layout:
    <base_path>/
        ab/cdef01234…  ← chunk file (hash_id = "abcdef01234…")
        catalog/
            <name>     ← encrypted catalog snapshots
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from sentinel.spi.base import ChunkMeta, IStorageProvider


class LocalProvider(IStorageProvider):
    """Storage provider backed by the local filesystem."""

    def __init__(self, base_path: str) -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        (self.base_path / "catalog").mkdir(exist_ok=True)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _chunk_path(self, hash_id: str) -> Path:
        """
        Map a hash_id to its on-disk path.
        Uses the first 2 hex chars as a sub-directory shard.
        """
        if len(hash_id) < 3:
            raise ValueError(f"Invalid hash_id: {hash_id!r}")
        return self.base_path / hash_id[:2] / hash_id[2:]

    # ------------------------------------------------------------------ #
    #  IStorageProvider                                                    #
    # ------------------------------------------------------------------ #

    def upload_chunk(self, data: bytes, hash_id: str) -> None:
        path = self._chunk_path(hash_id)
        if path.exists():
            # Idempotent — chunk already stored, nothing to do.
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to a temp file then rename.
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_bytes(data)
            tmp.rename(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def download_chunk(self, hash_id: str) -> bytes:
        path = self._chunk_path(hash_id)
        if not path.exists():
            raise KeyError(f"Chunk not found: {hash_id}")
        return path.read_bytes()

    def exists(self, hash_id: str) -> bool:
        return self._chunk_path(hash_id).exists()

    def delete_chunk(self, hash_id: str) -> None:
        path = self._chunk_path(hash_id)
        path.unlink(missing_ok=True)
        # Remove the shard dir if it is now empty.
        try:
            path.parent.rmdir()
        except OSError:
            pass  # Not empty — that is fine.

    def list_chunks(self, prefix: str = "") -> Iterator[ChunkMeta]:
        for shard_dir in sorted(self.base_path.iterdir()):
            if shard_dir.name == "catalog" or not shard_dir.is_dir():
                continue
            for chunk_file in sorted(shard_dir.iterdir()):
                hash_id = shard_dir.name + chunk_file.name
                if not hash_id.startswith(prefix):
                    continue
                yield ChunkMeta(hash_id=hash_id, size=chunk_file.stat().st_size)

    def health_check(self) -> bool:
        try:
            probe = self.base_path / ".sentinel_probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------ #
    #  Catalog overrides                                                   #
    # ------------------------------------------------------------------ #

    def upload_catalog(self, data: bytes, name: str) -> None:
        dest = self.base_path / "catalog" / name
        tmp = dest.with_suffix(".tmp")
        try:
            tmp.write_bytes(data)
            tmp.rename(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def download_catalog(self, name: str) -> bytes:
        path = self.base_path / "catalog" / name
        if not path.exists():
            raise KeyError(f"Catalog snapshot not found: {name}")
        return path.read_bytes()

    def __repr__(self) -> str:
        return f"LocalProvider(base_path={self.base_path!r})"
