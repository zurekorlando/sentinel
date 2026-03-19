"""
Cryptography primitives for Sentinel — DPE sub-module.

Key Derivation
--------------
Argon2id (RFC 9106 recommended parameters) derives a 256-bit master key from
the user's passphrase and a random salt stored with the job configuration.

Per-Chunk Encryption
--------------------
AES-256-GCM provides authenticated encryption.  Each chunk receives a unique
12-byte random IV (nonce).  The 16-byte GCM authentication tag is appended by
the `cryptography` library automatically and verified on decryption.

Wire format of an encrypted chunk blob:
    [ 12 bytes IV ][ N bytes ciphertext + 16 bytes GCM tag ]

Zero-Knowledge guarantee
------------------------
The raw key is never written to disk.  The salt (32 bytes) is persisted in
the job config or catalog so the key can be re-derived at restore time.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ------------------------------------------------------------------ #
#  Argon2id parameters                                                 #
# ------------------------------------------------------------------ #
_ARGON2_TIME_COST = 3
_ARGON2_MEMORY_KB = 65_536   # 64 MB
_ARGON2_PARALLELISM = 4
_ARGON2_HASH_LEN = 32        # 256-bit output
_SALT_LEN = 32               # bytes

# ------------------------------------------------------------------ #
#  AES-GCM parameters                                                  #
# ------------------------------------------------------------------ #
_IV_LEN = 12                 # 96-bit nonce (NIST recommended)
_TAG_LEN = 16                # 128-bit authentication tag


# ------------------------------------------------------------------ #
#  Key derivation                                                      #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class DerivedKey:
    """A 256-bit key together with the salt needed to re-derive it."""
    key: bytes    # 32 bytes
    salt: bytes   # _SALT_LEN bytes


def derive_key(passphrase: str, salt: bytes | None = None) -> DerivedKey:
    """
    Derive a 256-bit encryption key from *passphrase* using Argon2id.

    Args:
        passphrase: User-provided secret string.
        salt:       Optional existing salt (provide when re-deriving at
                    restore time).  A fresh random salt is generated when
                    None is passed.

    Returns:
        A :class:`DerivedKey` containing the 32-byte key and its salt.
    """
    if not passphrase:
        raise ValueError("Passphrase must not be empty")

    if salt is None:
        salt = os.urandom(_SALT_LEN)
    elif len(salt) != _SALT_LEN:
        raise ValueError(f"Salt must be exactly {_SALT_LEN} bytes")

    raw_key: bytes = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_KB,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_ARGON2_HASH_LEN,
        type=Type.ID,
    )
    return DerivedKey(key=raw_key, salt=salt)


# ------------------------------------------------------------------ #
#  Per-chunk encryption                                                #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class EncryptedBlob:
    """
    The result of encrypting a single chunk.

    Attributes:
        data: ``iv || ciphertext || gcm_tag`` (ready to hand to SPI).
        iv:   The 12-byte IV extracted for storage in the catalog.
    """
    data: bytes
    iv: bytes


class ChunkCipher:
    """
    Stateless AES-256-GCM encryptor/decryptor.

    Thread-safe: the underlying AESGCM object is immutable once created and
    all mutable state lives on the stack inside each call.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("Key must be exactly 32 bytes for AES-256")
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: bytes) -> EncryptedBlob:
        """
        Encrypt *plaintext* with a freshly generated random IV.

        The returned :class:`EncryptedBlob`.data is structured as:
            [ 12-byte IV ][ len(plaintext) + 16-byte tag bytes ]

        The IV is also returned separately so the catalog can index it
        without re-parsing the blob.
        """
        iv = os.urandom(_IV_LEN)
        ciphertext = self._aesgcm.encrypt(iv, plaintext, None)
        return EncryptedBlob(data=iv + ciphertext, iv=iv)

    def decrypt(self, blob: bytes) -> bytes:
        """
        Decrypt a blob produced by :meth:`encrypt`.

        Args:
            blob: ``iv || ciphertext || tag`` byte string.

        Returns:
            Original plaintext bytes.

        Raises:
            cryptography.exceptions.InvalidTag: if the blob is corrupt or
                has been tampered with.
        """
        if len(blob) < _IV_LEN + _TAG_LEN:
            raise ValueError("Blob is too short to be a valid encrypted chunk")
        iv = blob[:_IV_LEN]
        ciphertext = blob[_IV_LEN:]
        return self._aesgcm.decrypt(iv, ciphertext, None)

    def decrypt_with_iv(self, ciphertext: bytes, iv: bytes) -> bytes:
        """
        Decrypt when the IV is stored separately (e.g. retrieved from the
        catalog rather than embedded in the blob).
        """
        return self._aesgcm.decrypt(iv, ciphertext, None)
