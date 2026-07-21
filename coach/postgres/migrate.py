"""Apply Garmin Coach PostgreSQL migrations.

Usage: ``python -m coach.postgres.migrate upgrade``
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv

from .config import DatabaseConfigurationError, DatabaseSettings

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(settings: DatabaseSettings) -> Config:
    """Build Alembic configuration without placing the database URL in logs."""
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_settings"] = settings
    return config


def migrate(settings: DatabaseSettings, revision: str = "head") -> None:
    """Upgrade one configured database to ``revision``."""
    command.upgrade(alembic_config(settings), revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    upgrade = subcommands.add_parser("upgrade", help="upgrade the database")
    upgrade.add_argument("revision", nargs="?", default="head")
    downgrade = subcommands.add_parser("downgrade", help="downgrade the database")
    downgrade.add_argument("revision", nargs="?", default="-1")
    subcommands.add_parser("current", help="show the current migration revision")
    arguments = parser.parse_args(argv)

    try:
        if os.environ.get("GARMIN_COACH_DISABLE_DOTENV") != "1":
            load_dotenv(_PROJECT_ROOT / ".env", override=False)
        settings = DatabaseSettings.from_env(
            "GARMIN_COACH_MIGRATION_DATABASE_URL"
        )
        if arguments.command == "upgrade":
            migrate(settings, arguments.revision)
        elif arguments.command == "downgrade":
            command.downgrade(alembic_config(settings), arguments.revision)
        else:
            command.current(alembic_config(settings), verbose=False)
    except DatabaseConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Driver and Alembic exceptions may retain connection details. Keep the
        # supported command's failure contract deliberately opaque.
        print("database migration failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
