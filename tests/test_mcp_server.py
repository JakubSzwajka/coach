from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.legacy_mcp import legacy_mcp_adapter

from coach.mcp_server import read_coaching_context


class McpServerTest(unittest.TestCase):
    def test_read_coaching_context_uses_configured_store_and_bounded_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "derived" / "athlete.json",
                {"snapshot_date": "2026-07-18", "full_name": "Synthetic Athlete"},
            )
            self._write_json(
                root / "derived" / "daily" / "2026-07-18.json",
                {"date": "2026-07-18", "hrv_avg": None},
            )
            self._write_json(root / "derived" / "activities.json", [])

            with legacy_mcp_adapter(root):
                result = read_coaching_context(days=2, end_date="2026-07-18")

            self.assertEqual(
                result["window"],
                {
                    "start_date": "2026-07-17",
                    "end_date": "2026-07-18",
                    "days": 2,
                },
            )
            self.assertEqual(result["wellness"]["missing_dates"], ["2026-07-17"])
            self.assertIsNone(result["wellness"]["records"][0]["hrv_avg"])
            self.assertIsNone(result["collection_health"])
            self.assertEqual(result["training_history"], [])

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
