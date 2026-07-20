"""Owner-run provisioning CLI for profiles and Garmin credentials.

Subcommands: create, list, set-garmin, delete, backfill. Never echoes or logs
credential values.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from collector import run_all
from .credentials import CredentialStore
from .profiles import ProfileNotFound, ProfileRegistry


def _resolve_id(
    registry: ProfileRegistry, *, subject: str | None, profile_id: str | None
) -> str | None:
    if profile_id is not None:
        return profile_id
    if subject is not None:
        return registry.resolve(subject)
    return None


def _cmd_create(args: argparse.Namespace) -> int:
    registry = ProfileRegistry(args.base)
    profile = registry.bind(args.subject, args.display_name)
    print(profile.id)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    registry = ProfileRegistry(args.base)
    for profile in registry.list():
        print(
            f"{profile.id}\t{profile.clerk_subject}\t"
            f"{profile.display_name or ''}\t{profile.created_at}"
        )
    return 0


def _cmd_set_garmin(args: argparse.Namespace) -> int:
    registry = ProfileRegistry(args.base)
    profile = registry.bind(args.subject)
    root = registry.data_root(profile.id)

    if args.password is not None:
        password = args.password
    elif args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass()

    CredentialStore(args.base).set_garmin(root, args.email, password)
    print(f"stored garmin credentials for {profile.id}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    registry = ProfileRegistry(args.base)
    profile_id = _resolve_id(
        registry, subject=args.subject, profile_id=args.profile_id
    )
    if profile_id is None:
        print("no matching profile", file=sys.stderr)
        return 1
    if not args.yes:
        print("refusing to delete without --yes", file=sys.stderr)
        return 1
    try:
        registry.delete(profile_id)
    except ProfileNotFound:
        print(f"no matching profile: {profile_id}", file=sys.stderr)
        return 1
    print(f"deleted profile {profile_id}")
    return 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    registry = ProfileRegistry(args.base)
    profile_id = registry.resolve(args.subject)
    if profile_id is None:
        print(f"no profile for subject {args.subject}", file=sys.stderr)
        return 1
    root = registry.data_root(profile_id)
    garmin = CredentialStore(args.base).get_garmin(root)
    if garmin is None:
        print(f"profile {profile_id} has no Garmin credentials", file=sys.stderr)
        return 1
    argv = ["--days", str(args.days)] if args.days else []
    try:
        run_all._run_profile(profile_id, root, garmin, argv)
    except Exception as exc:  # noqa: BLE001 - report and fail
        print(f"backfill failed for {profile_id}: {exc}", file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="data")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--subject", required=True)
    create.add_argument("--display-name", default=None)
    create.set_defaults(func=_cmd_create)

    listing = sub.add_parser("list")
    listing.set_defaults(func=_cmd_list)

    set_garmin = sub.add_parser("set-garmin")
    set_garmin.add_argument("--subject", required=True)
    set_garmin.add_argument("--email", required=True)
    set_garmin.add_argument("--password", default=None)
    set_garmin.add_argument("--password-stdin", action="store_true")
    set_garmin.set_defaults(func=_cmd_set_garmin)

    delete = sub.add_parser("delete")
    group = delete.add_mutually_exclusive_group(required=True)
    group.add_argument("--subject")
    group.add_argument("--profile-id")
    delete.add_argument("--yes", action="store_true")
    delete.set_defaults(func=_cmd_delete)

    backfill = sub.add_parser("backfill")
    backfill.add_argument("--subject", required=True)
    backfill.add_argument("--days", type=int, default=None)
    backfill.set_defaults(func=_cmd_backfill)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
