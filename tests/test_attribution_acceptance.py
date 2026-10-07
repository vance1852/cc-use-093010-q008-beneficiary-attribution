from __future__ import annotations

import unittest
from pathlib import Path

from benefit_attribution.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_full_scenario(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["attribution_versions"], [1, 2, 3])
        self.assertTrue(result["published_version_preserved"])
        self.assertTrue(result["duplicate_callback_replayed"])
        self.assertEqual(result["final_shared_split"],
                         {"c-outcome-assoc": "22500.00", "c-outcome-park": "7500.00"})
        self.assertEqual(result["open_disputes"], 0)
        self.assertEqual(result["effective_beneficiaries"], 1)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["schema"]["schema_version"], "1")


if __name__ == "__main__":
    unittest.main()
