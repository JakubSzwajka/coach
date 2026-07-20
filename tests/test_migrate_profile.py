from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from coach.migrate_profile import main
from coach.profiles import ProfileRegistry


class MigrateProfileTest(unittest.TestCase):
    def _base(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _seed_flat(self, base: Path) -> None:
        (base / "derived").mkdir(parents=True)
        (base / "derived" / "athlete.json").write_text(
            json.dumps({"full_name": "Owner"}), encoding="utf-8"
        )
        (base / "raw").mkdir(parents=True)
        (base / "raw" / "x").write_text("x", encoding="utf-8")
        (base / "index").mkdir(parents=True)
        (base / "index" / "state.json").write_text("{}", encoding="utf-8")

    def test_happy_path_moves_flat_dirs_into_profile_root(self) -> None:
        base = self._base()
        self._seed_flat(base)

        code = main(["--owner-subject", "user_owner", "--base", str(base)])
        self.assertEqual(code, 0)

        profile_id = ProfileRegistry(base).resolve("user_owner")
        self.assertIsNotNone(profile_id)
        root = base / "profiles" / profile_id

        self.assertEqual(
            json.loads((root / "derived" / "athlete.json").read_text()),
            {"full_name": "Owner"},
        )
        self.assertEqual((root / "raw" / "x").read_text(), "x")
        self.assertEqual((root / "index" / "state.json").read_text(), "{}")

        self.assertFalse((base / "derived").exists())
        self.assertFalse((base / "raw").exists())
        self.assertFalse((base / "index").exists())

    def test_profiles_dir_and_secret_key_untouched(self) -> None:
        base = self._base()
        self._seed_flat(base)
        secret = base / "secret.key"
        secret.write_bytes(b"shared-key")

        main(["--owner-subject", "user_owner", "--base", str(base)])

        self.assertTrue(secret.exists())
        self.assertEqual(secret.read_bytes(), b"shared-key")
        self.assertTrue((base / "profiles").is_dir())

    def test_idempotent_second_run_does_not_clobber(self) -> None:
        base = self._base()
        self._seed_flat(base)

        self.assertEqual(
            main(["--owner-subject", "user_owner", "--base", str(base)]), 0
        )
        # A second run must not error and must leave migrated data intact.
        self.assertEqual(
            main(["--owner-subject", "user_owner", "--base", str(base)]), 0
        )

        profile_id = ProfileRegistry(base).resolve("user_owner")
        root = base / "profiles" / profile_id
        self.assertEqual(
            json.loads((root / "derived" / "athlete.json").read_text()),
            {"full_name": "Owner"},
        )
        self.assertEqual((root / "raw" / "x").read_text(), "x")

    def test_dry_run_makes_no_changes_and_does_not_bind(self) -> None:
        base = self._base()
        self._seed_flat(base)

        code = main(
            ["--owner-subject", "user_owner", "--base", str(base), "--dry-run"]
        )
        self.assertEqual(code, 0)

        self.assertTrue((base / "derived").exists())
        self.assertTrue((base / "raw").exists())
        self.assertTrue((base / "index").exists())
        self.assertIsNone(ProfileRegistry(base).resolve("user_owner"))
        self.assertFalse((base / "profiles").exists())

    def test_only_known_entries_are_moved(self) -> None:
        base = self._base()
        self._seed_flat(base)
        readme = base / "README.txt"
        readme.write_text("keep me", encoding="utf-8")

        main(["--owner-subject", "user_owner", "--base", str(base)])

        self.assertTrue(readme.exists())
        self.assertEqual(readme.read_text(), "keep me")
        profile_id = ProfileRegistry(base).resolve("user_owner")
        self.assertFalse((base / "profiles" / profile_id / "README.txt").exists())


if __name__ == "__main__":
    unittest.main()
