from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from coach.credentials import CredentialError, CredentialStore

EMAIL = "runner@example.com"
PASSWORD = "s3cr3t-p@ssw0rd"


class CredentialStoreTest(unittest.TestCase):
    def _store(self) -> tuple[CredentialStore, Path]:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        profile = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return CredentialStore(base), profile

    def test_round_trip_returns_same_email_and_password(self) -> None:
        with mock.patch.dict(os.environ, {"GARMIN_COACH_SECRET_KEY": Fernet.generate_key().decode()}):
            store, profile = self._store()
            store.set_garmin(profile, EMAIL, PASSWORD)
            got = store.get_garmin(profile)
        self.assertEqual(got, {"email": EMAIL, "password": PASSWORD})

    def test_get_without_stored_credentials_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"GARMIN_COACH_SECRET_KEY": Fernet.generate_key().decode()}):
            store, profile = self._store()
            self.assertIsNone(store.get_garmin(profile))

    def test_ciphertext_does_not_contain_plaintext_password(self) -> None:
        with mock.patch.dict(os.environ, {"GARMIN_COACH_SECRET_KEY": Fernet.generate_key().decode()}):
            store, profile = self._store()
            store.set_garmin(profile, EMAIL, PASSWORD)
            blob = (profile / "secrets" / "garmin.enc").read_bytes()
        self.assertNotIn(PASSWORD.encode("utf-8"), blob)
        self.assertNotIn(EMAIL.encode("utf-8"), blob)

    def test_different_key_cannot_decrypt(self) -> None:
        with mock.patch.dict(os.environ, {"GARMIN_COACH_SECRET_KEY": Fernet.generate_key().decode()}):
            store, profile = self._store()
            store.set_garmin(profile, EMAIL, PASSWORD)
        with mock.patch.dict(os.environ, {"GARMIN_COACH_SECRET_KEY": Fernet.generate_key().decode()}):
            other = CredentialStore(Path(self.enterContext(tempfile.TemporaryDirectory())))
            with self.assertRaises(CredentialError):
                other.get_garmin(profile)

    def test_delete_removes_file_and_subsequent_get_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"GARMIN_COACH_SECRET_KEY": Fernet.generate_key().decode()}):
            store, profile = self._store()
            store.set_garmin(profile, EMAIL, PASSWORD)
            store.delete_garmin(profile)
            self.assertFalse((profile / "secrets" / "garmin.enc").exists())
            self.assertIsNone(store.get_garmin(profile))

    def test_key_file_created_with_0600_when_no_env_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GARMIN_COACH_SECRET_KEY", None)
            store, profile = self._store()
            store.set_garmin(profile, EMAIL, PASSWORD)
            key_path = store._base_dir / "secret.key"
            self.assertTrue(key_path.exists())
            mode = stat.S_IMODE(os.stat(key_path).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
