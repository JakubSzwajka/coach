# Garmin collector

Pulls Garmin Connect data into a two-layer store on a daily/on-demand basis.

## Layers

- **`data/raw/`** — exactly what Garmin returned. Immutable, reprocessable, the source of truth.
- **`data/derived/`** — regenerable, compact, coach-friendly training records.
- **`data/index/state.json`** — cursors: seen activity ids, last snapshot, last run.
- **`data/index/collector-health.json`** — machine-readable outcomes and freshness.
- **`data/logs/`** — per-day run logs.

```
data/
  raw/
    daily/2026/07/2026-07-18/{stats,sleep,hrv,rhr,stress,body_battery,
                              steps,floors,intensity_minutes,respiration,
                              spo2,training_readiness,training_status,
                              max_metrics,hydration,user_summary}.json
    activities/2026/07/<activity_id>/{list_summary,summary,details,splits,
                                      hr_zones,weather,exercise_sets}.json
    plans/2026-07-18/{scheduled_workouts,workouts,training_plans}.json
    profile/2026-07-18/{user_profile,settings,unit_system,full_name,devices,
                        device_last_used,personal_records,goals_active,
                        race_predictions}.json
  derived/
    athlete.json                 # name, gender, weight, VO2max, thresholds,
                                 # available/preferred training days
    daily/2026-07-18.json        # one compact record per day
    activities/<activity_id>.json
    timeline.jsonl               # append-only daily rollups, one line per day
  index/state.json
  index/collector-health.json    # schema-versioned run/domain/date health
  logs/collector-2026-07-18.log
```

## Run

```bash
python -m collector.collect                 # yesterday + today + new activities
python -m collector.collect --days 30       # backfill last 30 days
python -m collector.collect --date 2026-07-10
python -m collector.collect --weekly        # also refresh profile snapshot
python -m collector.collect --no-activities
```

## Machine-readable collection health

`data/index/collector-health.json` is the stable downstream health seam
(`schema_version: 1`). It is atomically replaced at run start, domain/date
attempts, domain completion, and run completion, so an interrupted process
remains visibly `attempted`.

- Outcomes are `not_attempted`, `attempted`, `successful`, `degraded`, or
  `failed`.
- Run and domain records expose UTC RFC 3339 `attempted_at`, `finished_at`, and
  carried `last_success_at` timestamps. Freshness advances only after a fully
  successful run/domain/date, not after a degraded attempt.
- Daily records are keyed by ISO date and expose the `stats` anchor outcome,
  every endpoint outcome, and whether this run wrote the `_complete` marker.
- Activities expose the list outcome, latest-page-independent pending-retry
  count, aggregate detail-call outcomes, preserved detail-file count,
  summary-anchor completion counts, and cursor advances without activity
  identifiers. Profile snapshots expose per-endpoint and
  required-anchor outcomes, preserved-file counts, and whether the cursor
  advanced. Skipped activities or a profile that is not due remain
  `not_attempted`.
- All attempted domains successful means the run is `successful`; all failed
  means `failed`; mixed or partial outcomes mean `degraded`. A fatal collector
  exception records `failed` and is still re-raised.

Endpoint failures may still let the collector return zero after preserving
successful files. Existing raw/derived files remain readable and are never
deleted by health reporting. `state.last_run`, a log `done` line, `_complete`
file presence, and raw file presence are not success signals; consume the
health contract instead. Exception messages and Collected Record identifiers
are intentionally excluded.

## Cadence

| What | When |
|---|---|
| Daily wellness + training state (16 metrics) | every run, for yesterday and today |
| Plans / scheduled workouts | every run |
| New activities + details | every run; `seen_activities` advances only after the `summary` anchor exists |
| Profile snapshot + `athlete.json` | every 7 days, or `--weekly`; `last_snapshot` advances only after `user_profile`, `full_name`, and `personal_records` exist |

## Cron

`collector/run.sh` resolves the repo path and activates the venv, so cron's
minimal environment works. Run twice a day to catch morning readiness and
evening activities:

```cron
# finalize yesterday + morning readiness
30 6 * * *  /path/to/garmin-coach/collector/run.sh >> /tmp/garmin-coach.log 2>&1
# pick up the day's activities
0 21 * * *  /path/to/garmin-coach/collector/run.sh >> /tmp/garmin-coach.log 2>&1
```

## Design notes

- Credential login is rare and rate-limited (Garmin 429s the mobile strategy).
  Tokens are cached in `~/.garminconnect`; runs resume from cache in ~1–2 s and
  avoid that path. The client backs off on 429.
- Every endpoint call is wrapped: an unsupported metric records a skip and the
  run continues. Add a metric by adding one line in `endpoints.py`.
- Activity details and same-date profile endpoints already written successfully
  are reused on retry. A failed activity summary remains discoverable from its
  preserved list summary even after leaving the bounded latest-activities page;
  it and any missing required profile anchor leave their cursor unchanged so
  the next run retries only missing files.
- Derived files are disposable — delete `data/derived/` and re-run to rebuild
  from `data/raw/` (add a `--rebuild` reprocessor later if wanted).
- Uses only `garminconnect` + `curl_cffi`; `.env` loading and JSON store are
  dependency-free.

## Coach skill (next)

Point the coach at `data/derived/`:
- `athlete.json` — who they are, thresholds, when they can train.
- `timeline.jsonl` — scan trends (readiness, HRV, sleep, load) fast.
- `daily/<date>.json` — a specific day in detail.
- `activities/<id>.json` — a specific session.
