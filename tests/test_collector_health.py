from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from collector import collect, store


class FixedDate(date):
    @classmethod
    def today(cls) -> "FixedDate":
        return cls(2026, 1, 10)


class DriftingDate(date):
    calls = 0

    @classmethod
    def today(cls) -> "DriftingDate":
        cls.calls += 1
        day = 10 if cls.calls == 1 else 11
        return cls(2026, 1, day)


class SyntheticClient:
    def get_activities(self, start: int, limit: int) -> list[dict]:
        return [
            {
                "activityId": "synthetic-activity",
                "startTimeLocal": "2026-01-09T08:00:00",
            }
        ]


class CollectorHealthContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        data = self.root / "data"
        self.stack.enter_context(
            patch.multiple(
                store,
                DATA=data,
                RAW=data / "raw",
                DERIVED=data / "derived",
                INDEX=data / "index",
                LOGS=data / "logs",
                STATE_FILE=data / "index" / "state.json",
                HEALTH_FILE=data / "index" / "collector-health.json",
                ATHLETE_FILE=data / "derived" / "athlete.json",
                TIMELINE_FILE=data / "derived" / "timeline.jsonl",
            )
        )
        self.stack.enter_context(patch.object(collect, "date", FixedDate))
        self.stack.enter_context(patch.object(collect, "connect", return_value=SyntheticClient()))
        self.stack.enter_context(
            patch.object(
                collect.endpoints,
                "DAILY",
                {
                    "stats": lambda client, iso: {"date": iso},
                    "sleep": lambda client, iso: {"date": iso},
                },
            )
        )
        self.stack.enter_context(
            patch.object(
                collect.endpoints,
                "PLANS",
                {"workouts": lambda client, collection_date: {"items": []}},
            )
        )
        self.stack.enter_context(
            patch.object(
                collect.endpoints,
                "SNAPSHOT",
                {
                    "user_profile": lambda client: {"fixture": "profile"},
                    "full_name": lambda client: {"fullName": "Synthetic Athlete"},
                    "personal_records": lambda client: [],
                },
            )
        )
        self.stack.enter_context(
            patch.object(
                collect.endpoints,
                "ACTIVITY_DETAIL",
                {"summary": lambda client, activity_id: {"fixture": True}},
            )
        )
        for name in (
            "normalize_daily",
            "normalize_activity",
            "rebuild_athlete",
            "rebuild_activities_index",
        ):
            self.stack.enter_context(patch.object(getattr(collect, "normalize"), name))

    def read_health(self) -> dict:
        with (self.root / "data" / "index" / "collector-health.json").open(
            encoding="utf-8"
        ) as handle:
            return json.load(handle)

    def assert_utc_timestamp(self, value: str) -> None:
        self.assertTrue(value.endswith("Z"))
        self.assertIsNotNone(datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo)

    def test_successful_run_records_explicit_domain_and_date_outcomes(self) -> None:
        exit_code = collect.main(["--date", "2026-01-09", "--weekly"])

        self.assertEqual(exit_code, 0)
        health = self.read_health()
        self.assertEqual(health["schema_version"], 1)
        self.assertEqual(health["run"]["outcome"], "successful")
        self.assert_utc_timestamp(health["run"]["attempted_at"])
        self.assert_utc_timestamp(health["run"]["finished_at"])
        self.assertEqual(
            health["run"]["last_success_at"], health["run"]["finished_at"]
        )

        self.assertEqual(set(health["domains"]), {"daily", "activities", "plans", "profile"})
        for domain in health["domains"].values():
            self.assertEqual(domain["outcome"], "successful")
            self.assert_utc_timestamp(domain["attempted_at"])
            self.assert_utc_timestamp(domain["finished_at"])
            self.assertEqual(domain["last_success_at"], domain["finished_at"])

        daily = health["domains"]["daily"]["dates"]["2026-01-09"]
        self.assertEqual(daily["outcome"], "successful")
        self.assertEqual(
            daily["anchor"], {"name": "stats", "outcome": "successful"}
        )
        self.assertEqual(
            daily["endpoints"], {"stats": "successful", "sleep": "successful"}
        )
        self.assertTrue(daily["complete_marker_written"])
        self.assertEqual(daily["last_success_at"], daily["finished_at"])

        activities = health["domains"]["activities"]
        self.assertEqual(activities["date"], "2026-01-10")
        self.assertEqual(health["domains"]["plans"]["date"], "2026-01-10")
        self.assertEqual(health["domains"]["profile"]["date"], "2026-01-10")
        self.assertEqual(activities["list_endpoint"], "successful")
        self.assertEqual(activities["activities_found"], 1)
        self.assertEqual(activities["activities_new"], 1)
        self.assertEqual(
            activities["detail_endpoints"],
            {"attempted": 1, "successful": 1, "failed": 0},
        )
        self.assertEqual(
            health["domains"]["plans"]["endpoints"], {"workouts": "successful"}
        )
        self.assertEqual(
            health["domains"]["profile"]["endpoints"],
            {
                "full_name": "successful",
                "personal_records": "successful",
                "user_profile": "successful",
            },
        )
        self.assertNotIn("synthetic-activity", json.dumps(health))

    def test_skipped_domains_are_explicit_and_keep_prior_freshness(self) -> None:
        collect.main(["--date", "2026-01-09", "--weekly"])
        previous = self.read_health()
        prior_activities_success = previous["domains"]["activities"][
            "last_success_at"
        ]
        prior_profile_success = previous["domains"]["profile"]["last_success_at"]

        exit_code = collect.main(
            ["--date", "2026-01-09", "--no-activities"]
        )

        self.assertEqual(exit_code, 0)
        health = self.read_health()
        self.assertEqual(health["run"]["outcome"], "successful")
        for name, last_success_at in (
            ("activities", prior_activities_success),
            ("profile", prior_profile_success),
        ):
            domain = health["domains"][name]
            self.assertEqual(domain["outcome"], "not_attempted")
            self.assertIsNone(domain["attempted_at"])
            self.assertIsNone(domain["finished_at"])
            self.assertIsNone(domain["date"])
            self.assertEqual(domain["last_success_at"], last_success_at)

    def test_degraded_run_preserves_partial_data_and_success_freshness(self) -> None:
        collect.main(["--date", "2026-01-09", "--weekly"])
        previous_health = self.read_health()
        previous_success_at = "2025-12-31T23:59:59Z"
        previous_health["run"]["last_success_at"] = previous_success_at
        previous_health["domains"]["daily"]["last_success_at"] = previous_success_at
        previous_health["domains"]["daily"]["dates"]["2026-01-09"][
            "last_success_at"
        ] = previous_success_at
        store.write_json(store.HEALTH_FILE, previous_health)
        sleep_path = store.raw_daily(FixedDate(2026, 1, 9), "sleep")
        previous_sleep = store.read_json(sleep_path)

        def unavailable_sleep(client, iso):
            raise RuntimeError("private failure detail must not persist")

        with patch.object(
            collect.endpoints,
            "DAILY",
            {
                "stats": lambda client, iso: {"date": iso, "refresh": True},
                "sleep": unavailable_sleep,
            },
        ):
            exit_code = collect.main(["--date", "2026-01-09", "--weekly"])

        self.assertEqual(exit_code, 0)
        health = self.read_health()
        daily_domain = health["domains"]["daily"]
        daily = daily_domain["dates"]["2026-01-09"]
        self.assertEqual(health["run"]["outcome"], "degraded")
        self.assertEqual(daily_domain["outcome"], "degraded")
        self.assertEqual(daily["outcome"], "degraded")
        self.assertEqual(
            daily["anchor"], {"name": "stats", "outcome": "successful"}
        )
        self.assertEqual(
            daily["endpoints"], {"stats": "successful", "sleep": "failed"}
        )
        self.assertTrue(daily["complete_marker_written"])
        self.assertEqual(health["run"]["last_success_at"], previous_success_at)
        self.assertEqual(daily_domain["last_success_at"], previous_success_at)
        self.assertEqual(daily["last_success_at"], previous_success_at)
        self.assertEqual(store.read_json(sleep_path), previous_sleep)
        self.assertTrue(
            store.raw_daily(FixedDate(2026, 1, 9), "_complete").exists()
        )
        self.assertNotIn("private failure detail", json.dumps(health))


    def test_failed_health_prevents_stale_complete_marker_skip(self) -> None:
        old_date = FixedDate(2026, 1, 8)
        store.write_json(
            store.raw_daily(old_date, "_complete"),
            {"collected_at": old_date.isoformat()},
        )
        store.write_json(
            store.HEALTH_FILE,
            {
                "schema_version": 1,
                "run": {"last_success_at": None},
                "domains": {
                    "daily": {
                        "last_success_at": None,
                        "dates": {
                            old_date.isoformat(): {
                                "outcome": "failed",
                                "last_success_at": None,
                            }
                        },
                    }
                },
            },
        )
        attempted_dates: list[str] = []

        def record_stats(client, iso):
            attempted_dates.append(iso)
            return {"date": iso}

        with patch.object(
            collect.endpoints, "DAILY", {"stats": record_stats}
        ):
            collect.main(["--days", "3", "--no-activities"])

        self.assertIn(old_date.isoformat(), attempted_dates)

    def test_one_run_uses_one_collection_date_across_domains(self) -> None:
        DriftingDate.calls = 0
        with patch.object(collect, "date", DriftingDate):
            collect.main(
                ["--date", "2026-01-09", "--weekly", "--no-activities"]
            )

        health = self.read_health()
        self.assertEqual(health["domains"]["plans"]["date"], "2026-01-10")
        self.assertEqual(health["domains"]["profile"]["date"], "2026-01-10")
        self.assertTrue(
            store.raw_snapshot(
                "plans", FixedDate(2026, 1, 10), "workouts"
            ).exists()
        )
        self.assertTrue(
            store.raw_snapshot(
                "profile", FixedDate(2026, 1, 10), "user_profile"
            ).exists()
        )
        self.assertFalse(
            store.raw_snapshot(
                "plans", FixedDate(2026, 1, 11), "workouts"
            ).exists()
        )
        self.assertFalse(
            store.raw_snapshot(
                "profile", FixedDate(2026, 1, 11), "user_profile"
            ).exists()
        )

    def test_all_attempted_domains_can_fail_even_when_the_run_returns_zero(self) -> None:
        def unavailable(*args):
            raise RuntimeError("synthetic endpoint failure")

        class UnavailableActivitiesClient:
            def get_activities(self, start: int, limit: int):
                return unavailable(start, limit)

        with (
            patch.object(collect, "connect", return_value=UnavailableActivitiesClient()),
            patch.object(
                collect.endpoints,
                "DAILY",
                {"stats": unavailable, "sleep": unavailable},
            ),
            patch.object(collect.endpoints, "PLANS", {"workouts": unavailable}),
            patch.object(
                collect.endpoints, "SNAPSHOT", {"user_profile": unavailable}
            ),
        ):
            exit_code = collect.main(["--date", "2026-01-09", "--weekly"])

        self.assertEqual(exit_code, 0)
        health = self.read_health()
        self.assertEqual(health["run"]["outcome"], "failed")
        self.assertIsNone(health["run"]["last_success_at"])
        self.assert_utc_timestamp(health["run"]["attempted_at"])
        self.assert_utc_timestamp(health["run"]["finished_at"])
        for domain in health["domains"].values():
            self.assertEqual(domain["outcome"], "failed")
            self.assertIsNone(domain["last_success_at"])
            self.assert_utc_timestamp(domain["attempted_at"])
            self.assert_utc_timestamp(domain["finished_at"])
        daily = health["domains"]["daily"]["dates"]["2026-01-09"]
        self.assertEqual(daily["outcome"], "failed")
        self.assert_utc_timestamp(daily["attempted_at"])
        self.assert_utc_timestamp(daily["finished_at"])
        self.assertEqual(
            daily["anchor"], {"name": "stats", "outcome": "failed"}
        )
        self.assertFalse(daily["complete_marker_written"])
        self.assertIsNotNone(store.load_state().get("last_run"))
        log_text = next((self.root / "data" / "logs").iterdir()).read_text(
            encoding="utf-8"
        )
        self.assertIn("done", log_text)
        self.assertNotIn("synthetic endpoint failure", json.dumps(health))

    def test_fatal_domain_error_records_failure_and_is_reraised(self) -> None:
        with patch.object(
            collect.normalize,
            "normalize_daily",
            side_effect=RuntimeError("synthetic normalization failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "synthetic normalization failure"
            ):
                collect.main(["--date", "2026-01-09"])

        health = self.read_health()
        self.assertEqual(health["run"]["outcome"], "failed")
        self.assert_utc_timestamp(health["run"]["finished_at"])
        daily = health["domains"]["daily"]
        self.assertEqual(daily["outcome"], "failed")
        self.assert_utc_timestamp(daily["finished_at"])
        day = daily["dates"]["2026-01-09"]
        self.assertEqual(day["outcome"], "failed")
        self.assert_utc_timestamp(day["finished_at"])
        self.assertEqual(
            day["anchor"], {"name": "stats", "outcome": "successful"}
        )
        self.assertEqual(
            day["endpoints"], {"stats": "successful", "sleep": "successful"}
        )
        self.assertTrue(day["complete_marker_written"])
        self.assertTrue(
            all(
                health["domains"][name]["outcome"] == "not_attempted"
                for name in ("activities", "plans", "profile")
            )
        )
        self.assertNotIn("synthetic normalization failure", json.dumps(health))

    def test_interrupted_domain_and_date_remain_attempted(self) -> None:
        def interrupt(client, iso):
            raise KeyboardInterrupt

        with patch.object(collect.endpoints, "DAILY", {"stats": interrupt}):
            with self.assertRaises(KeyboardInterrupt):
                collect.main(["--date", "2026-01-09", "--no-activities"])

        health = self.read_health()
        self.assertEqual(health["run"]["outcome"], "attempted")
        self.assertIsNone(health["run"]["finished_at"])
        daily = health["domains"]["daily"]
        self.assertEqual(daily["outcome"], "attempted")
        self.assertIsNone(daily["finished_at"])
        day = daily["dates"]["2026-01-09"]
        self.assertEqual(day["outcome"], "attempted")
        self.assertIsNone(day["finished_at"])
        self.assertEqual(
            day["anchor"], {"name": "stats", "outcome": "attempted"}
        )
        self.assertTrue(
            all(
                health["domains"][name]["outcome"] == "not_attempted"
                for name in ("activities", "plans", "profile")
            )
        )

    def test_failed_activity_summary_retries_after_it_leaves_latest_page(
        self,
    ) -> None:
        class SlidingActivitiesClient:
            calls = 0

            def get_activities(self, start, limit):
                self.calls += 1
                if self.calls == 1:
                    return [
                        {
                            "activityId": "synthetic-activity",
                            "startTimeLocal": "2026-01-09T08:00:00",
                        }
                    ]
                return [
                    {
                        "activityId": "synthetic-newer",
                        "startTimeLocal": "2026-01-10T08:00:00",
                    }
                ]

        summary_attempts: dict[str, int] = {}
        splits_attempts: dict[str, int] = {}

        def flaky_summary(client, activity_id):
            summary_attempts[activity_id] = (
                summary_attempts.get(activity_id, 0) + 1
            )
            if (
                activity_id == "synthetic-activity"
                and summary_attempts[activity_id] == 1
            ):
                raise RuntimeError("synthetic summary failure")
            return {"activityName": "Synthetic Run"}

        def stable_splits(client, activity_id):
            splits_attempts[activity_id] = (
                splits_attempts.get(activity_id, 0) + 1
            )
            return {"source_attempt": splits_attempts[activity_id]}

        activity_date = FixedDate(2026, 1, 9)
        with (
            patch.object(
                collect, "connect", return_value=SlidingActivitiesClient()
            ),
            patch.object(
                collect.endpoints,
                "ACTIVITY_DETAIL",
                {"summary": flaky_summary, "splits": stable_splits},
            ),
        ):
            collect.main(["--date", "2026-01-09", "--activities-limit", "1"])

            first_state = store.load_state()
            first_health = self.read_health()["domains"]["activities"]
            splits_path = store.raw_activity(
                "synthetic-activity", "splits", activity_date
            )
            first_splits = store.read_json(splits_path)

            self.assertNotIn(
                "synthetic-activity", first_state.get("seen_activities", [])
            )
            self.assertEqual(first_health["outcome"], "degraded")
            self.assertEqual(
                first_health["anchors"], {"successful": 0, "failed": 1}
            )

            collect.main(["--date", "2026-01-09", "--activities-limit", "1"])

            second_state = store.load_state()
            second_health = self.read_health()["domains"]["activities"]
            self.assertEqual(
                set(second_state["seen_activities"]),
                {"synthetic-activity", "synthetic-newer"},
            )
            self.assertEqual(second_health["outcome"], "successful")
            self.assertEqual(second_health["activities_pending_retry"], 1)
            self.assertEqual(
                second_health["anchors"], {"successful": 2, "failed": 0}
            )
            self.assertEqual(second_health["detail_files_reused"], 1)
            self.assertEqual(store.read_json(splits_path), first_splits)

            collect.main(["--date", "2026-01-09", "--activities-limit", "1"])

        self.assertEqual(
            summary_attempts,
            {"synthetic-activity": 2, "synthetic-newer": 1},
        )
        self.assertEqual(
            splits_attempts,
            {"synthetic-activity": 1, "synthetic-newer": 1},
        )
        self.assertEqual(
            self.read_health()["domains"]["activities"]["activities_new"], 0
        )

    def test_completed_activity_evicted_from_seen_window_stays_complete(
        self,
    ) -> None:
        activity_date = FixedDate(2026, 1, 9)
        activity_ids = [f"synthetic-{index}" for index in range(501)]
        for activity_id in activity_ids:
            summary = {
                "activityId": activity_id,
                "startTimeLocal": "2026-01-09T08:00:00",
            }
            store.write_json(
                store.raw_activity(activity_id, "list_summary", activity_date),
                summary,
            )
            store.write_json(
                store.raw_activity(activity_id, "summary", activity_date),
                {"activityName": "Synthetic Session"},
            )
        store.save_state(
            {
                "seen_activities": activity_ids[1:],
                "last_snapshot": "2026-01-10",
            }
        )

        class EmptyActivitiesClient:
            def get_activities(self, start, limit):
                return []

        def unexpected_detail_call(client, activity_id):
            raise AssertionError("completed raw activity must not be retried")

        with (
            patch.object(
                collect, "connect", return_value=EmptyActivitiesClient()
            ),
            patch.object(
                collect.endpoints,
                "ACTIVITY_DETAIL",
                {"summary": unexpected_detail_call},
            ),
        ):
            collect.main(["--date", "2026-01-09"])

        health = self.read_health()["domains"]["activities"]
        self.assertEqual(health["activities_pending_retry"], 0)
        self.assertEqual(health["activities_new"], 0)
        self.assertEqual(health["cursor_advanced"], 0)
        self.assertEqual(store.load_state()["seen_activities"], activity_ids[1:])

    def test_incomplete_profile_retries_until_required_anchors_exist(
        self,
    ) -> None:
        calls = {
            "user_profile": 0,
            "full_name": 0,
            "personal_records": 0,
            "settings": 0,
        }

        def user_profile(client):
            calls["user_profile"] += 1
            return {"fixture": "profile", "source_attempt": calls["user_profile"]}

        def flaky_full_name(client):
            calls["full_name"] += 1
            if calls["full_name"] == 1:
                raise RuntimeError("synthetic profile-anchor failure")
            return {"fullName": "Synthetic Athlete"}

        def personal_records(client):
            calls["personal_records"] += 1
            return []

        def settings(client):
            calls["settings"] += 1
            return {"fixture": "settings"}

        snapshot_endpoints = {
            "user_profile": user_profile,
            "full_name": flaky_full_name,
            "personal_records": personal_records,
            "settings": settings,
        }
        collection_date = FixedDate(2026, 1, 10)
        with patch.object(collect.endpoints, "SNAPSHOT", snapshot_endpoints):
            collect.main(["--date", "2026-01-09", "--no-activities"])

            first_state = store.load_state()
            first_health = self.read_health()["domains"]["profile"]
            profile_path = store.raw_snapshot(
                "profile", collection_date, "user_profile"
            )
            first_profile = store.read_json(profile_path)

            self.assertNotIn("last_snapshot", first_state)
            self.assertEqual(first_health["outcome"], "degraded")
            self.assertEqual(
                first_health["anchors"],
                {
                    "full_name": "failed",
                    "personal_records": "successful",
                    "user_profile": "successful",
                },
            )

            collect.main(["--date", "2026-01-09", "--no-activities"])

            second_state = store.load_state()
            second_health = self.read_health()["domains"]["profile"]
            self.assertEqual(second_state["last_snapshot"], "2026-01-10")
            self.assertEqual(second_health["outcome"], "successful")
            self.assertEqual(second_health["files_reused"], 3)
            self.assertEqual(store.read_json(profile_path), first_profile)

            collect.main(["--date", "2026-01-09", "--no-activities"])

        self.assertEqual(
            calls,
            {
                "user_profile": 1,
                "full_name": 2,
                "personal_records": 1,
                "settings": 1,
            },
        )
        self.assertEqual(
            self.read_health()["domains"]["profile"]["outcome"],
            "not_attempted",
        )
if __name__ == "__main__":
    unittest.main()
