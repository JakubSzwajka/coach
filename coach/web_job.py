"""Removed pre-cutover file-backed web worker.

Connect, Refresh, and Status now use the persistent private HTTP adapter and
durable CoachApplication collection jobs. This module intentionally has no
compatibility fallback.
"""

from __future__ import annotations


def main() -> int:
    raise RuntimeError(
        "coach.web_job is disabled; run coach.http_adapter and use the signed-in web routes"
    )


if __name__ == "__main__":
    raise SystemExit(main())
