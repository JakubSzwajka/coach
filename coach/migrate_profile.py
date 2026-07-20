"""Migrate a flat single-profile data dir into the owner's per-profile root.

Today's layout keeps collected data directly under the base dir (``raw``,
``derived``, ``index`` ...). This moves those into the owner's isolated
profile root and binds the owner's Clerk subject in the registry. The local
stdio server keeps working because it honors ``GARMIN_COACH_DATA_DIR`` — after
migration the owner points that env var at the printed profile root.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .profiles import ProfileRegistry

_MIGRATABLE = ("raw", "derived", "index", "logs", "app", "garmin-tokens")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-subject", required=True)
    parser.add_argument("--base", default="data")
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    base = Path(args.base)
    registry = ProfileRegistry(base)

    present = [name for name in _MIGRATABLE if (base / name).exists()]

    if args.dry_run:
        existing_id = registry.resolve(args.owner_subject)
        if existing_id is None:
            print(f"[dry-run] would bind {args.owner_subject} to a new profile id")
            root = base / "profiles" / "<new-id>"
        else:
            root = registry.data_root(existing_id)
            print(f"[dry-run] {args.owner_subject} already bound to {existing_id}")
        for name in present:
            print(f"[dry-run] would move {base / name} -> {root / name}")
        return 0

    profile = registry.bind(args.owner_subject, args.display_name)
    root = registry.data_root(profile.id)
    root.mkdir(parents=True, exist_ok=True)

    for name in present:
        src = base / name
        target = root / name
        if target.exists():
            print(f"already migrated: {target} — skipping")
            continue
        shutil.move(str(src), str(target))
        print(f"moved {src} -> {target}")

    print(f"profile id: {profile.id}")
    print(f"Local server: set GARMIN_COACH_DATA_DIR={root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
