from __future__ import annotations

import json
import sqlite3
import unittest

from benefit_attribution.api import JsonApplication
from benefit_attribution.jsonio import load_json
from benefit_attribution.service import AttributionService


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
SHA = "c" * 64


class AttributionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(AttributionService(self.connection))
        for uid, role in (
            ("steward", "steward"), ("analyst", "analyst"),
            ("reviewer", "reviewer"), ("auditor", "auditor"),
        ):
            self.app.handle("POST", "/users", body=json.dumps(
                {"user_id": uid, "display_name": uid, "role": role}).encode())
        policy = load_json(ROOT / "fixtures" / "demo_attribution_policy_v1.json")
        response = self.app.handle(
            "POST", "/policies", headers={"x-actor-id": "analyst"},
            body=json.dumps({"policy": policy}).encode(),
        )
        self.assertEqual(response.status, 201)

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "steward"):
        return self.app.handle(
            "POST", path, headers={"x-actor-id": actor},
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def _get(self, path: str, actor: str = "auditor"):
        return self.app.handle("GET", path, headers={"x-actor-id": actor})

    def _seed(self) -> None:
        self.assertEqual(self._post("/organizations", {
            "organization_id": "park", "name": "园区", "organization_kind": "park",
            "region_code": "R1",
        }).status, 201)
        self._post("/applicants", {
            "applicant_id": "a", "name": "星河智造", "applicant_kind": "enterprise",
            "region_code": "R1", "identities": [{"identity_type": "business_license", "value": "L1"}],
        })
        self._post("/applicants", {
            "applicant_id": "b", "name": "星河经营部", "applicant_kind": "enterprise",
            "region_code": "R1", "identities": [{"identity_type": "business_license", "value": "L1"}],
        })
        self._post("/beneficiaries", {
            "beneficiary_id": "ben", "name": "星河智造", "beneficiary_kind": "enterprise",
            "region_code": "R1",
        })
        self._post("/resources", {
            "resource_id": "res", "program_id": "prog", "resource_type": "grant",
            "region_code": "R1",
        })
        self._post("/claims", {
            "claim_id": "cl-a", "applicant_id": "a", "beneficiary_id": "ben",
            "program_id": "prog", "region_code": "R1", "resource_id": "res",
            "chain": ["park"],
        })
        self._post("/claims", {
            "claim_id": "cl-b", "applicant_id": "b", "beneficiary_id": "ben",
            "program_id": "prog", "region_code": "R1",
        })
        self._post("/milestones", {
            "milestone_id": "m1", "evidence_type": "business_improvement",
            "evidence_level": 3, "occurred_at": "2026-02-20", "content_sha256": SHA,
            "claim_id": "cl-a",
        })

    def test_health_and_missing_actor(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").body["status"], "ok")
        response = self.app.handle("POST", "/applicants", body=b"{}")
        self.assertEqual(response.status, 422)
        self.assertIn("X-Actor-Id", response.body["error"]["message"])

    def test_duplicate_detection_via_api(self) -> None:
        self._seed()
        response = self._post("/duplicates/ai-support-attribution/detect",
                              {"version": 1}, actor="auditor")
        self.assertEqual(response.status, 200)
        members = [tuple(c["applicant_ids"]) for c in response.body["clusters"]]
        self.assertIn(("a", "b"), members)

    def test_callback_dedup_and_lineage(self) -> None:
        self._seed()
        outcome = {
            "outcome_id": "o1", "outcome_type": "business_improvement", "outcome_key": "biz",
            "claim_id": "cl-a", "value": "500", "occurred_at": "2026-03-01",
            "evidence_level": 3, "milestone_ids": ["m1"],
        }
        first = self._post("/callbacks", {"callback_id": "cb1", "source": "portal", "payload": outcome})
        replay = self._post("/callbacks", {"callback_id": "cb2", "source": "portal", "payload": outcome})
        self.assertEqual(first.status, 201)
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay.body["status"], "duplicate")

        attributed = self._post(
            "/outcomes/biz/attribute", {"policy_id": "ai-support-attribution", "version": 1},
            actor="analyst",
        )
        self.assertEqual(attributed.status, 201)
        attr_id = attributed.body["attribution_id"]
        self.assertEqual(attributed.body["result"]["total_share"], "1.000000")

        published = self._post(
            f"/attributions/{attr_id}/publish",
            {"channel": "dashboard", "receipt_sha256": SHA}, actor="analyst",
        )
        self.assertEqual(published.status, 200)

        # 异议与独立复核走完整路由。
        dispute = self._post("/disputes", {"attribution_id": attr_id, "reason": "有异议"})
        self.assertEqual(dispute.status, 201)
        reviewed = self._post(
            f"/disputes/{dispute.body['dispute_id']}/review", {}, actor="reviewer"
        )
        self.assertEqual(reviewed.status, 200)
        completed = self._post(
            f"/disputes/{dispute.body['dispute_id']}/complete",
            {"decision": "dismiss", "finding": "维持"}, actor="reviewer",
        )
        self.assertEqual(completed.status, 200)

        lineage = self._get("/outcomes/biz/lineage")
        self.assertEqual(lineage.status, 200)
        self.assertEqual(lineage.body["attribution_versions"][0]["publication"]["channel"],
                         "dashboard")
        self.assertEqual(lineage.body["outcomes"][0]["claim"]["chain"][0]["organization_id"],
                         "park")

        coverage = self._get("/reports/coverage/ai-support-attribution?version=1")
        self.assertEqual(coverage.body["covered_region_count"], 1)
        views = self._get("/claim_views?program_id=prog")
        self.assertEqual({v["claim_id"] for v in views.body["views"]}, {"cl-a", "cl-b"})

    def test_publish_locks_against_second_publication(self) -> None:
        self._seed()
        self._post("/outcomes", {
            "outcome_id": "o1", "outcome_type": "business_improvement", "outcome_key": "z",
            "claim_id": "cl-a", "value": "1", "occurred_at": "2026-03-01",
            "evidence_level": 3, "milestone_ids": ["m1"],
        })
        attr = self._post("/outcomes/z/attribute",
                          {"policy_id": "ai-support-attribution"}, actor="analyst")
        attr_id = attr.body["attribution_id"]
        self.assertEqual(self._post(f"/attributions/{attr_id}/publish",
                                    {"channel": "c", "receipt_sha256": SHA},
                                    actor="analyst").status, 200)
        again = self._post(f"/attributions/{attr_id}/publish",
                           {"channel": "c", "receipt_sha256": SHA}, actor="analyst")
        self.assertEqual(again.status, 409)

    def test_unknown_route_and_bad_json(self) -> None:
        self.assertEqual(self.app.handle("GET", "/nope").status, 404)
        response = self.app.handle("POST", "/policies", headers={"x-actor-id": "analyst"},
                                   body=b"not-json")
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
