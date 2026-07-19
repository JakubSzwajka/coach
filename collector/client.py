"""Authenticated Garmin client: .env loading, token cache, 429 backoff.

Credential login is rare and rate-limited (Garmin returns 429 on the mobile
strategy). Once tokens are cached in ~/.garminconnect, runs are fast and skip
that path — so a cron job should almost always resume from cache.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from garminconnect import Garmin

TOKENSTORE = os.path.expanduser(os.getenv("GARMINTOKENS", "~/.garminconnect"))
ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> None:
    """Minimal .env loader (KEY="value" per line). No external dependency."""
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def connect(max_retries: int = 4) -> Garmin:
    """Return a logged-in client, preferring cached tokens.

    Retries credential login with exponential backoff when Garmin rate-limits
    (429). Raises the last exception if every attempt fails.
    """
    load_env()
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            g = Garmin(
                email,
                password,
                prompt_mfa=lambda: input("MFA code: ").strip(),
            )
            g.login(TOKENSTORE)  # cache first, else credentials; persists tokens
            return g
        except Exception as exc:  # noqa: BLE001 - decide on message
            last_exc = exc
            msg = str(exc).lower()
            if "429" in msg or "rate" in msg or "too many" in msg:
                wait = min(60, 5 * 2**attempt)
                print(f"Rate limited (attempt {attempt + 1}); backing off {wait}s")
                time.sleep(wait)
                continue
            raise
    assert last_exc is not None
    raise last_exc
