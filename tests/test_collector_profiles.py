from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import store
from tests import legacy_file_run_all as run_all
from coach.credentials import CredentialStore
from coach.profiles import ProfileRegistry


class StoreEnvRootTest(unittest.TestCase):
    def test_data_dir_env_overrides_base(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        try:
            with mock.patch.dict(os.environ, {"GARMIN_COACH_DATA_DIR": tmp}):
                importlib.reload(store)
                self.assertEqual(store.DATA, Path(tmp))
                self.assertEqual(store.RAW, Path(tmp) / "raw")
        finally:
            os.environ.pop("GARMIN_COACH_DATA_DIR", None)
            importlib.reload(store)


class RunAllTest(unittest.TestCase):
    def _seed_two_profiles(self, base: Path) -> tuple[ProfileRegistry, list]:
        registry = ProfileRegistry(base)
        creds = CredentialStore(base)
        a = registry.bind("user_a", display_name="A")
        b = registry.bind("user_b", display_name="B")
        creds.set_garmin(registry.data_root(a.id), "a@example.com", "pw-a")
        creds.set_garmin(registry.data_root(b.id), "b@example.com", "pw-b")
        return registry, [a, b]

    def test_main_runs_each_profile_serially_with_its_root_and_creds(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.dict(os.environ, {"GARMIN_COACH_DATA_DIR": str(base)}):
            registry, profiles = self._seed_two_profiles(base)
            calls: list[tuple] = []

            def record(profile_id, root, garmin, argv):
                calls.append((profile_id, root, garmin, argv))

            with mock.patch.object(run_all, "_run_profile", side_effect=record):
                code = run_all.main([])

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        expected = {
            profiles[0].id: (
                registry.data_root(profiles[0].id),
                {"email": "a@example.com", "password": "pw-a"},
            ),
            profiles[1].id: (
                registry.data_root(profiles[1].id),
                {"email": "b@example.com", "password": "pw-b"},
            ),
        }
        for profile_id, root, garmin, argv in calls:
            self.assertIn(profile_id, expected)
            exp_root, exp_creds = expected[profile_id]
            self.assertEqual(root, exp_root)
            self.assertEqual(garmin, exp_creds)
            self.assertEqual(argv, [])
        self.assertEqual({c[0] for c in calls}, set(expected))

    def test_failure_of_one_profile_does_not_stop_the_others(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.dict(os.environ, {"GARMIN_COACH_DATA_DIR": str(base)}):
            _registry, profiles = self._seed_two_profiles(base)
            attempted: list[str] = []
            first_id = profiles[0].id

            def flaky(profile_id, root, garmin, argv):
                attempted.append(profile_id)
                if profile_id == first_id:
                    raise RuntimeError("boom")

            with mock.patch.object(run_all, "_run_profile", side_effect=flaky):
                code = run_all.main([])

        self.assertNotEqual(code, 0)
        self.assertEqual(set(attempted), {p.id for p in profiles})

    def test_profile_without_credentials_is_skipped(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.dict(os.environ, {"GARMIN_COACH_DATA_DIR": str(base)}):
            registry = ProfileRegistry(base)
            creds = CredentialStore(base)
            with_creds = registry.bind("user_has", display_name="Has")
            without = registry.bind("user_none", display_name="None")
            creds.set_garmin(
                registry.data_root(with_creds.id), "has@example.com", "pw"
            )
            attempted: list[str] = []

            def record(profile_id, root, garmin, argv):
                attempted.append(profile_id)

            with mock.patch.object(run_all, "_run_profile", side_effect=record):
                code = run_all.main([])

        self.assertEqual(code, 0)
        self.assertEqual(attempted, [with_creds.id])
        self.assertNotIn(without.id, attempted)


if __name__ == "__main__":
    unittest.main()
