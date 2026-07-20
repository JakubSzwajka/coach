from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coach.profiles import (
    Profile,
    ProfileNotFound,
    ProfileRegistry,
    RegistryIntegrityError,
)


class ProfileRegistryTest(unittest.TestCase):
    def _registry(self) -> tuple[ProfileRegistry, Path]:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return ProfileRegistry(tmp), tmp

    def test_bind_creates_opaque_id_and_is_idempotent(self) -> None:
        registry, _ = self._registry()

        first = registry.bind("user_owner", display_name="Owner")
        again = registry.bind("user_owner")

        self.assertIsInstance(first, Profile)
        self.assertEqual(first.id, again.id)
        self.assertRegex(first.id, r"^[0-9a-f]{32}$")
        self.assertEqual(first.clerk_subject, "user_owner")
        self.assertEqual(first.display_name, "Owner")

    def test_distinct_subjects_get_distinct_profiles_and_roots(self) -> None:
        registry, base = self._registry()

        a = registry.bind("user_a")
        b = registry.bind("user_b")

        self.assertNotEqual(a.id, b.id)
        self.assertEqual(
            registry.data_root(a.id), base / "profiles" / a.id
        )
        self.assertNotEqual(registry.data_root(a.id), registry.data_root(b.id))

    def test_resolve_returns_bound_id_or_none(self) -> None:
        registry, _ = self._registry()
        bound = registry.bind("user_a")

        self.assertEqual(registry.resolve("user_a"), bound.id)
        self.assertIsNone(registry.resolve("user_unknown"))

    def test_bind_fills_blank_display_name_without_reassigning(self) -> None:
        registry, _ = self._registry()
        first = registry.bind("user_a")
        self.assertIsNone(first.display_name)

        updated = registry.bind("user_a", display_name="Later Name")
        self.assertEqual(updated.id, first.id)
        self.assertEqual(updated.display_name, "Later Name")

    def test_list_returns_all_profiles_oldest_first(self) -> None:
        registry, _ = self._registry()
        registry.bind("user_a")
        registry.bind("user_b")

        subjects = [profile.clerk_subject for profile in registry.list()]
        self.assertEqual(subjects, ["user_a", "user_b"])

    def test_get_unknown_profile_raises(self) -> None:
        registry, _ = self._registry()
        with self.assertRaises(ProfileNotFound):
            registry.get("f" * 32)

    def test_delete_purges_data_root_and_registry_row(self) -> None:
        registry, _ = self._registry()
        profile = registry.bind("user_a")
        root = registry.data_root(profile.id)
        (root / "derived").mkdir(parents=True)
        (root / "derived" / "athlete.json").write_text("{}", encoding="utf-8")

        registry.delete(profile.id)

        self.assertFalse(root.exists())
        self.assertIsNone(registry.resolve("user_a"))
        with self.assertRaises(ProfileNotFound):
            registry.get(profile.id)

    def test_delete_unknown_profile_raises(self) -> None:
        registry, _ = self._registry()
        with self.assertRaises(ProfileNotFound):
            registry.delete("a" * 32)

    def test_invalid_profile_id_is_rejected(self) -> None:
        registry, _ = self._registry()
        with self.assertRaises(Exception):
            registry.data_root("../escape")

    def test_binding_persists_across_registry_instances(self) -> None:
        registry, base = self._registry()
        bound = registry.bind("user_a", display_name="A")

        reopened = ProfileRegistry(base)
        self.assertEqual(reopened.resolve("user_a"), bound.id)
        self.assertEqual(reopened.get(bound.id).display_name, "A")

    def test_corrupt_registry_is_reported(self) -> None:
        registry, base = self._registry()
        registry.bind("user_a")
        (base / "profiles" / "registry.json").write_text(
            "not json", encoding="utf-8"
        )
        with self.assertRaises(RegistryIntegrityError):
            registry.list()


if __name__ == "__main__":
    unittest.main()
