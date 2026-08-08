from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

from collector import endpoints
from collector.postgres_adapter import GarminCollectionAdapter, _GarminSession


class _WindowedGarmin:
    def __init__(self, today: date) -> None:
        self.activities = [
            {
                "activityId": "new-activity",
                "startTimeLocal": f"{today.isoformat()} 07:00:00",
            },
            {
                "activityId": "overlap-activity",
                "startTimeLocal": f"{(today - timedelta(days=7)).isoformat()} 07:00:00",
            },
            {
                "activityId": "stale-activity",
                "startTimeLocal": f"{(today - timedelta(days=9)).isoformat()} 07:00:00",
            },
        ]
        self.daily_dates: list[str] = []
        self.list_requests: list[tuple[int, int]] = []
        self.detail_requests: list[str] = []

    def get_stats(self, iso: str):
        self.daily_dates.append(iso)
        return {"totalSteps": 4321}

    def get_activities(self, start: int, limit: int):
        self.list_requests.append((start, limit))
        return self.activities[start : start + limit]

    def get_activity(self, activity_id: str):
        self.detail_requests.append(activity_id)
        listed = next(
            item for item in self.activities if item["activityId"] == activity_id
        )
        return {
            "activityName": activity_id,
            "activityTypeDTO": {"typeKey": "running"},
            "startTimeLocal": listed["startTimeLocal"],
            "duration": 1800,
            "distance": 5000,
        }


class PostgreSQLCollectorAdapterTest(unittest.TestCase):
    def test_incremental_uses_checkpoint_overlap_and_weekly_reconciliation(self) -> None:
        today = date.today()
        prior_through = today - timedelta(days=5)
        prior = {
            "through_date": prior_through.isoformat(),
            "last_activity_reconciliation": today.isoformat(),
        }
        with (
            patch.object(
                endpoints,
                "DAILY",
                {"stats": lambda client, iso: client.get_stats(iso)},
            ),
            patch.object(endpoints, "PLANS", {}),
            patch.object(endpoints, "SNAPSHOT", {}),
            patch.object(
                endpoints,
                "ACTIVITY_DETAIL",
                {"summary": lambda client, activity_id: client.get_activity(activity_id)},
            ),
        ):
            routine_client = _WindowedGarmin(today)
            routine = GarminCollectionAdapter().collect(
                _GarminSession(routine_client, initial_days=30, activity_limit=100),
                "incremental",
                prior,
            )
            weekly_client = _WindowedGarmin(today)
            weekly = GarminCollectionAdapter().collect(
                _GarminSession(weekly_client, initial_days=30, activity_limit=100),
                "incremental",
                {
                    **prior,
                    "last_activity_reconciliation": (
                        today - timedelta(days=7)
                    ).isoformat(),
                },
            )

        self.assertEqual(
            routine_client.daily_dates,
            [
                (today - timedelta(days=offset)).isoformat()
                for offset in range(0, 7)
            ],
        )
        self.assertEqual(routine_client.list_requests, [(0, 20)])
        self.assertEqual(
            routine_client.detail_requests,
            ["new-activity", "overlap-activity"],
        )
        self.assertEqual(
            routine.checkpoint.cursor["last_activity_reconciliation"],
            today.isoformat(),
        )
        self.assertEqual(
            weekly_client.detail_requests,
            ["new-activity", "overlap-activity", "stale-activity"],
        )
        self.assertEqual(
            weekly.checkpoint.cursor["last_activity_reconciliation"],
            today.isoformat(),
        )


if __name__ == "__main__":
    unittest.main()
