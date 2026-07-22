"""Compatibility command for the PostgreSQL-authoritative collector.

Profile enumeration is intentionally owned by durable collection jobs. This
entry point runs the one actor explicitly configured for the process and never
walks a data directory or credential registry.
"""

from __future__ import annotations

from .collect import main


if __name__ == "__main__":
    raise SystemExit(main())
