"""Declarative map of what to pull.

Each spec is (name, fn). Daily specs take (client, iso_date); snapshot specs
take (client). Every call is wrapped in try/except by the runner, so an
unsupported metric for your device/account just records a skip and the run
continues. Add a line here to collect more.
"""

from __future__ import annotations

from typing import Callable

# name -> fn(client, iso_date) -> data  (date-keyed, pulled daily)
DAILY: dict[str, Callable] = {
    "stats": lambda g, d: g.get_stats(d),
    "user_summary": lambda g, d: g.get_user_summary(d),
    "sleep": lambda g, d: g.get_sleep_data(d),
    "hrv": lambda g, d: g.get_hrv_data(d),
    "rhr": lambda g, d: g.get_rhr_day(d),
    "stress": lambda g, d: g.get_stress_data(d),
    "body_battery": lambda g, d: g.get_body_battery(d, d),
    "steps": lambda g, d: g.get_steps_data(d),
    "floors": lambda g, d: g.get_floors(d),
    "intensity_minutes": lambda g, d: g.get_intensity_minutes_data(d),
    "respiration": lambda g, d: g.get_respiration_data(d),
    "spo2": lambda g, d: g.get_spo2_data(d),
    "training_readiness": lambda g, d: g.get_training_readiness(d),
    "training_status": lambda g, d: g.get_training_status(d),
    "max_metrics": lambda g, d: g.get_max_metrics(d),
    "hydration": lambda g, d: g.get_hydration_data(d),
}

# name -> fn(client) -> data  (slow-changing; pulled on the weekly snapshot)
SNAPSHOT: dict[str, Callable] = {
    "user_profile": lambda g: g.get_user_profile(),
    "settings": lambda g: g.get_userprofile_settings(),
    "unit_system": lambda g: g.get_unit_system(),
    "full_name": lambda g: {"fullName": g.get_full_name()},
    "devices": lambda g: g.get_devices(),
    "device_last_used": lambda g: g.get_device_last_used(),
    "personal_records": lambda g: g.get_personal_record(),
    "goals_active": lambda g: g.get_goals("active"),
    "race_predictions": lambda g: g.get_race_predictions(),
}

# name -> fn(client, collection_date) -> data  (training calendar; pulled daily)
PLANS: dict[str, Callable] = {
    "scheduled_workouts": lambda g, d: g.get_scheduled_workouts(d.year, d.month),
    "workouts": lambda g, d: g.get_workouts(),
    "training_plans": lambda g, d: g.get_training_plans(),
}

# Per-activity detail endpoints, keyed by name -> fn(client, activity_id)
ACTIVITY_DETAIL: dict[str, Callable] = {
    "summary": lambda g, a: g.get_activity(a),
    "splits": lambda g, a: g.get_activity_splits(a),
    "hr_zones": lambda g, a: g.get_activity_hr_in_timezones(a),
    "weather": lambda g, a: g.get_activity_weather(a),
    "exercise_sets": lambda g, a: g.get_activity_exercise_sets(a),
}
