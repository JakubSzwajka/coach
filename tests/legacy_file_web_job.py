"""Test-only frozen contract for the removed file-backed web worker.

Background Garmin connect/refresh worker invoked by the web server.

The request arrives as one JSON object on stdin so Clerk subjects and Garmin
credentials never appear in argv or logs. Progress is written to an opaque,
subject-hashed status file under the configured data root.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tests import legacy_file_run_all as run_all

from coach.credentials import CredentialStore
from coach.profiles import ProfileRegistry

_SCHEMA_VERSION = 1
_DEFAULT_BACKFILL_DAYS = 365
_MAX_BACKFILL_DAYS = 730


class JobInputError(ValueError):
    """The internal stdin request is invalid."""
class CollectionDegraded(RuntimeError):
    """The collector finished but its health contract did not report success."""




def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _job_key(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]


def _status_path(base: Path, subject: str) -> Path:
    return base / "jobs" / f"{_job_key(subject)}.json"


def _write_status(base: Path, subject: str, status: dict[str, Any]) -> None:
    directory = base / "jobs"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _status_path(base, subject)
    status = {
        "schema_version": _SCHEMA_VERSION,
        **status,
        **({"worker_pid": os.getpid()} if status.get("state") == "running" else {}),
        "updated_at": _utc_now(),
    }
    encoded = json.dumps(status, sort_keys=True).encode("utf-8")
    temporary = path.with_suffix(".json.tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _progress_reporter(
    base: Path, subject: str, *, action: str, daily_total: int
) -> Callable[[str], None]:
    daily_current = 0

    def report(line: str) -> None:
        nonlocal daily_current
        _, separator, message = line.partition("] ")
        if not separator:
            return
        if message == "authenticated":
            step = "authenticated"
            progress = None
        elif message.startswith("daily "):
            daily_current += 1
            step = "daily"
            progress = {"current": daily_current, "total": daily_total}
        elif message == "plans":
            step = "plans"
            progress = None
        elif message.startswith("activities "):
            step = "activities"
            progress = None
        elif message == "profile snapshot":
            step = "profile"
            progress = None
        elif message == "done":
            step = "finalizing"
            progress = None
        else:
            return

        status: dict[str, Any] = {
            "state": "running",
            "action": action,
            "step": step,
        }
        if progress is not None:
            status["progress"] = progress
        _write_status(base, subject, status)

    return report


def _read_request() -> dict[str, Any]:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise JobInputError("request must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise JobInputError("request must be an object")
    return payload


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise JobInputError(f"{field} is required")
    return value.strip()
def _required_secret(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise JobInputError(f"{field} is required")
    return value




def _assert_collection_success(profile_root: Path) -> None:
    try:
        health = json.loads(
            (profile_root / "index" / "collector-health.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionDegraded from exc
    if (
        not isinstance(health, dict)
        or health.get("schema_version") != 1
        or not isinstance(health.get("run"), dict)
        or health["run"].get("outcome") != "successful"
    ):
        raise CollectionDegraded


def _store_credentials(
    base: Path, profile_root: Path, email: str, password: str
) -> None:
    # The first Fernet key creation is base-wide, so serialize it across
    # different subjects; per-subject job locks alone cannot prevent the race.
    lock_path = base / "jobs" / "credentials.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        CredentialStore(base).set_garmin(profile_root, email, password)


def _run_connect(base: Path, subject: str, payload: dict[str, Any]) -> None:
    email = _required_text(payload, "email")
    password = _required_secret(payload, "password")
    days = payload.get("days", _DEFAULT_BACKFILL_DAYS)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= _MAX_BACKFILL_DAYS:
        raise JobInputError(f"days must be between 1 and {_MAX_BACKFILL_DAYS}")

    registry = ProfileRegistry(base)
    profile = registry.bind(subject)
    root = registry.data_root(profile.id)
    # Never let first-login order choose the owner of a legacy flat store.
    # Legacy migration remains an explicit, owner-authorized maintenance action.
    root.mkdir(parents=True, exist_ok=True)

    _write_status(
        base,
        subject,
        {"state": "running", "action": "connect", "step": "securing_credentials"},
    )
    _store_credentials(base, root, email, password)
    _write_status(
        base,
        subject,
        {"state": "running", "action": "connect", "step": "authenticating"},
    )
    run_all._run_profile(
        profile.id,
        root,
        {"email": email, "password": password},
        ["--days", str(days)],
        progress=_progress_reporter(
            base, subject, action="connect", daily_total=days
        ),
    )
    _assert_collection_success(root)


def _run_refresh(base: Path, subject: str) -> None:
    registry = ProfileRegistry(base)
    profile_id = registry.resolve(subject)
    if profile_id is None:
        raise JobInputError("profile is not linked")
    root = registry.data_root(profile_id)
    garmin = CredentialStore(base).get_garmin(root)
    if garmin is None:
        raise JobInputError("Garmin credentials are not configured")
    _write_status(
        base,
        subject,
        {"state": "running", "action": "refresh", "step": "authenticating"},
    )
    run_all._run_profile(
        profile_id,
        root,
        garmin,
        [],
        progress=_progress_reporter(
            base, subject, action="refresh", daily_total=2
        ),
    )
    _assert_collection_success(root)


def main() -> int:
    try:
        payload = _read_request()
        subject = _required_text(payload, "subject")
        action = payload.get("action")
        if action not in {"connect", "refresh"}:
            raise JobInputError("action must be connect or refresh")
    except JobInputError:
        return 2

    base = Path(os.environ.get("GARMIN_COACH_DATA_DIR", "data"))
    jobs = base / "jobs"
    jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = jobs / f"{_job_key(subject)}.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 3

        started_at = _utc_now()
        _write_status(
            base,
            subject,
            {"state": "running", "action": action, "step": "starting", "started_at": started_at},
        )
        try:
            if action == "connect":
                _run_connect(base, subject, payload)
            else:
                _run_refresh(base, subject)
        except CollectionDegraded:
            _write_status(
                base,
                subject,
                {
                    "state": "failed",
                    "action": action,
                    "step": "failed",
                    "error": "collection_degraded",
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                },
            )
            return 1
        except JobInputError:
            _write_status(
                base,
                subject,
                {
                    "state": "failed",
                    "action": action,
                    "step": "failed",
                    "error": "invalid_request",
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                },
            )
            return 2
        except Exception:
            _write_status(
                base,
                subject,
                {
                    "state": "failed",
                    "action": action,
                    "step": "failed",
                    "error": "collection_failed",
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                },
            )
            return 1

        _write_status(
            base,
            subject,
            {
                "state": "succeeded",
                "action": action,
                "step": "complete",
                "started_at": started_at,
                "finished_at": _utc_now(),
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
