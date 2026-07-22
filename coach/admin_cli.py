"""Removed pre-cutover file-backed profile administration CLI.

Profiles, source connections, credentials, and collection jobs now live behind
CoachApplication. Use the signed-in Connect/Refresh routes or the explicitly
configured PostgreSQL collector entry point.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    raise RuntimeError("the file-backed admin CLI is disabled after PostgreSQL cutover")


if __name__ == "__main__":
    raise SystemExit(main())
