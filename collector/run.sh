#!/usr/bin/env bash
# Cron entrypoint for the Garmin collector.
# Resolves its own repo location so cron's minimal env still works.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# shellcheck disable=SC1091
source .venv/bin/activate

# Pass through any args (e.g. --days 30, --date 2026-07-10, --weekly)
exec python -m collector.collect "$@"
