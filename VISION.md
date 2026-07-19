# Vision

**garmin-coach is a personal running/fitness data platform: collect once, use
everywhere.**

The durable core is a clean, local record of one athlete's Garmin data. Every
other feature — dashboards, trend analysis, and eventually an AI coach — is just
a consumer of that record. Get the data layer right and honest, and the rest
composes on top of it.

This document explains the direction of the project and the guardrails an agent
(or a human) should work within. See [`AGENTS.md`](AGENTS.md) for how coding
agents operate in this repo.

## What it is

- A personal tool for **one athlete at a time** (today: me), built so a second
  person could take it and run it for themselves.
- A **two-layer data store**: immutable `data/raw/` (exactly what Garmin
  returned) plus coach-friendly `data/derived/` (normalized, stable shapes the
  app and coach read).
- A **collector** that pulls Garmin data idempotently, on a schedule and on
  demand, and a **buildless viz app** that renders it.
- The seam for a future **coach** that reasons over `data/derived/`.

## Ambition (staged)

The coach is built in deliberate stages. Each stage must stand on its own before
the next begins — no stage is a throwaway scaffold for the one after it.

1. **Describe** — collect reliably and show the data well (dashboards, trends,
   activities). *This is where we are.*
2. **Explain** — interpret the data: training load, recovery, VO2max and HRV
   trends, anomalies, "what changed and why."
3. **Coach** — turn insight into concrete guidance: workouts, weekly plans,
   adjustments that respond to recovery and goals.

## Priorities

Current focus:

- Reliable, idempotent data collection (no gaps, no duplicates).
- An honest dashboard: show real data, never invent or smooth over missing data.
- Keeping `data/derived/` shapes stable so downstream consumers don't churn.

Next:

- The **insight layer** — analysis over the derived data (stage 2).
- The **coach skill** that reads `data/derived/` and reasons about training.
- Deepening normalization (splits, HR streams) as features need it.

Later:

- **Multi-profile** support (e.g. a second athlete) — the data layer and app
  should stay profile-ready even while single-user.
- **Hand-off readiness** — setup, docs, and config clean enough that someone
  else can clone it and run it against their own Garmin account.

## What fits

Work that clearly advances the vision and can be verified end to end:

- Bug fixes in the collector, store, normalization, or viz.
- New derived metrics or small analysis primitives over existing data.
- Dashboard/UX improvements that make real data easier to read.
- Collection reliability: idempotency, retries, rate-limit handling, token
  caching, gap detection.
- Docs and setup improvements that make the project easier to run and hand off.
- Tests and manual verification for behavior that can realistically be exercised.

## Non-goals & guardrails

Hard boundaries — do not cross without an explicit decision:

- **Read-only to Garmin.** The project only ever *reads* from Garmin. It must
  never write back, modify, or otherwise mutate the Garmin account or its data.
- **No medical or clinical claims.** Output is training/fitness guidance, not
  diagnosis, treatment, or clinical advice. Never present it as medical fact.

Direction guardrails — prefer these, revisit only with a clear reason:

- **Data integrity over convenience.** Never fabricate, interpolate silently, or
  paper over missing data to make a chart look complete. Missing is missing.
- **Simple by default, modular for maintainability.** Reach for the smallest
  thing that works; add frameworks or infrastructure only for a concrete,
  demonstrated payoff.
- **Keep `raw/` immutable.** Reprocessing derives from raw; it never rewrites it.
- **Portable.** Avoid hard-wiring the project to one machine's paths or state in
  ways that block a clean hand-off.

## Autonomous vs. ask-first

This rule drives the `ready-for-agent` / `ready-for-human` triage split (see
[`docs/agents/triage-labels.md`](docs/agents/triage-labels.md)).

**An agent may work autonomously** when the change improves collection
reliability, correctness, derived metrics, analysis, dashboard UX, docs, or
tests within this vision, keeps `data/derived/` shapes stable (or migrates
consumers in the same change), and can be verified end to end against real data.

**Ask first** when the work:

- changes product direction or moves the project to a new stage,
- changes the `data/derived/` schema in a way consumers depend on,
- touches credentials, auth, or anything that could write to Garmin,
- adds a major dependency or long-running/background machinery,
- makes health/coaching claims that could read as medical advice, or
- cannot be verified with confidence against real data.

This list is a guardrail, not a law. A strong rationale can change it — raise it
explicitly rather than working around it.
