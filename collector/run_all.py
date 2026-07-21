"""Serial, failure-isolated multi-profile collector runner.

Each profile is collected in its own subprocess so the collector's
module-global paths (``collector.store``) are cleanly isolated per profile.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from collections.abc import Callable

from coach.credentials import CredentialStore
from coach.profiles import ProfileRegistry

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_profile(
    profile_id: str,
    root: Path,
    garmin: dict,
    argv: list[str],
    progress: Callable[[str], None] | None = None,
) -> None:
    """Collect one profile in an isolated subprocess."""
    env = os.environ.copy()
    env.update(
        {
            "GARMIN_COACH_DATA_DIR": str(root),
            "GARMIN_EMAIL": garmin["email"],
            "GARMIN_PASSWORD": garmin["password"],
            "GARMINTOKENS": str(root / "garmin-tokens"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    command = [sys.executable, "-m", "collector.collect", *argv]
    if progress is None:
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            env=env,
            check=True,
            cwd=str(_REPO_ROOT),
        )
        return

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=str(_REPO_ROOT),
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        progress(line.rstrip("\n"))
    return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)


def main(argv: list[str] | None = None) -> int:
    base = Path(os.environ.get("GARMIN_COACH_DATA_DIR", "data"))
    registry = ProfileRegistry(base)
    creds = CredentialStore(base)

    exit_code = 0
    for profile in registry.list():
        root = registry.data_root(profile.id)
        garmin = creds.get_garmin(root)
        if garmin is None:
            print(f"profile {profile.id} has no Garmin credentials — skipping")
            continue
        try:
            _run_profile(profile.id, root, garmin, argv or [])
        except Exception as exc:  # noqa: BLE001 - isolate one profile's failure
            exit_code = 1
            print(f"profile {profile.id} collection failed: {exc}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
