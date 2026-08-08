# Synthetic file-store import rehearsal

`coach.postgres.file_import` is one-shot maintenance tooling for a frozen legacy
store. It is not imported by a runtime producer or consumer, has no default
source or database, never loads dotenv, and does not provide file fallback.
Every invocation requires an absolute `--source-root`, an explicit
migration-owner `--database-url`, and the Clerk issuer that owns the legacy
registry bindings. The ordinary application role is intentionally rejected.
Import additionally requires an absolute `--encryption-key-file`.

This procedure is authorized **only for disposable synthetic fixtures**. It
does not authorize reading or importing private files, Profile or Clerk
identifiers, source payloads, health values, credentials, token caches, or any
live service. A private-data cutover requires a separate owner gate.

## Import unit and deterministic conflicts

One Profile is one PostgreSQL transaction. The transaction contains its
registry binding, deterministic Garmin source connection, every source capture
actually present, typed current projections, App Records and all available Plan
Revisions, operational state, wrapped integration material, and immutable
`file_import_units` evidence. If any part fails, that Profile rolls back as a
unit; earlier Profile units remain committed and a rerun resumes safely.

The importer preserves App UUIDs, revision numbers, timestamps, ownership,
Plan Revision snapshots, Goal Event references, explicit fulfilment matches,
provenance, and `null`/unknown values. It maps collected activity matches by
the source relationship without exposing that identity. File raw paths contain
only the latest payload the file collector retained, so the importer records
only those payloads and marks their provenance `latest_available_only`; it never
fabricates overwritten source history.

A completed unit is immutable. The same manifest and importer version are a
no-op followed by reconciliation. A changed frozen source, conflicting binding,
identity, encrypted-material shape, or schema revision fails closed with a safe
code; it is never silently merged. PostgreSQL constraints remain authoritative.

Legacy `garmin.enc` bytes are not decrypted. They are wrapped as opaque
ciphertext under the target key and the source is marked `needs_reconnect`.
Token-cache files are packaged and encrypted without displaying their contents.
The maintenance report never emits payloads, identifiers, actor/source/record
ids, health values, credentials, tokens, paths, DSNs, or personal data—only
categories, counts, and safe codes.

## Exact staged flow

Use fresh placeholders and disposable PostgreSQL. Do not put real data or
secrets in the repository.

```bash
SOURCE_ROOT=/absolute/path/to/disposable-synthetic-store
DATABASE_URL='postgresql://synthetic_admin:placeholder@127.0.0.1:5432/synthetic_import'
CLERK_ISSUER='https://identity.example.test'
TARGET_KEY_FILE=/absolute/path/to/disposable-target.key

.venv/bin/python -m coach.postgres.file_import inventory \
  --source-root "$SOURCE_ROOT" --database-url "$DATABASE_URL" \
  --clerk-issuer "$CLERK_ISSUER"
```

1. **Inventory** — with writers already absent from the disposable fixture,
   require a clean inventory. Review only sanitized categories/counts; do not
   open fixture payloads as part of report review.
2. **Import** — apply migrations to the disposable database, then run:

   ```bash
   .venv/bin/python -m coach.postgres.file_import import \
     --source-root "$SOURCE_ROOT" --database-url "$DATABASE_URL" \
     --clerk-issuer "$CLERK_ISSUER" \
     --encryption-key-file "$TARGET_KEY_FILE"
   ```

3. **Rerun** — run the exact import command again. It must succeed with unchanged
   counts and no extra captures, App Records, Plan Revisions, jobs, or evidence.
4. **Shadow** — compare source and target internally:

   ```bash
   .venv/bin/python -m coach.postgres.file_import shadow \
     --source-root "$SOURCE_ROOT" --database-url "$DATABASE_URL" \
     --clerk-issuer "$CLERK_ISSUER"
   ```

   The report must be `ok`. Seeded rehearsal copies must separately prove gaps,
   duplicate identities, stale App heads, canonical projection mismatches,
   missing/bad references, cross-Profile references, and import drift produce
   only sanitized findings.
5. **Writer freeze** — for a future separately authorized cutover, stop every
   file collector and App Record writer, reject an active/running legacy job,
   and freeze the source before taking the final inventory. Do not dual-write.
6. **Verify** — repeat inventory/import/rerun/shadow against the frozen copy,
   verify PostgreSQL constraints and transaction rollback/failure recovery, and
   keep every runtime consumer on its current authority until the owner gate.
7. **Switch** — only after explicit private-data authorization and a clean final
   verification, switch all producers and consumers together to PostgreSQL.
   There is no partial switch, dual-write period, or silent file fallback.
8. **Rollback** — if verification or the coordinated switch fails, stop the
   PostgreSQL writers before returning all producers and consumers together to
   the unchanged frozen file authority. Never merge writes from both sides.
   The separate backup/restore rehearsal remains required before any private
   cutover; this importer does not perform or authorize it.

A nonzero command is a stop condition. Preserve the frozen input, correct the
synthetic fixture or disposable target deliberately, and restart at inventory;
never bypass an issue category or edit immutable import evidence.
