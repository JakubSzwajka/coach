# Use PostgreSQL as the sole durable runtime authority

**Status:** Accepted

## Context

Garmin Coach currently runs from profile-scoped files: parsed Garmin responses,
regenerable derived views, App Records, profile bindings, collector state, and
encrypted integration material. That implementation established the domain and
consumer contracts, but it cannot provide one transaction across ingestion,
App Record invariants, projections, tenancy, and operational state. Maintaining
both files and PostgreSQL as writable authorities would add an unrecoverable
split-brain boundary.

PostgreSQL is the approved M1 direction. This decision defines the authority,
module, tenancy, security, and migration boundaries before schema work begins.
The later cutover of private data remains a separate owner decision.

## Decision

### PostgreSQL authority

PostgreSQL is the sole durable runtime authority for:

- Profiles and Clerk identity bindings;
- provider-neutral source connections;
- encrypted Garmin credential and token bundles;
- collection jobs, runs, attempts, health, and checkpoints;
- immutable Collected Record source captures;
- typed canonical records and controlled observations;
- revisioned App Records;
- dashboard, context, trend, plan, and calendar projections.

After cutover, files may exist only as migration inputs, exports, backups,
frozen rollback material, or temporary secret hydration. There is no runtime
filesystem/PostgreSQL dual-write and no silent file fallback.

### Source captures

Each semantically distinct parsed provider response is stored as an immutable,
append-only JSONB capture with:

- a provider-neutral source identity scoped to a Profile;
- provenance and collection time;
- an optional provider/source time;
- a deterministic content hash;
- the parsed provider payload.

An identical repeat pull is idempotent. Changed content appends a capture rather
than replacing history. The current Garmin library supplies parsed objects, so
this preserves JSON values, not original HTTP bytes, lexical formatting, or key
order. Migration imports only the history still present in files and never
invents overwritten versions.

### Typed model and missing values

Identity, Training Sessions, Goal Events, Training Plans, Plan Revisions,
Planned Sessions, fulfilment, matches, and other lifecycle-bearing concepts use
typed relational entities. Extensible wellness and physiology facts use a
controlled observation model with an explicit definition, value type, unit,
time window, method, provenance, and missing semantics.

JSONB is limited to immutable provider payloads, provider-only or opaque detail,
cursor state, and privacy-safe diagnostics. Garmin Coach does not use a
universal `records(kind, payload)` table or unrestricted EAV. Missing values
remain unknown; projections do not silently interpolate them.

### Application boundary

One deep Python `CoachApplication` owns the use cases:

- `read(actor, query)` returns stable profile-scoped projections;
- `execute(actor, command)` performs validated, revision-aware App Record
  mutations;
- `ingest(profile, batch)` atomically records internal source ingestion and
  updates projections.

The module owns actor-to-Profile resolution, tenancy, validation, transactions,
source/App ownership, concurrency checks, and projection updates. Persistence
helpers remain private.

MCP is an in-process adapter over `CoachApplication`. Next.js calls a persistent
private authenticated HTTP adapter and does not query PostgreSQL tables or
reimplement authoritative Python models and validation. Browser and MCP
payloads never choose a Profile id.

### Tenancy

A Profile is the access and tenancy boundary for one athlete's record. The
current cardinality is one Clerk identity to one Profile. Profile-owned
relationships use profile-scoped keys and constraints; row-level security may
later add defence in depth. A separate Athlete entity is deferred until sharing
or multiple athletes per Profile is a real requirement.

### Garmin integration secret custody

Garmin remains read-only. Credential and token bundles are encrypted before
storage as ciphertext (`bytea`); key material stays outside PostgreSQL.

A refresh worker acquires a per-Profile database lock, prefers cached tokens,
materializes decrypted token files only inside a private temporary directory
for `garminconnect`, persists refreshed encrypted tokens immediately, performs
collection, and removes the directory on success or failure. Encrypted
email/password are an unattended reauthentication fallback. MFA or password
repair transitions the connection to `needs_reconnect` instead of retrying
indefinitely.

### Migration and cutover

M1 uses a staged one-shot maintenance cutover:

1. build an idempotent importer;
2. shadow-read and reconcile with synthetic fixtures first;
3. rehearse PostgreSQL backup/restore and frozen-file rollback;
4. stop collectors and App Record mutations;
5. back up PostgreSQL and freeze/back up files;
6. import and reconcile;
7. switch every producer and consumer;
8. retain frozen files for a bounded rollback window.

The architecture approval authorizes implementation and synthetic rehearsal. It
does **not** authorize inspection or import of private files, credentials,
tokens, payloads, Profile identifiers, or health data. That cutover requires an
explicit owner gate.

### Milestone boundary

M1 owns the provider-neutral PostgreSQL foundation, generic ingestion contract,
existing-data importer, all consumer cutovers, and the one-shot cutover.

M2 owns calendar and Training Plan web UI built on M1 projections and commands.

M3 owns Garmin endpoint contracts, endpoint-specific mappings, retry and
rate-limit hardening, and ongoing rebuild/reconciliation tooling. M1 stores the
current parsed Garmin responses without expanding endpoint behavior; M3 makes
the adapter and mappings durable.

## Consequences

- M1 must use disposable real PostgreSQL for migration, constraint,
  transaction, and concurrency tests; SQLite or an in-memory fake cannot prove
  the required semantics.
- A clean deployment must start PostgreSQL and apply migrations before any
  producer or consumer runs.
- M1 cannot finish with only an ADR, schema, or importer: collector, MCP, and
  web must use `CoachApplication`, filesystem fallback must be unreachable, and
  PostgreSQL-only end-to-end plus backup/restore/rollback evidence must pass.
- Existing file-oriented docs remain accurate only as explicitly labelled
  pre-cutover operating instructions.
- The current raw files may contain only the latest response at a path. The
  importer reports that limitation rather than fabricating source history.
- PostgreSQL adds deployment and backup responsibilities, but removes the
  cross-process file races and split transaction boundaries from the target
  runtime.

## Non-goals

This decision does not:

- authorize the private-data cutover;
- allow writes to Garmin;
- define new Garmin endpoints or endpoint-specific mappings;
- introduce medical claims or inferred missing health values;
- add Profile sharing, multiple athletes per Profile, or browser-selected
  tenancy;
- require an ORM, event sourcing, universal EAV, or a TypeScript domain rewrite;
- make M2 or M3 deliverables prerequisites for the provider-neutral M1
  foundation beyond their documented dependency seams.
