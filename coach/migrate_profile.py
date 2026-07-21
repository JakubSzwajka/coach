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
import sys
from pathlib import Path

from .profiles import ProfileRegistry

MIGRATABLE = ("raw", "derived", "index", "logs", "app", "garmin-tokens")


class MigrationConflict(RuntimeError):
    """Both the legacy source and profile target exist for one collection."""


def migrate_flat_data(base: Path, profile_root: Path) -> list[tuple[Path, Path]]:
    """Move legacy flat-store directories into one profile root.

    Repeating a completed migration is safe. A split source/target collection
    is rejected for explicit reconciliation instead of being silently skipped.
    The caller decides which profile may claim flat data.
    """
    conflicts = [
        name
        for name in MIGRATABLE
        if (base / name).exists() and (profile_root / name).exists()
    ]
    if conflicts:
        raise MigrationConflict(
            "legacy and profile collections both exist: " + ", ".join(conflicts)
        )

    moved: list[tuple[Path, Path]] = []
    profile_root.mkdir(parents=True, exist_ok=True)
    for name in MIGRATABLE:
        source = base / name
        target = profile_root / name
        if not source.exists() or target.exists():
            continue
        shutil.move(str(source), str(target))
        moved.append((source, target))
    return moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-subject", required=True)
    parser.add_argument("--base", default="data")
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    base = Path(args.base)
    registry = ProfileRegistry(base)

    present = [name for name in MIGRATABLE if (base / name).exists()]

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
    try:
        moved = migrate_flat_data(base, root)
    except MigrationConflict as exc:
        print(f"migration conflict: {exc}", file=sys.stderr)
        return 2
    moved_names = {source.name for source, _ in moved}
    for name in present:
        target = root / name
        if name in moved_names:
            print(f"moved {base / name} -> {target}")
        else:
            print(f"already migrated: {target} — skipping")

    print(f"profile id: {profile.id}")
    print(f"Local server: set GARMIN_COACH_DATA_DIR={root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
