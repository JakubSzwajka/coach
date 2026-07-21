"""Versioned authenticated-ciphertext envelope for PostgreSQL integration blobs."""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.fernet import Fernet

_PREFIX = b"gc1:"


@dataclass(frozen=True, slots=True, init=False)
class EncryptedBlob:
    """Opaque, versioned Fernet ciphertext.

    Key material is supplied by the caller and is never stored in this object or
    PostgreSQL. Plain bytes cannot be constructed as an `EncryptedBlob` through
    the public interface.
    """

    _envelope: bytes

    def __init__(self, envelope: bytes) -> None:
        raise TypeError("use EncryptedBlob.encrypt or EncryptedBlob.from_envelope")

    @classmethod
    def _from_validated(cls, envelope: bytes) -> "EncryptedBlob":
        instance = object.__new__(cls)
        object.__setattr__(instance, "_envelope", envelope)
        return instance

    @classmethod
    def encrypt(cls, plaintext: bytes, key: bytes) -> "EncryptedBlob":
        if not isinstance(plaintext, bytes):
            raise TypeError("plaintext must be bytes")
        return cls._from_validated(_PREFIX + Fernet(key).encrypt(plaintext))

    @classmethod
    def from_envelope(cls, envelope: bytes, key: bytes) -> "EncryptedBlob":
        if (
            not isinstance(envelope, bytes)
            or not envelope.startswith(_PREFIX)
            or len(envelope) < 64
        ):
            raise ValueError("invalid encrypted blob envelope")
        Fernet(key).decrypt(envelope[len(_PREFIX) :])
        return cls._from_validated(envelope)

    @property
    def envelope(self) -> bytes:
        return self._envelope

    def decrypt(self, key: bytes) -> bytes:
        return Fernet(key).decrypt(self._envelope[len(_PREFIX) :])

    def __repr__(self) -> str:
        return "EncryptedBlob(<redacted>)"
