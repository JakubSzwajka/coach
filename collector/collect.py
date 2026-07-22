#!/usr/bin/env python3
"""Run a PostgreSQL-authoritative Garmin collection job.

This entry point never reads or writes the legacy data directory. Parsed source
responses flow in memory to CoachApplication.ingest, while durable job, health,
checkpoint, credential, and token state is updated by CoachApplication.execute.
"""

from __future__ import annotations

import argparse
import os
import sys
from uuid import uuid4

from dotenv import load_dotenv

from coach.application import (
    ClerkActor,
    CoachApplication,
    ConfigureSourceCredentials,
    EnsureProfile,
    EnsureSourceConnection,
    GetSourceConnectionStatus,
    RequestCollection,
    RunCollectionJob,
    SecretBundle,
)
from coach.postgres import DatabaseSettings

from .postgres_adapter import GarminCollectionAdapter, credential_bundle


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=None, help="initial-sync history (1-730 days)")
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="compatibility flag; profile captures are refreshed every job",
    )
    parser.add_argument("--reconnect", action="store_true", help="replace stored Garmin credentials")
    parser.add_argument("--date", help=argparse.SUPPRESS)
    parser.add_argument("--activities-limit", type=int, default=100, help=argparse.SUPPRESS)
    parser.add_argument("--no-activities", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.date or arguments.no_activities or arguments.force or arguments.activities_limit != 100:
        parser.error("legacy file-collector selection flags are not supported after cutover")
    if arguments.days is not None and not 1 <= arguments.days <= 730:
        parser.error("--days must be between 1 and 730")

    if os.environ.get("GARMIN_COACH_DISABLE_DOTENV") != "1":
        load_dotenv(override=False)
    actor = ClerkActor(
        _required("GARMIN_COACH_LOCAL_ACTOR_ISSUER"),
        _required("GARMIN_COACH_LOCAL_ACTOR_SUBJECT"),
    )
    application = CoachApplication(
        DatabaseSettings.from_env(),
        collection_adapter=GarminCollectionAdapter(),
        encryption_key=_required("GARMIN_COACH_ENCRYPTION_KEY").encode("ascii"),
    )
    application.execute(actor, EnsureProfile())
    source = application.execute(
        actor, EnsureSourceConnection("garmin", "primary")
    )
    status = application.read(actor, GetSourceConnectionStatus("garmin"))
    if status is None:
        raise RuntimeError("Garmin source connection is unavailable")
    if arguments.reconnect or not status.credentials_stored:
        email = _required("GARMIN_EMAIL")
        password = _required("GARMIN_PASSWORD")
        application.execute(
            actor,
            ConfigureSourceCredentials(
                source,
                SecretBundle(
                    credential_bundle(
                        email,
                        password,
                        initial_days=arguments.days or 365,
                    )
                ),
            ),
        )
    kind = "initial_sync" if arguments.days is not None else "incremental"
    requested = application.execute(
        actor,
        RequestCollection(source, f"collector:{uuid4().hex}", kind),
    )
    completed = application.execute(actor, RunCollectionJob(requested.job))
    print(f"collection state={completed.state} kind={completed.kind}")
    return 0 if completed.state == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
