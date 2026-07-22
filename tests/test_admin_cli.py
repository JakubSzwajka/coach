from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import legacy_file_admin_cli as admin_cli
from coach.credentials import CredentialStore
from coach.profiles import ProfileRegistry
from tests import legacy_file_run_all as run_all


class AdminCliTest(unittest.TestCase):
    def _base(self) -> str:
        return self.enterContext(tempfile.TemporaryDirectory())

    def _run(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = admin_cli.main(argv)
        return code, out.getvalue()

    def test_create_then_list_shows_subject(self) -> None:
        base = self._base()
        code, created = self._run(
            ["--base", base, "create", "--subject", "user_owner"]
        )
        self.assertEqual(code, 0)
        profile_id = created.strip()

        code, listing = self._run(["--base", base, "list"])
        self.assertEqual(code, 0)
        self.assertIn("user_owner", listing)
        self.assertIn(profile_id, listing)

    def test_set_garmin_with_password_stores_credentials(self) -> None:
        base = self._base()
        code, _ = self._run(
            [
                "--base", base, "set-garmin",
                "--subject", "user_owner",
                "--email", "owner@example.com",
                "--password", "s3cret",
            ]
        )
        self.assertEqual(code, 0)

        registry = ProfileRegistry(base)
        pid = registry.resolve("user_owner")
        root = registry.data_root(pid)
        self.assertEqual(
            CredentialStore(base).get_garmin(root),
            {"email": "owner@example.com", "password": "s3cret"},
        )

    def test_set_garmin_reads_password_from_stdin(self) -> None:
        base = self._base()
        with mock.patch("sys.stdin", io.StringIO("stdin-pass\n")):
            code, out = self._run(
                [
                    "--base", base, "set-garmin",
                    "--subject", "user_owner",
                    "--email", "owner@example.com",
                    "--password-stdin",
                ]
            )
        self.assertEqual(code, 0)
        self.assertNotIn("stdin-pass", out)

        registry = ProfileRegistry(base)
        root = registry.data_root(registry.resolve("user_owner"))
        self.assertEqual(
            CredentialStore(base).get_garmin(root),
            {"email": "owner@example.com", "password": "stdin-pass"},
        )

    def test_delete_requires_yes_then_purges(self) -> None:
        base = self._base()
        self._run(["--base", base, "create", "--subject", "user_owner"])
        registry = ProfileRegistry(base)
        pid = registry.resolve("user_owner")
        root = registry.data_root(pid)
        root.mkdir(parents=True, exist_ok=True)
        (root / "marker").write_text("x", encoding="utf-8")

        code, _ = self._run(["--base", base, "delete", "--subject", "user_owner"])
        self.assertNotEqual(code, 0)
        self.assertEqual(ProfileRegistry(base).resolve("user_owner"), pid)

        code, _ = self._run(
            ["--base", base, "delete", "--subject", "user_owner", "--yes"]
        )
        self.assertEqual(code, 0)
        self.assertIsNone(ProfileRegistry(base).resolve("user_owner"))
        self.assertFalse(root.exists())

    def test_backfill_invokes_run_profile_with_creds_and_days(self) -> None:
        base = self._base()
        self._run(
            [
                "--base", base, "set-garmin",
                "--subject", "user_owner",
                "--email", "owner@example.com",
                "--password", "s3cret",
            ]
        )
        registry = ProfileRegistry(base)
        pid = registry.resolve("user_owner")
        root = registry.data_root(pid)

        calls: list[tuple] = []

        def record(profile_id, prof_root, garmin, argv):
            calls.append((profile_id, prof_root, garmin, argv))

        with mock.patch.object(run_all, "_run_profile", side_effect=record):
            code, _ = self._run(
                ["--base", base, "backfill", "--subject", "user_owner", "--days", "3"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        profile_id, prof_root, garmin, argv = calls[0]
        self.assertEqual(profile_id, pid)
        self.assertEqual(prof_root, root)
        self.assertEqual(garmin, {"email": "owner@example.com", "password": "s3cret"})
        self.assertEqual(argv, ["--days", "3"])

    def test_backfill_without_credentials_fails_and_skips_run(self) -> None:
        base = self._base()
        self._run(["--base", base, "create", "--subject", "user_owner"])

        with mock.patch.object(run_all, "_run_profile") as stub:
            code, _ = self._run(
                ["--base", base, "backfill", "--subject", "user_owner"]
            )

        self.assertNotEqual(code, 0)
        stub.assert_not_called()


if __name__ == "__main__":
    unittest.main()
