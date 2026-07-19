"""Build the coach-friendly ``derived/`` layer from ``raw/``.

Derived files are regenerable: delete them and re-run to rebuild from raw.
The coach skill should read only this layer.
"""

from __future__ import annotations

from datetime import date

from . import store


def _g(obj, *keys, default=None):
    """Safe nested get across dicts."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def normalize_daily(d: date) -> dict:
    """Merge the day's raw files into one compact, typed record."""
    def raw(name):
        return store.read_json(store.raw_daily(d, name))

    stats = raw("stats") or {}
    sleep = raw("sleep") or {}
    hrv = raw("hrv") or {}
    readiness = raw("training_readiness")
    status = raw("training_status") or {}
    body_battery = raw("body_battery")

    sleep_dto = _g(sleep, "dailySleepDTO", default={}) or {}
    if isinstance(readiness, list):
        readiness = readiness[0] if readiness else {}
    readiness = readiness or {}

    record = {
        "date": d.isoformat(),
        "steps": stats.get("totalSteps"),
        "distance_m": stats.get("totalDistanceMeters"),
        "active_kcal": stats.get("activeKilocalories"),
        "resting_hr": stats.get("restingHeartRate"),
        "min_hr": stats.get("minHeartRate"),
        "max_hr": stats.get("maxHeartRate"),
        "avg_stress": stats.get("averageStressLevel"),
        "intensity_min_moderate": stats.get("moderateIntensityMinutes"),
        "intensity_min_vigorous": stats.get("vigorousIntensityMinutes"),
        "floors_ascended": stats.get("floorsAscended"),
        "sleep_seconds": sleep_dto.get("sleepTimeSeconds"),
        "sleep_score": _g(sleep_dto, "sleepScores", "overall", "value"),
        "deep_sleep_s": sleep_dto.get("deepSleepSeconds"),
        "light_sleep_s": sleep_dto.get("lightSleepSeconds"),
        "rem_sleep_s": sleep_dto.get("remSleepSeconds"),
        "awake_s": sleep_dto.get("awakeSleepSeconds"),
        "hrv_avg": _g(hrv, "hrvSummary", "lastNightAvg"),
        "hrv_status": _g(hrv, "hrvSummary", "status"),
        "training_readiness": readiness.get("score"),
        "training_readiness_level": readiness.get("level"),
        "training_status": _g(
            status, "latestTrainingStatusData", default=None
        ) is not None,
        "vo2max_running": _g(
            status,
            "mostRecentVO2Max",
            "generic",
            "vo2MaxPreciseValue",
        ),
    }
    if isinstance(body_battery, list) and body_battery:
        bb = body_battery[0]
        record["body_battery_charged"] = bb.get("charged")
        record["body_battery_drained"] = bb.get("drained")

    store.write_json(store.derived_daily(d), record)
    store.append_timeline(record)
    return record


def normalize_activity(activity_id: int | str, d: date | None = None) -> dict:
    """Compact one activity's raw summary into a coach-friendly record."""
    summary = store.read_json(store.raw_activity(activity_id, "summary", d)) or {}
    record = {
        "activity_id": activity_id,
        "name": summary.get("activityName"),
        "type": _g(summary, "activityTypeDTO", "typeKey")
        or _g(summary, "activityType", "typeKey"),
        "start_local": _g(summary, "summaryDTO", "startTimeLocal")
        or summary.get("startTimeLocal"),
        "distance_m": _g(summary, "summaryDTO", "distance")
        or summary.get("distance"),
        "duration_s": _g(summary, "summaryDTO", "duration")
        or summary.get("duration"),
        "avg_hr": _g(summary, "summaryDTO", "averageHR")
        or summary.get("averageHR"),
        "max_hr": _g(summary, "summaryDTO", "maxHR") or summary.get("maxHR"),
        "elevation_gain_m": _g(summary, "summaryDTO", "elevationGain")
        or summary.get("elevationGain"),
        "avg_speed_mps": _g(summary, "summaryDTO", "averageSpeed")
        or summary.get("averageSpeed"),
        "calories": _g(summary, "summaryDTO", "calories"),
        "training_effect_aerobic": _g(summary, "summaryDTO", "trainingEffect"),
        "training_effect_anaerobic": _g(
            summary, "summaryDTO", "anaerobicTrainingEffect"
        ),
    }
    store.write_json(store.derived_activity(activity_id), record)
    return record


def rebuild_athlete() -> dict:
    """Snapshot current profile/thresholds into derived/athlete.json.

    Reads the most recent profile snapshot folder under raw/profile/.
    """
    prof_root = store.RAW / "profile"
    snapshots = sorted(p for p in prof_root.glob("*") if p.is_dir()) if prof_root.exists() else []
    if not snapshots:
        return {}
    latest = snapshots[-1]
    profile = store.read_json(latest / "user_profile.json") or {}
    ud = profile.get("userData", {}) or {}
    name = store.read_json(latest / "full_name.json") or {}
    prs = store.read_json(latest / "personal_records.json")
    athlete = {
        "snapshot_date": latest.name,
        "full_name": name.get("fullName"),
        "user_profile_id": profile.get("id"),
        "gender": ud.get("gender"),
        "birth_date": ud.get("birthDate"),
        "weight_g": ud.get("weight"),
        "height_cm": ud.get("height"),
        "vo2max_running": ud.get("vo2MaxRunning"),
        "vo2max_cycling": ud.get("vo2MaxCycling"),
        "lactate_threshold_speed_mps": ud.get("lactateThresholdSpeed"),
        "lactate_threshold_hr": ud.get("lactateThresholdHeartRate"),
        "available_training_days": ud.get("availableTrainingDays"),
        "preferred_long_training_days": ud.get("preferredLongTrainingDays"),
        "personal_records_count": len(prs) if isinstance(prs, list) else None,
    }
    store.write_json(store.ATHLETE_FILE, athlete)
    return athlete


def rebuild_activities_index() -> list[dict]:
    """Write derived/activities.json: all activity summaries, newest first.

    This is the manifest the visualiser reads for its list view (browsers
    cannot enumerate a directory).
    """
    act_dir = store.DERIVED / "activities"
    rows: list[dict] = []
    if act_dir.exists():
        for f in act_dir.glob("*.json"):
            rec = store.read_json(f)
            if isinstance(rec, dict):
                rows.append(rec)
    rows.sort(key=lambda r: r.get("start_local") or "", reverse=True)
    store.write_json(store.DERIVED / "activities.json", rows)
    return rows
