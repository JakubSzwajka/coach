import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coach.credentials import CredentialStore
from coach.profiles import ProfileRegistry
from tests import legacy_file_web_job as web_job


class WebJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, payload: dict, collector_outcome: str = "successful") -> int:
        def collect(_profile_id, root, garmin, argv, progress=None):
            self.collected_root = root
            self.collected_garmin = garmin
            self.collected_argv = argv
            if progress is not None:
                progress("[2026-07-20T21:24:02] authenticated")
                progress("[2026-07-20T21:24:03] daily 2026-07-20")
            health = root / "index" / "collector-health.json"
            health.parent.mkdir(parents=True, exist_ok=True)
            health.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run": {"outcome": collector_outcome},
                        "domains": {},
                    }
                ),
                encoding="utf-8",
            )

        request = io.StringIO(json.dumps(payload))
        with (
            patch.dict(os.environ, {"GARMIN_COACH_DATA_DIR": str(self.base)}),
            patch.object(web_job.sys, "stdin", request),
            patch.object(web_job.run_all, "_run_profile", side_effect=collect),
        ):
            return web_job.main()

    def _status(self, subject: str) -> dict:
        return json.loads(web_job._status_path(self.base, subject).read_text(encoding="utf-8"))

    def test_connect_binds_subject_without_claiming_legacy_flat_data(self) -> None:
        legacy = self.base / "derived"
        legacy.mkdir()
        legacy_athlete = legacy / "athlete.json"
        legacy_athlete.write_text('{"full_name":"Test"}', encoding="utf-8")
        password = "  exact password  "

        result = self._run(
            {
                "action": "connect",
                "subject": "user_owner",
                "email": "owner@example.com",
                "password": password,
                "days": 30,
            }
        )

        self.assertEqual(result, 0)
        profile_id = ProfileRegistry(self.base).resolve("user_owner")
        self.assertIsNotNone(profile_id)
        root = ProfileRegistry(self.base).data_root(profile_id)
        self.assertTrue(legacy_athlete.exists())
        self.assertFalse((root / "derived" / "athlete.json").exists())
        self.assertEqual(
            CredentialStore(self.base).get_garmin(root),
            {"email": "owner@example.com", "password": password},
        )
        self.assertEqual(self.collected_root, root)
        self.assertEqual(self.collected_argv, ["--days", "30"])
        self.assertEqual(self._status("user_owner")["state"], "succeeded")
        self.assertNotIn("user_owner", web_job._status_path(self.base, "user_owner").name)
        self.assertNotIn(password, (root / "secrets" / "garmin.enc").read_text(encoding="utf-8"))

    def test_refresh_uses_bound_profiles_stored_credentials(self) -> None:
        registry = ProfileRegistry(self.base)
        profile = registry.bind("user_owner")
        root = registry.data_root(profile.id)
        CredentialStore(self.base).set_garmin(root, "owner@example.com", "secret")

        result = self._run({"action": "refresh", "subject": "user_owner"})

        self.assertEqual(result, 0)
        self.assertEqual(self.collected_root, root)
        self.assertEqual(self.collected_garmin["email"], "owner@example.com")
        self.assertEqual(self.collected_argv, [])
        self.assertEqual(self._status("user_owner")["action"], "refresh")

    def test_degraded_collector_health_is_not_reported_as_success(self) -> None:
        result = self._run(
            {
                "action": "connect",
                "subject": "user_owner",
                "email": "owner@example.com",
                "password": "secret",
                "days": 7,
            },
            collector_outcome="degraded",
        )

        self.assertEqual(result, 1)
        status = self._status("user_owner")
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "collection_degraded")
        serialized = json.dumps(status)
        self.assertNotIn("user_owner", serialized)
        self.assertNotIn("owner@example.com", serialized)
        self.assertNotIn("secret", serialized)

    def test_invalid_days_fail_without_creating_credentials(self) -> None:
        result = self._run(
            {
                "action": "connect",
                "subject": "user_owner",
                "email": "owner@example.com",
                "password": "secret",
                "days": 0,
            }
        )

        self.assertEqual(result, 2)
        self.assertIsNone(ProfileRegistry(self.base).resolve("user_owner"))
        self.assertEqual(self._status("user_owner")["error"], "invalid_request")

    def test_progress_reporter_tracks_days_and_sanitized_stages(self) -> None:
        report = web_job._progress_reporter(
            self.base, "user_owner", action="connect", daily_total=365
        )

        report("[2026-07-20T21:24:02] authenticated")
        report("[2026-07-20T21:24:03] daily 2026-07-20")
        report("[2026-07-20T21:24:04]   skip sleep: private Garmin error")
        report("[2026-07-20T21:24:05] daily 2026-07-19")

        status = self._status("user_owner")
        self.assertEqual(status["step"], "daily")
        self.assertEqual(status["progress"], {"current": 2, "total": 365})
        self.assertNotIn("2026-07-20T21:24:03", json.dumps(status))
        self.assertNotIn("private Garmin error", json.dumps(status))

        report("[2026-07-20T21:24:06] activities (checking latest 15)")
        status = self._status("user_owner")
        self.assertEqual(status["step"], "activities")
        self.assertNotIn("progress", status)


if __name__ == "__main__":
    unittest.main()
