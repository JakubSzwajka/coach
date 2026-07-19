# coach

A personal running/fitness **data platform** over your own Garmin data:
collect once, use everywhere. A local collector pulls your Garmin Connect data
into a clean two-layer store; a buildless web app visualises it; and (in stages)
an AI coach reasons over it.

See [`VISION.md`](VISION.md) for the north star, staged ambition, and guardrails.

> **Guardrails:** the project is **read-only** to Garmin (it never writes back
> to your account), and any coaching output is training guidance, **not medical
> advice**.

## How it fits together

```
Garmin Connect ──▶ collector ──▶ data/raw/      (immutable, exactly as returned)
                                 data/derived/  (compact, coach-friendly)
                                       │
                                       ├──▶ viz/   (dashboard, trends, activities)
                                       └──▶ coach  (reads data/derived/ — staged)
```

## Requirements

- Python 3.10+
- A Garmin Connect account (the collector logs in as you)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then fill in your Garmin credentials
set -a; source .env; set +a   # load them into the shell
```

Credentials are only used on the rare re-login — auth tokens are cached in
`~/.garminconnect` afterwards. Your `.env` and all collected `data/` are
gitignored and never committed.

## Collect data

```bash
python -m collector.collect                 # yesterday + today + new activities
python -m collector.collect --days 30       # backfill last 30 days
python -m collector.collect --date 2026-07-10
python -m collector.collect --weekly        # also refresh the profile snapshot
```

Collection is idempotent (no gaps, no duplicates) and safe to run on a cron.
See [`collector/README.md`](collector/README.md) for the data layout and cron
setup.

## View the dashboard

```bash
python -m http.server 8000    # from the repo root
# open http://localhost:8000/viz/
```

See [`viz/README.md`](viz/README.md) for the views and the `data.js` seam.

## Layout

```
collector/   Python package: idempotent Garmin pull → raw + derived store
viz/         buildless vanilla-JS app over data/derived/
data/        collected data (gitignored: raw/, derived/, index/, logs/)
docs/        knowledge/, agents/ (agent config), adr/ (as decisions land)
spikes/      throwaway experiments (auth smoke test)
VISION.md    project direction and guardrails
AGENTS.md    how coding agents work in this repo
```

## Working with agents

This repo is set up for AI coding agents — issue tracking, triage labels, and
domain docs are described in [`AGENTS.md`](AGENTS.md) and `docs/agents/`.

## License

[MIT](LICENSE) © 2026 Jakub Szwajka
