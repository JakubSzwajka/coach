"""Filesystem layout, atomic writes, and collection state.

Layout (all under ``data/``)::

    raw/
      daily/<YYYY>/<MM>/<YYYY-MM-DD>/<name>.json     # date-keyed wellness/training
      activities/<YYYY>/<MM>/<activity_id>/<name>.json
      body/<name>/<YYYY-MM>.json
      profile/<YYYY-MM-DD>/<name>.json               # weekly snapshot
      plans/<YYYY-MM-DD>/<name>.json
    derived/
      athlete.json                                   # current profile/zones/thresholds
      daily/<YYYY-MM-DD>.json                        # one merged record per day
      activities/<activity_id>.json
      timeline.jsonl                                 # append-only daily rollups
    index/state.json                                 # cursors + what we've pulled
    logs/collector-<YYYY-MM-DD>.log
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
DERIVED = DATA / "derived"
INDEX = DATA / "index"
LOGS = DATA / "logs"
STATE_FILE = INDEX / "state.json"


def _parts(d: date) -> tuple[str, str, str]:
    return f"{d.year:04d}", f"{d.month:02d}", d.isoformat()


def raw_daily(d: date, name: str) -> Path:
    y, m, iso = _parts(d)
    return RAW / "daily" / y / m / iso / f"{name}.json"


def raw_activity(activity_id: int | str, name: str, d: date | None = None) -> Path:
    if d is not None:
        y, m, _ = _parts(d)
        return RAW / "activities" / y / m / str(activity_id) / f"{name}.json"
    return RAW / "activities" / "_" / str(activity_id) / f"{name}.json"


def raw_snapshot(kind: str, d: date, name: str) -> Path:
    """kind is 'profile' or 'plans'."""
    return RAW / kind / d.isoformat() / f"{name}.json"


def derived_daily(d: date) -> Path:
    return DERIVED / "daily" / f"{d.isoformat()}.json"


def derived_activity(activity_id: int | str) -> Path:
    return DERIVED / "activities" / f"{activity_id}.json"


ATHLETE_FILE = DERIVED / "athlete.json"
TIMELINE_FILE = DERIVED / "timeline.jsonl"


def write_json(path: Path, data: Any) -> Path:
    """Atomic write: temp file in the same dir, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def append_timeline(record: dict) -> None:
    """Append a daily rollup, replacing any existing line for the same date."""
    TIMELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if TIMELINE_FILE.exists():
        with TIMELINE_FILE.open(encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    rows = [r for r in rows if r.get("date") != record.get("date")]
    rows.append(record)
    rows.sort(key=lambda r: r.get("date", ""))
    tmp = TIMELINE_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, TIMELINE_FILE)


def load_state() -> dict:
    return read_json(STATE_FILE, default={}) or {}


def save_state(state: dict) -> None:
    write_json(STATE_FILE, state)
