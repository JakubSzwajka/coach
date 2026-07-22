# Garmin collector

The runtime collector reads Garmin and writes only through
`CoachApplication.ingest/execute`. PostgreSQL owns immutable parsed captures,
canonical Training Sessions and observations, durable jobs/checkpoints/health,
and encrypted credential/token bundles. There is no raw/derived file write,
dual-write, cursor file, health file, log file, or file fallback.

`collector/postgres_adapter.py` keeps provider responses in memory and returns a
`CollectedBatch`. The application binds the job-owned opaque source capability,
atomically ingests that batch, and advances job health/checkpoint state.
`garminconnect` token files exist only in the private temporary directory issued
by `CoachApplication`; encrypted rotated tokens are persisted immediately and
the directory is removed on success or failure.

The supported operator path is the signed-in **Connect Garmin** / **Refresh
Garmin** flow in the one Docker Compose stack documented in the root README.
The direct CLI is for an explicitly configured deployment actor:

```bash
GARMIN_COACH_DATABASE_URL='postgresql://coach:<app-password>@127.0.0.1:5432/coach' \
GARMIN_COACH_LOCAL_ACTOR_ISSUER='https://local.example.test' \
GARMIN_COACH_LOCAL_ACTOR_SUBJECT='local-owner-placeholder' \
GARMIN_COACH_ENCRYPTION_KEY='<fernet-key>' \
GARMIN_EMAIL='you@example.com' \
GARMIN_PASSWORD='<garmin-password>' \
python -m collector.collect --days 30 --reconnect
```

Incremental jobs use cached encrypted tokens and omit Garmin credentials. Garmin
remains read-only. Repairable authentication or MFA transitions the source to
`needs_reconnect`; provider/rate-limit failures preserve prior successful data
and freshness rather than advancing a checkpoint.

Test-only frozen file adapters under `tests/legacy_file_*` keep historical
portable behavior contracts executable. No producer or consumer entry point
imports them. Frozen private files remain owner-gated migration/rollback inputs,
not runtime state.
