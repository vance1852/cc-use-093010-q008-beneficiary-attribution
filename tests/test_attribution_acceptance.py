from __future__ import annotations

import unittest
from pathlib import Path

from benefit_attribution.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AttributionAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["attribution_versions"], [1, 2, 3])
        self.assertTrue(result["published_version_locked"])
        self.assertEqual(result["duplicate_callback"], "duplicate")
        self.assertEqual(result["v2_shares"], ["0.500000", "0.500000"])
        self.assertEqual(result["v2_total_conserved"], "100000.000000")
        self.assertEqual(result["dispute_final_status"], "upheld")
        self.assertEqual(result["coverage_v1_regions"], 1)
        self.assertEqual(result["coverage_v1_effective_beneficiaries"], 1)
        self.assertEqual(result["schema"]["missing_tables"], [])


if __name__ == "__main__":
    unittest.main()
