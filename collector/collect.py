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

from . import endpoints, health, normalize, store
from .client import connect

SNAPSHOT_EVERY_DAYS = 7
SEEN_ACTIVITIES_KEPT = 500
ACTIVITY_ANCHOR = "summary"
PROFILE_ANCHORS = ("user_profile", "full_name", "personal_records")


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


def collect_day(g, d: date) -> dict:
    """Pull one day and return its endpoint-level health outcome."""
    iso = d.isoformat()
    log(f"daily {iso}")
    endpoint_outcomes: dict[str, str] = {}
    stats_ok = False
    for name, fn in endpoints.DAILY.items():
        got, data = _pull(name, fn, g, iso)
        successful = got and data is not None
        endpoint_outcomes[name] = "successful" if successful else "failed"
        if successful:
            store.write_json(store.raw_daily(d, name), data)
            if name == "stats":
                stats_ok = True
    successful_count = sum(
        outcome == "successful" for outcome in endpoint_outcomes.values()
    )
    log(f"  wrote {successful_count}/{len(endpoints.DAILY)} daily metrics")
    # Only write a marker when the anchor metric came through. Health remains
    # authoritative for whether an existing marker represents a successful
    # latest attempt; path-keyed retries never duplicate data.
    if stats_ok:
        store.write_json(store.raw_daily(d, "_complete"), {"collected_at": iso})
    else:
        log(f"  {iso} left incomplete (no stats) — will retry on next run")
    return {
        "outcome": health.aggregate_outcomes(list(endpoint_outcomes.values())),
        "anchor": {
            "name": "stats",
            "outcome": endpoint_outcomes.get("stats", "failed"),
        },
        "endpoints": endpoint_outcomes,
        "complete_marker_written": stats_ok,
    }


def collect_plans(g, collection_date: date) -> dict:
    log("plans")
    endpoint_outcomes: dict[str, str] = {}
    for name, fn in endpoints.PLANS.items():
        got, data = _pull(name, fn, g, collection_date)
        successful = got and data is not None
        endpoint_outcomes[name] = "successful" if successful else "failed"
        if successful:
            store.write_json(
                store.raw_snapshot("plans", collection_date, name), data
            )
    return {
        "outcome": health.aggregate_outcomes(list(endpoint_outcomes.values())),
        "endpoints": endpoint_outcomes,
    }


def collect_snapshot(g, state: dict, collection_date: date) -> dict:
    log("profile snapshot")
    endpoint_outcomes: dict[str, str] = {}
    files_reused = 0
    for name, fn in endpoints.SNAPSHOT.items():
        path = store.raw_snapshot("profile", collection_date, name)
        if path.exists():
            endpoint_outcomes[name] = "successful"
            files_reused += 1
            continue
        got, data = _pull(name, fn, g)
        successful = got and data is not None
        endpoint_outcomes[name] = "successful" if successful else "failed"
        if successful:
            store.write_json(path, data)

    anchors = {
        name: endpoint_outcomes.get(name, "failed") for name in PROFILE_ANCHORS
    }
    complete = all(outcome == "successful" for outcome in anchors.values())
    if complete:
        normalize.rebuild_athlete()
        state["last_snapshot"] = collection_date.isoformat()
    else:
        log("  profile snapshot left incomplete — will retry on next run")

    return {
        "outcome": health.aggregate_outcomes(list(endpoint_outcomes.values())),
        "endpoints": endpoint_outcomes,
        "anchors": anchors,
        "files_reused": files_reused,
        "cursor_advanced": complete,
    }


def _activity_date(summary: dict) -> date | None:
    ts = summary.get("startTimeLocal")
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def collect_activities(g, state: dict, limit: int) -> dict:
    log(f"activities (checking latest {limit})")
    got, activities = _pull("activities", g.get_activities, 0, limit)
    list_successful = got and activities is not None
    latest = activities if list_successful else []

    pending_by_id = {
        summary["activityId"]: summary
        for summary in store.iter_raw_activity_summaries_missing(ACTIVITY_ANCHOR)
        if summary.get("activityId") is not None
    }
    seen_order = [
        activity_id
        for activity_id in state.get("seen_activities", [])
        if activity_id not in pending_by_id
    ]
    seen = set(seen_order)
    latest_ids = {a.get("activityId") for a in latest}
    pending_outside_latest = [
        summary
        for activity_id, summary in pending_by_id.items()
        if activity_id not in latest_ids
    ]
    new = [a for a in latest if a.get("activityId") not in seen]
    new.extend(pending_outside_latest)
    log(
        f"  {len(new)} new/retry of {len(latest)} latest"
        f" ({len(pending_by_id)} pending retry)"
    )

    detail_counts = {"attempted": 0, "successful": 0, "failed": 0}
    detail_files_reused = 0
    anchor_counts = {"successful": 0, "failed": 0}
    completed_this_run: list = []
    for a in new:
        aid = a.get("activityId")
        d = _activity_date(a)
        list_summary_path = store.raw_activity(aid, "list_summary", d)
        if not list_summary_path.exists():
            store.write_json(list_summary_path, a)

        anchor_successful = False
        for name, fn in endpoints.ACTIVITY_DETAIL.items():
            path = store.raw_activity(aid, name, d)
            if path.exists():
                detail_files_reused += 1
                if name == ACTIVITY_ANCHOR:
                    anchor_successful = True
                continue

            detail_counts["attempted"] += 1
            ok, data = _pull(f"activity {aid} {name}", fn, g, aid)
            if ok and data is not None:
                store.write_json(path, data)
                detail_counts["successful"] += 1
                if name == ACTIVITY_ANCHOR:
                    anchor_successful = True
            else:
                detail_counts["failed"] += 1

        if anchor_successful:
            normalize.normalize_activity(aid, d)
            seen.add(aid)
            completed_this_run.append(aid)
            anchor_counts["successful"] += 1
        else:
            anchor_counts["failed"] += 1
            log(f"  activity {aid} left incomplete (no {ACTIVITY_ANCHOR})")

    # Keep the most recent completed ids so the set does not grow unbounded.
    ordered = [
        a.get("activityId")
        for a in latest
        if a.get("activityId") in seen
    ] + completed_this_run + seen_order
    dedup: list = []
    for x in ordered:
        if x not in dedup:
            dedup.append(x)
    state["seen_activities"] = dedup[:SEEN_ACTIVITIES_KEPT]

    incomplete = detail_counts["failed"] or anchor_counts["failed"]
    if list_successful:
        outcome = "degraded" if incomplete else "successful"
    else:
        any_detail_available = detail_counts["successful"] or detail_files_reused
        outcome = "degraded" if any_detail_available else "failed"
    return {
        "outcome": outcome,
        "list_endpoint": "successful" if list_successful else "failed",
        "activities_found": len(latest),
        "activities_new": len(new),
        "activities_pending_retry": len(pending_by_id),
        "detail_endpoints": detail_counts,
        "detail_files_reused": detail_files_reused,
        "anchors": anchor_counts,
        "cursor_advanced": anchor_counts["successful"],
    }


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

    today = date.today()
    report = health.CollectionHealth(store.HEALTH_FILE)
    try:
        state = store.load_state()
        g = connect()
        log("authenticated")

        recent = {today, today - timedelta(days=1)}  # always refresh; day not final
        daily_outcomes: list[str] = []

        def pull_day(d: date) -> None:
            report.begin_day(d.isoformat())
            result = collect_day(g, d)
            report.record_day_results(d.isoformat(), result)
            normalize.normalize_daily(d)
            report.finish_day(d.isoformat(), result)
            daily_outcomes.append(result["outcome"])

        if args.date:
            pull_day(date.fromisoformat(args.date))
        elif args.days:
            skipped = 0
            for i in range(args.days):
                d = today - timedelta(days=i)
                done = (
                    store.raw_daily(d, "_complete").exists()
                    and report.day_outcome(d.isoformat()) == "successful"
                )
                if done and not args.force and d not in recent:
                    skipped += 1
                    continue
                pull_day(d)
            if skipped:
                log(f"skipped {skipped} already-complete day(s)")
        else:
            # default cron run: finalize yesterday, refresh today
            pull_day(today - timedelta(days=1))
            pull_day(today)

        if daily_outcomes:
            report.finish_domain(
                "daily", health.aggregate_outcomes(daily_outcomes)
            )

        report.begin_domain("plans", today.isoformat())
        plans = collect_plans(g, today)
        report.finish_domain("plans", plans.pop("outcome"), plans)

        if not args.no_activities:
            report.begin_domain("activities", today.isoformat())
            activities = collect_activities(g, state, args.activities_limit)
            report.finish_domain(
                "activities", activities.pop("outcome"), activities
            )

        last_snap = state.get("last_snapshot")
        due = (
            args.weekly
            or last_snap is None
            or (today - date.fromisoformat(last_snap)).days >= SNAPSHOT_EVERY_DAYS
        )
        if due:
            report.begin_domain("profile", today.isoformat())
            profile = collect_snapshot(g, state, today)
            report.finish_domain("profile", profile.pop("outcome"), profile)

        normalize.rebuild_activities_index()
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        store.save_state(state)
        log("done")
        report.finish_run()
        return 0
    except Exception:
        report.fail_run()
        raise


if __name__ == "__main__":
    sys.exit(main())
