from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coach.data import ReadOnlyRecord, RevisionConflict
from coach.mcp_server import (
    create_training_session,
    delete_training_session,
    get_training_session,
    list_training_sessions,
    replace_training_session,
)


class McpAppSessionToolsTest(unittest.TestCase):
    def test_full_lifecycle_through_configured_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ, {"GARMIN_COACH_DATA_DIR": tmp}, clear=False
            ):
                created = create_training_session(
                    sport="bouldering",
                    local_date="2026-07-18",
                    session_rpe=6,
                    duration={"value": 3600, "unit": "seconds", "basis": "active"},
                    notes="Session A",
                )
                self.assertEqual(created["origin"], "app_record")
                self.assertEqual(created["revision"], 1)
                session_id = created["id"]

                listed = list_training_sessions(days=7, end_date="2026-07-18")
                self.assertIn(session_id, {s["id"] for s in listed["sessions"]})

                fetched = get_training_session(session_id)
                self.assertEqual(fetched["id"], session_id)

                replaced = replace_training_session(
                    session_id=session_id,
                    expected_revision=1,
                    sport="bouldering",
                    local_date="2026-07-18",
                    session_rpe=8,
                    notes="Session A revised",
                )
                self.assertEqual(replaced["revision"], 2)
                self.assertEqual(replaced["session_rpe"], 8)

                with self.assertRaises(RevisionConflict):
                    replace_training_session(
                        session_id=session_id,
                        expected_revision=1,
                        sport="bouldering",
                        local_date="2026-07-18",
                    )

                deleted = delete_training_session(session_id, expected_revision=2)
                self.assertTrue(deleted["deleted"])
                # A synthetic persisted file must be gone; the app dir stays clean.
                stem = session_id.split(":", 1)[1]
                self.assertFalse(
                    (Path(tmp) / "app" / "sessions" / f"{stem}.json").exists()
                )

    def test_garmin_ids_cannot_be_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ, {"GARMIN_COACH_DATA_DIR": tmp}, clear=False
            ):
                with self.assertRaises(ReadOnlyRecord):
                    replace_training_session(
                        session_id="garmin:123",
                        expected_revision=1,
                        sport="running",
                        local_date="2026-07-18",
                    )
                with self.assertRaises(ReadOnlyRecord):
                    delete_training_session("garmin:123", expected_revision=1)


if __name__ == "__main__":
    unittest.main()
