"""Stable machine-readable collector health state."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import store

SCHEMA_VERSION = 1
DOMAINS = ("daily", "activities", "plans", "profile")


def utc_timestamp() -> str:
    """Return an RFC 3339 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def aggregate_outcomes(outcomes: list[str]) -> str:
    """Classify attempted outcomes without treating skips as failures."""
    attempted = [value for value in outcomes if value != "not_attempted"]
    if not attempted:
        return "not_attempted"
    if all(value == "successful" for value in attempted):
        return "successful"
    if all(value == "failed" for value in attempted):
        return "failed"
    return "degraded"


class CollectionHealth:
    """Persist one run's observable health while carrying success freshness."""

    def __init__(self, path: Path) -> None:
        self.path = path
        previous = store.read_json(path, default={}) or {}
        previous_run = previous.get("run", {})
        previous_domains = previous.get("domains", {})

        domains: dict[str, dict[str, Any]] = {}
        for name in DOMAINS:
            prior = previous_domains.get(name, {})
            domains[name] = {
                "outcome": "not_attempted",
                "attempted_at": None,
                "finished_at": None,
                "last_success_at": prior.get("last_success_at"),
            }
            if name == "daily":
                domains[name]["dates"] = deepcopy(prior.get("dates", {}))
            else:
                domains[name]["date"] = None

        self.document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run": {
                "outcome": "attempted",
                "attempted_at": utc_timestamp(),
                "finished_at": None,
                "last_success_at": previous_run.get("last_success_at"),
            },
            "domains": domains,
        }
        self._active_domain: str | None = None
        self._active_date: str | None = None
        self._persist()

    def _persist(self) -> None:
        store.write_json(self.path, self.document)

    def day_outcome(self, iso_date: str) -> str | None:
        day = self.document["domains"]["daily"]["dates"].get(iso_date)
        return day.get("outcome") if day else None

    def begin_domain(self, name: str, collection_date: str | None = None) -> None:
        domain = self.document["domains"][name]
        domain.update(
            {
                "outcome": "attempted",
                "attempted_at": utc_timestamp(),
                "finished_at": None,
            }
        )
        if name != "daily":
            domain["date"] = collection_date
        self._active_domain = name
        self._active_date = None
        self._persist()

    def begin_day(self, iso_date: str) -> None:
        if self._active_domain != "daily":
            self.begin_domain("daily")
        prior = self.document["domains"]["daily"]["dates"].get(iso_date, {})
        self.document["domains"]["daily"]["dates"][iso_date] = {
            "outcome": "attempted",
            "attempted_at": utc_timestamp(),
            "finished_at": None,
            "last_success_at": prior.get("last_success_at"),
            "anchor": {"name": "stats", "outcome": "attempted"},
            "endpoints": {},
            "complete_marker_written": False,
        }
        self._active_date = iso_date
        self._persist()

    def record_day_results(self, iso_date: str, result: dict[str, Any]) -> None:
        day = self.document["domains"]["daily"]["dates"][iso_date]
        for key in ("anchor", "endpoints", "complete_marker_written"):
            day[key] = result[key]
        self._persist()

    def finish_day(self, iso_date: str, result: dict[str, Any]) -> None:
        finished_at = utc_timestamp()
        day = self.document["domains"]["daily"]["dates"][iso_date]
        day.update(result)
        day["finished_at"] = finished_at
        if result["outcome"] == "successful":
            day["last_success_at"] = finished_at
        self._active_date = None
        self._persist()

    def finish_domain(
        self, name: str, outcome: str, details: dict[str, Any] | None = None
    ) -> None:
        finished_at = utc_timestamp()
        domain = self.document["domains"][name]
        if details:
            domain.update(details)
        domain["outcome"] = outcome
        domain["finished_at"] = finished_at
        if outcome == "successful":
            domain["last_success_at"] = finished_at
        self._active_domain = None
        self._active_date = None
        self._persist()

    def finish_run(self) -> None:
        outcomes = [
            self.document["domains"][name]["outcome"] for name in DOMAINS
        ]
        outcome = aggregate_outcomes(outcomes)
        finished_at = utc_timestamp()
        run = self.document["run"]
        run["outcome"] = outcome
        run["finished_at"] = finished_at
        if outcome == "successful":
            run["last_success_at"] = finished_at
        self._persist()

    def fail_run(self) -> None:
        finished_at = utc_timestamp()
        if self._active_date is not None:
            day = self.document["domains"]["daily"]["dates"][self._active_date]
            day["outcome"] = "failed"
            day["finished_at"] = finished_at
        if self._active_domain is not None:
            domain = self.document["domains"][self._active_domain]
            domain["outcome"] = "failed"
            domain["finished_at"] = finished_at
        self.document["run"]["outcome"] = "failed"
        self.document["run"]["finished_at"] = finished_at
        self._persist()
