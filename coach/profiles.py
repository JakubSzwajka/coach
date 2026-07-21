"""Profile registry: isolated per-profile data roots keyed by Clerk subject.

Multi-profile support lives entirely in this module and the request-scoped root
in ``coach.mcp_server``. Each profile owns a directory under ``<base>/profiles/``
and is bound one-to-one to a Clerk ``sub``. The local stdio server does not use
this registry; it operates on the flat owner root for backward compatibility
until the migration lands.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_VERSION = 1
_REGISTRY_NAME = "registry.json"
_PROFILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ProfileError(RuntimeError):
    """Base for profile-registry failures."""


class ProfileNotFound(ProfileError):
    """No profile exists for the requested identity."""


class RegistryIntegrityError(ProfileError):
    """The stored registry does not satisfy the expected contract."""


@dataclass(frozen=True, slots=True)
class Profile:
    """One athlete profile bound to a single Clerk subject."""

    id: str
    clerk_subject: str
    display_name: str | None
    created_at: str


class ProfileRegistry:
    """Bind Clerk subjects to opaque profile ids and their data roots."""

    def __init__(self, base_dir: Path | str) -> None:
        self._base = Path(base_dir)
        self._profiles_dir = self._base / "profiles"
        self._registry_path = self._profiles_dir / _REGISTRY_NAME

    def data_root(self, profile_id: str) -> Path:
        """Return the isolated data root for a profile id."""
        if not _PROFILE_ID_RE.match(profile_id):
            raise ProfileError(f"invalid profile id: {profile_id!r}")
        return self._profiles_dir / profile_id

    def list(self) -> list[Profile]:
        """Return every registered profile, oldest first (creation order)."""
        rows = self._load()
        ordered = sorted(rows.items(), key=lambda item: item[1].get("seq", 0))
        return [self._to_profile(pid, row) for pid, row in ordered]

    def get(self, profile_id: str) -> Profile:
        """Return one profile by id, or raise ``ProfileNotFound``."""
        rows = self._load()
        row = rows.get(profile_id)
        if row is None:
            raise ProfileNotFound(f"profile {profile_id} does not exist")
        return self._to_profile(profile_id, row)

    def resolve(self, clerk_subject: str) -> str | None:
        """Return the profile id bound to a Clerk subject, or ``None``."""
        for pid, row in self._load().items():
            if row.get("clerk_subject") == clerk_subject:
                return pid
        return None

    def bind(self, clerk_subject: str, display_name: str | None = None) -> Profile:
        """Return the subject's profile, creating and binding one if unseen.

        Idempotent: a subject that already has a profile keeps it. This is the
        auto-bind-on-first-login path; a display name only fills a blank one.
        """
        if not clerk_subject or not isinstance(clerk_subject, str):
            raise ProfileError("clerk_subject is required")
        with self._exclusive_write():
            rows = self._load()
            for pid, row in rows.items():
                if row.get("clerk_subject") == clerk_subject:
                    if display_name and not row.get("display_name"):
                        row["display_name"] = display_name
                        self._save(rows)
                    return self._to_profile(pid, row)

            profile_id = secrets.token_hex(16)
            next_seq = 1 + max(
                (row.get("seq", 0) for row in rows.values()), default=0
            )
            rows[profile_id] = {
                "clerk_subject": clerk_subject,
                "display_name": display_name,
                "created_at": _utc_now(),
                "seq": next_seq,
            }
            self._save(rows)
            return self._to_profile(profile_id, rows[profile_id])

    def delete(self, profile_id: str) -> None:
        """Purge a profile: remove its data root and its registry row."""
        with self._exclusive_write():
            rows = self._load()
            if profile_id not in rows:
                raise ProfileNotFound(f"profile {profile_id} does not exist")
            shutil.rmtree(self.data_root(profile_id), ignore_errors=True)
            del rows[profile_id]
            self._save(rows)

    @contextmanager
    def _exclusive_write(self):
        """Serialize registry read-modify-write cycles across local processes."""
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._profiles_dir / "registry.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with os.fdopen(descriptor, "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield
        finally:
            # Closing the descriptor releases the advisory lock.
            pass

    def _load(self) -> dict[str, dict]:
        try:
            with self._registry_path.open(encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryIntegrityError("profile registry is unreadable") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != _SCHEMA_VERSION
            or not isinstance(document.get("profiles"), dict)
        ):
            raise RegistryIntegrityError("unsupported profile registry shape")
        return document["profiles"]

    def _save(self, rows: dict[str, dict]) -> None:
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        document = {"schema_version": _SCHEMA_VERSION, "profiles": rows}
        tmp = self._registry_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
        os.replace(tmp, self._registry_path)

    @staticmethod
    def _to_profile(profile_id: str, row: dict) -> Profile:
        subject = row.get("clerk_subject")
        if not isinstance(subject, str) or not subject:
            raise RegistryIntegrityError(
                f"profile {profile_id} is missing clerk_subject"
            )
        return Profile(
            id=profile_id,
            clerk_subject=subject,
            display_name=row.get("display_name"),
            created_at=row.get("created_at", ""),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
