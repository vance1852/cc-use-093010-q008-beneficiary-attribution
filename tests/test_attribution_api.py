from __future__ import annotations

import json
import sqlite3
import unittest

from benefit_attribution.api import JsonApplication
from benefit_attribution.service import AttributionService

POLICY = {
    "policy_id": "p", "version": 1, "title": "v1",
    "effective_from": "2026-01-01T00:00:00Z",
    "rules": ["LEGAL_IDENTITY", "SAME_EVIDENCE", "SHARED_RESOURCE"],
    "share_strategy": "equal",
}


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(AttributionService(self.connection))
        for uid, role in (
            ("editor", "registry_editor"), ("padmin", "policy_admin"),
            ("analyst", "analyst"), ("boss", "manager"), ("auditor", "auditor"),
        ):
            self.app.handle("POST", "/users", body=json.dumps(
                {"user_id": uid, "display_name": uid, "role": role}).encode())

    def tearDown(self) -> None:
        self.connection.close()

    def request(self, method: str, path: str, actor: str | None = None, payload=None,
                extra_headers=None):
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"] = actor
        if extra_headers:
            headers.update(extra_headers)
        body = b"" if payload is None else json.dumps(payload).encode()
        return self.app.handle(method, path, headers=headers, body=body)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_required(self) -> None:
        response = self.request("POST", "/policies", payload=POLICY)
        self.assertEqual(response.status, 422)

    def test_full_flow_over_http(self) -> None:
        r = self.request("POST", "/policies", "padmin", POLICY)
        self.assertEqual(r.status, 201)
        self.assertEqual(self.request("POST", "/beneficiaries", "editor", {
            "beneficiary_id": "b1", "name": "甲", "beneficiary_kind": "enterprise",
            "region_code": "BJ"}).status, 201)
        self.assertEqual(self.request("POST", "/applicants", "editor", {
            "applicant_id": "a1", "name": "园区", "applicant_kind": "park",
            "region_code": "BJ", "channel": "ch"}).status, 201)
        self.assertEqual(self.request("POST", "/resources", "editor", {
            "resource_id": "r1", "resource_kind": "advisory", "program_id": "prog",
            "region_code": "BJ"}).status, 201)
        self.assertEqual(self.request("POST", "/evidence", "editor", {
            "evidence_id": "e1", "milestone_kind": "business_outcome",
            "occurred_at": "2026-09-01T00:00:00Z", "reporting_org": "机构"}).status, 201)
        claim = {
            "applicant_id": "a1", "beneficiary_id": "b1", "resource_id": "r1",
            "milestone_kind": "business_outcome", "outcome_value": 100,
            "evidence_id": "e1", "reported_by_org": "机构"}
        self.assertEqual(self.request("POST", "/claims", "editor",
                                      dict(claim, claim_id="c1")).status, 201)
        # 回调幂等：重复推送回放。
        callback = dict(claim, claim_id="c2")
        first = self.request("POST", "/callbacks", "editor", callback,
                             {"Idempotency-Key": "cb-1"})
        replay = self.request("POST", "/callbacks", "editor", callback,
                              {"Idempotency-Key": "cb-1"})
        self.assertEqual(first.status, 200)
        self.assertFalse(first.body["replayed"])
        self.assertTrue(replay.body["replayed"])

        run = self.request("POST", "/attributions", "analyst", {
            "attribution_id": "attr-1", "policy_id": "p", "claim_ids": ["c1", "c2"]})
        self.assertEqual(run.status, 201)
        self.assertEqual(run.body["conservation_total"], "100.00")
        self.assertEqual(self.request("POST", "/attributions/attr-1/publish", "boss",
                                      {"version_no": 1}).status, 200)
        coverage = self.request("GET", "/coverage/p?policy_version=1", "auditor")
        self.assertEqual(coverage.status, 200)
        self.assertEqual(coverage.body["effective_beneficiary_count"], 1)
        chain = self.request("GET", "/attributions/attr-1/versions/1", "auditor")
        self.assertEqual(chain.status, 200)
        self.assertTrue(chain.body["chain"]["published"])
        disputes = self.request("GET", "/disputes", "auditor")
        self.assertEqual(disputes.body["disputes"], [])

    def test_forbidden_role(self) -> None:
        response = self.request("POST", "/policies", "editor", POLICY)
        self.assertEqual(response.status, 403)

    def test_unknown_route(self) -> None:
        response = self.request("GET", "/nope", "auditor")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
