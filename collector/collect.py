#!/usr/bin/env python3
"""Collect Garmin data into data/raw and build data/derived.

Examples::

    python -m collector.collect                 # yesterday + today, new activities
    python -m collector.collect --days 30       # backfill last 30 days
    python -m collector.collect --date 2026-07-10
    python -m collector.collect --weekly        # also refresh profile snapshot
    python -m collector.collect --activities-limit 50

Designed to be safe to run repeatedly (idempotent) and from cron.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from . import endpoints, normalize, store
from .client import connect

SNAPSHOT_EVERY_DAYS = 7
SEEN_ACTIVITIES_KEPT = 500


def log(msg: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    line = f"[{stamp}] {msg}"
    print(line)
    store.LOGS.mkdir(parents=True, exist_ok=True)
    with (store.LOGS / f"collector-{date.today().isoformat()}.log").open(
        "a", encoding="utf-8"
    ) as fh:
        fh.write(line + "\n")


def _pull(label, fn, *args) -> tuple[bool, object]:
    try:
        return True, fn(*args)
    except Exception as exc:  # noqa: BLE001 - report and continue
        log(f"  skip {label}: {exc}")
        return False, None


def collect_day(g, d: date) -> bool:
    """Pull one day. Returns True if the day was marked complete."""
    iso = d.isoformat()
    log(f"daily {iso}")
    ok = 0
    stats_ok = False
    for name, fn in endpoints.DAILY.items():
        got, data = _pull(name, fn, g, iso)
        if got and data is not None:
            store.write_json(store.raw_daily(d, name), data)
            ok += 1
            if name == "stats":
                stats_ok = True
    log(f"  wrote {ok}/{len(endpoints.DAILY)} daily metrics")
    normalize.normalize_daily(d)
    # Only mark complete when the anchor metric came through. During a 429
    # storm stats fails too, so we leave no marker and a re-run retries the
    # day instead of locking in gaps. Path-keyed writes mean retries never dup.
    if stats_ok:
        store.write_json(store.raw_daily(d, "_complete"), {"collected_at": iso})
        return True
    log(f"  {iso} left incomplete (no stats) — will retry on next run")
    return False


def collect_plans(g) -> None:
    log("plans")
    today = date.today()
    for name, fn in endpoints.PLANS.items():
        got, data = _pull(name, fn, g)
        if got and data is not None:
            store.write_json(store.raw_snapshot("plans", today, name), data)


def collect_snapshot(g, state: dict) -> None:
    log("profile snapshot")
    today = date.today()
    for name, fn in endpoints.SNAPSHOT.items():
        got, data = _pull(name, fn, g)
        if got and data is not None:
            store.write_json(store.raw_snapshot("profile", today, name), data)
    normalize.rebuild_athlete()
    state["last_snapshot"] = today.isoformat()


def _activity_date(summary: dict) -> date | None:
    ts = summary.get("startTimeLocal")
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def collect_activities(g, state: dict, limit: int) -> None:
    log(f"activities (checking latest {limit})")
    got, activities = _pull("activities", g.get_activities, 0, limit)
    if not got or not activities:
        return
    seen = set(state.get("seen_activities", []))
    new = [a for a in activities if a.get("activityId") not in seen]
    log(f"  {len(new)} new of {len(activities)}")
    for a in new:
        aid = a.get("activityId")
        d = _activity_date(a)
        store.write_json(store.raw_activity(aid, "list_summary", d), a)
        for name, fn in endpoints.ACTIVITY_DETAIL.items():
            ok, data = _pull(f"activity {aid} {name}", fn, g, aid)
            if ok and data is not None:
                store.write_json(store.raw_activity(aid, name, d), data)
        normalize.normalize_activity(aid, d)
        seen.add(aid)
    # keep the most recent ids so the set does not grow unbounded
    ordered = [a.get("activityId") for a in activities] + list(seen)
    dedup: list = []
    for x in ordered:
        if x not in dedup:
            dedup.append(x)
    state["seen_activities"] = dedup[:SEEN_ACTIVITIES_KEPT]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Collect Garmin data")
    p.add_argument("--date", help="collect a single day (YYYY-MM-DD)")
    p.add_argument("--days", type=int, help="backfill this many days including today")
    p.add_argument("--activities-limit", type=int, default=15)
    p.add_argument("--weekly", action="store_true", help="force profile snapshot")
    p.add_argument("--no-activities", action="store_true")
    p.add_argument(
        "--force",
        action="store_true",
        help="re-pull days already marked complete (default: skip them)",
    )
    args = p.parse_args(argv)

    state = store.load_state()
    g = connect()
    log("authenticated")

    today = date.today()
    recent = {today, today - timedelta(days=1)}  # always refresh; day not final

    if args.date:
        collect_day(g, date.fromisoformat(args.date))
    elif args.days:
        skipped = 0
        for i in range(args.days):
            d = today - timedelta(days=i)
            done = store.raw_daily(d, "_complete").exists()
            if done and not args.force and d not in recent:
                skipped += 1
                continue
            collect_day(g, d)
        if skipped:
            log(f"skipped {skipped} already-complete day(s)")
    else:
        # default cron run: finalize yesterday, refresh today
        collect_day(g, today - timedelta(days=1))
        collect_day(g, today)

    collect_plans(g)

    if not args.no_activities:
        collect_activities(g, state, args.activities_limit)

    last_snap = state.get("last_snapshot")
    due = (
        args.weekly
        or last_snap is None
        or (date.today() - date.fromisoformat(last_snap)).days >= SNAPSHOT_EVERY_DAYS
    )
    if due:
        collect_snapshot(g, state)

    normalize.rebuild_activities_index()
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    store.save_state(state)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
