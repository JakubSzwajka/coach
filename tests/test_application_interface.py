from __future__ import annotations

import inspect
import unittest
from uuid import uuid4

from coach.application import (
    CoachApplication,
    CollectedBatch,
    CollectedCapture,
    CollectionJobRef,
    GetCollectedRecord,
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
        operations = {
            name
            for name, value in inspect.getmembers(
                CoachApplication, predicate=inspect.isfunction
            )
            if not name.startswith("_")
        }
        self.assertEqual(operations, {"read", "execute", "ingest"})
        with self.assertRaises(TypeError):
            ProfileRef(uuid4())
        with self.assertRaises(TypeError):
            SourceConnectionRef(uuid4(), uuid4())
        with self.assertRaises(TypeError):
            CollectionJobRef(uuid4(), uuid4())

        source = SourceConnectionRef._from_uuids(uuid4(), uuid4())
        capture = CollectedCapture(
            "arbitrary-private-record-kind",
            "private-source-key",
            {"private": "payload"},
        )
        query = GetCollectedRecord(
            source,
            "arbitrary-private-record-kind",
            "private-source-key",
        )
        batch = CollectedBatch(source, (capture,), "private-idempotency-key")
        self.assertEqual(repr(query), "GetCollectedRecord(<redacted>)")
        self.assertEqual(repr(capture), "CollectedCapture(<redacted>)")
        self.assertEqual(
            repr(batch),
            "CollectedBatch(source=SourceConnectionRef(<opaque>), <redacted>)",
        )
        self.assertEqual(repr(batch.captures[0]), "CollectedCapture(<redacted>)")

    def test_invalid_interface_inputs_fail_before_database_access(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.application.read(object(), object())  # type: ignore[arg-type]
        with self.assertRaises(InvalidRequest):
            self.application.ingest(object(), object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
