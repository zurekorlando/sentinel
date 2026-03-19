"""
Content-Defined Chunking (CDC) using Rabin Fingerprinting — DPE sub-module.

Algorithm
---------
We maintain a 64-byte sliding window over the byte stream.  At each position
we compute a rolling polynomial hash in GF(2^64) using the irreducible
polynomial POLY.  A chunk boundary is emitted whenever:

    (fingerprint & MASK) == BOUNDARY

where MASK = avg_size - 1 (requires avg_size to be a power-of-two).

The boundary condition fires on average once every avg_size bytes, giving
an expected chunk size equal to avg_size.  Hard min/max limits prevent
pathological corner-cases (repeated-byte sequences, etc.).

Tables
------
Two 256-entry lookup tables accelerate the inner loop:

  mod_table[b]  – the XOR correction when the top byte of the 64-bit
                  fingerprint is b, i.e. b * x^64 mod POLY.

  out_table[b]  – the XOR correction for byte b *leaving* the window after
                  WINDOW_SIZE slides, i.e. b * x^(8·WINDOW_SIZE) mod POLY.

Both are computed once at import time.

Chunk sizes
-----------
  avg   64 KB  (target)
  min   32 KB  (hard floor — window must fill before any split)
  max  128 KB  (hard ceiling — forces a split regardless of fingerprint)
"""

from __future__ import annotations

import io
from typing import Generator

# ------------------------------------------------------------------ #
#  Polynomial and window configuration                                 #
# ------------------------------------------------------------------ #
POLY: int = 0x3DA3358B4DC173          # Restic-compatible irreducible poly
WINDOW_SIZE: int = 64                 # bytes in the sliding window
_MASK64: int = (1 << 64) - 1

AVG_CHUNK: int = 64 * 1024
MIN_CHUNK: int = 32 * 1024
MAX_CHUNK: int = 128 * 1024
_BOUNDARY: int = 0                    # fingerprint & MASK == _BOUNDARY → split
_CDC_MASK: int = AVG_CHUNK - 1       # works because AVG_CHUNK is a power of 2


# ------------------------------------------------------------------ #
#  Table generation                                                    #
# ------------------------------------------------------------------ #

def _build_tables() -> tuple[list[int], list[int]]:
    """
    Build mod_table and out_table for the Rabin polynomial rolling hash.

    mod_table[t] = t * x^64 mod POLY  (reduction for the overflowing top byte)
    out_table[b] = b * x^(8·WINDOW_SIZE) mod POLY  (outgoing byte correction)
    """

    def _gf_mul_poly(value: int, shifts: int) -> int:
        """Multiply *value* (a byte, 0-255) by x^shifts in GF(2^64)/POLY."""
        h = value
        for _ in range(shifts):
            if h >> 63:
                h = ((h << 1) ^ POLY) & _MASK64
            else:
                h = (h << 1) & _MASK64
        return h

    # mod_table[t] = t * x^64 mod POLY
    mod_table = [_gf_mul_poly(t, 64) for t in range(256)]

    # out_table[b]: start from b, multiply by x^8 exactly WINDOW_SIZE times
    # using mod_table for each step (8 bits at a time).
    out_table: list[int] = []
    for b in range(256):
        h = b
        for _ in range(WINDOW_SIZE):
            top = (h >> 56) & 0xFF
            h = ((h << 8) & _MASK64) ^ mod_table[top]
        out_table.append(h)

    return mod_table, out_table


_MOD_TABLE, _OUT_TABLE = _build_tables()


# ------------------------------------------------------------------ #
#  Chunker                                                             #
# ------------------------------------------------------------------ #

class RabinChunker:
    """
    Content-Defined Chunker using a Rabin polynomial rolling hash.

    Thread-safe: all mutable state lives in the generator frame, not on self.
    """

    def __init__(
        self,
        avg_size: int = AVG_CHUNK,
        min_size: int = MIN_CHUNK,
        max_size: int = MAX_CHUNK,
    ) -> None:
        if avg_size & (avg_size - 1):
            raise ValueError("avg_size must be a power of two")
        self.avg_size = avg_size
        self.min_size = min_size
        self.max_size = max_size
        self._mask = avg_size - 1

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def chunkify(self, data: bytes) -> Generator[bytes, None, None]:
        """
        Split an in-memory *bytes* object into variable-length chunks.

        Suitable for small files or pre-loaded buffers.  For large files
        use :meth:`chunkify_stream` instead.
        """
        yield from self._chunkify_stream(io.BytesIO(data))

    def chunkify_stream(
        self,
        stream: io.RawIOBase | io.BufferedIOBase,
        read_size: int = 4 * 1024 * 1024,
    ) -> Generator[bytes, None, None]:
        """
        Split a file-like object into variable-length chunks.

        Reads the stream in *read_size* blocks; only the current chunk
        accumulation buffer and the 64-byte window are kept in RAM.

        Args:
            stream:    Any readable binary stream.
            read_size: Internal read block size (default 4 MB).
        """
        yield from self._chunkify_stream(stream, read_size)

    # ------------------------------------------------------------------ #
    #  Core implementation                                                 #
    # ------------------------------------------------------------------ #

    def _chunkify_stream(
        self,
        stream: io.IOBase,
        read_size: int = 4 * 1024 * 1024,
    ) -> Generator[bytes, None, None]:
        """
        Core rolling-hash loop.

        State:
            window     – circular buffer of WINDOW_SIZE bytes
            w_pos      – next write position in the circular buffer
            fingerprint – current Rabin hash of the window
            chunk      – accumulation buffer for the current chunk
        """
        window = bytearray(WINDOW_SIZE)
        w_pos = 0
        fingerprint = 0
        chunk = bytearray()

        min_size = self.min_size
        max_size = self.max_size
        mask = self._mask
        mod_table = _MOD_TABLE
        out_table = _OUT_TABLE

        while True:
            block = stream.read(read_size)
            if not block:
                break

            for byte in block:
                # ---- slide window ----------------------------------------
                out_byte = window[w_pos]
                window[w_pos] = byte
                w_pos = (w_pos + 1) % WINDOW_SIZE

                # ---- update fingerprint ------------------------------------
                top = (fingerprint >> 56) & 0xFF
                fingerprint = (
                    ((fingerprint << 8) & _MASK64)
                    ^ byte
                    ^ mod_table[top]
                    ^ out_table[out_byte]
                )

                chunk.append(byte)
                chunk_len = len(chunk)

                # ---- check split condition ---------------------------------
                if chunk_len >= min_size:
                    if (fingerprint & mask) == _BOUNDARY or chunk_len >= max_size:
                        yield bytes(chunk)
                        chunk = bytearray()
                        window = bytearray(WINDOW_SIZE)
                        w_pos = 0
                        fingerprint = 0

        if chunk:
            yield bytes(chunk)
