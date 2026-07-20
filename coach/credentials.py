from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialError(RuntimeError):
    """Raised when stored credentials cannot be decrypted (wrong key / corrupt data)."""


class CredentialStore:
    """Fernet-based store for per-profile Garmin secrets."""

    def __init__(self, base_dir: Path | str) -> None:
        self._base_dir = Path(base_dir)
        self._fernet: Fernet | None = None

    def _resolve_key(self) -> bytes:
        env_key = os.environ.get("GARMIN_COACH_SECRET_KEY")
        if env_key:
            return env_key.encode("ascii")
        key_path = self._base_dir / "secret.key"
        if key_path.exists():
            return key_path.read_bytes()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(self._resolve_key())
        return self._fernet

    @staticmethod
    def _garmin_path(profile_root: Path) -> Path:
        return profile_root / "secrets" / "garmin.enc"

    def set_garmin(self, profile_root: Path, email: str, password: str) -> None:
        plaintext = json.dumps({"email": email, "password": password}).encode("utf-8")
        ciphertext = self._get_fernet().encrypt(plaintext)
        path = self._garmin_path(profile_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ciphertext)
        finally:
            os.close(fd)

    def get_garmin(self, profile_root: Path) -> dict | None:
        path = self._garmin_path(profile_root)
        if not path.exists():
            return None
        ciphertext = path.read_bytes()
        try:
            plaintext = self._get_fernet().decrypt(ciphertext)
        except InvalidToken as exc:
            raise CredentialError("failed to decrypt Garmin credentials") from exc
        data = json.loads(plaintext.decode("utf-8"))
        return {"email": data["email"], "password": data["password"]}

    def delete_garmin(self, profile_root: Path) -> None:
        path = self._garmin_path(profile_root)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
