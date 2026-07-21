from __future__ import annotations

import unittest

from cryptography.fernet import Fernet, InvalidToken

from coach.postgres.encryption import EncryptedBlob


class EncryptedBlobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Fernet.generate_key()

    def test_round_trip_uses_a_versioned_opaque_envelope(self) -> None:
        blob = EncryptedBlob.encrypt(b"synthetic-private-material", self.key)

        self.assertTrue(blob.envelope.startswith(b"gc1:"))
        self.assertNotIn(b"synthetic-private-material", blob.envelope)
        self.assertEqual(blob.decrypt(self.key), b"synthetic-private-material")
        self.assertEqual(repr(blob), "EncryptedBlob(<redacted>)")

    def test_plain_bytes_cannot_use_the_encrypted_blob_interface(self) -> None:
        with self.assertRaises(TypeError):
            EncryptedBlob(b"synthetic-private-material")

    def test_imported_envelope_must_be_authenticated_by_the_key(self) -> None:
        blob = EncryptedBlob.encrypt(b"synthetic-private-material", self.key)

        with self.assertRaises(InvalidToken):
            EncryptedBlob.from_envelope(blob.envelope, Fernet.generate_key())
        with self.assertRaises(ValueError):
            EncryptedBlob.from_envelope(b"not-an-envelope", self.key)


if __name__ == "__main__":
    unittest.main()
