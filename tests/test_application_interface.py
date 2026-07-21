from __future__ import annotations

import unittest
from uuid import uuid4

from coach.application import (
    CoachApplication,
    InvalidRequest,
    ProfileRef,
    SourceConnectionRef,
)
from coach.postgres import DatabaseSettings


class CoachApplicationInterfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.application = CoachApplication(
            DatabaseSettings.from_url(
                "postgresql://coach:synthetic@127.0.0.1:1/coach"
            )
        )

    def test_opaque_capabilities_cannot_be_constructed_by_callers(self) -> None:
        with self.assertRaises(TypeError):
            ProfileRef(uuid4())
        with self.assertRaises(TypeError):
            SourceConnectionRef(uuid4(), uuid4())

    def test_invalid_interface_inputs_fail_before_database_access(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.application.read(object(), object())  # type: ignore[arg-type]
        with self.assertRaises(InvalidRequest):
            self.application.ingest(object(), object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
