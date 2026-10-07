from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from benefit_attribution.clock import FrozenClock
from benefit_attribution.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from benefit_attribution.service import AttributionService

POLICY_V1 = {
    "policy_id": "p", "version": 1, "title": "v1",
    "effective_from": "2026-01-01T00:00:00Z",
    "rules": ["LEGAL_IDENTITY", "GROUP_SUBSIDIARY", "PARK_TENANT", "ASSOCIATION_MEMBER",
              "SHARED_RESOURCE", "SAME_EVIDENCE"],
    "share_strategy": "equal",
}
POLICY_V2 = {
    "policy_id": "p", "version": 2, "title": "v2",
    "effective_from": "2026-07-01T00:00:00Z",
    "rules": POLICY_V1["rules"] + ["CONTACT_FINGERPRINT"],
    "share_strategy": "applicant_weight",
    "applicant_weight_field": "w",
}


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.service = AttributionService(self.connection, self.clock)
        for uid, role in (
            ("editor", "registry_editor"), ("padmin", "policy_admin"),
            ("analyst", "analyst"), ("boss", "manager"),
            ("reviewer", "reviewer"), ("auditor", "auditor"),
        ):
            self.service.create_user(uid, uid, role)
        self.service.publish_policy("padmin", POLICY_V1)
        self.service.publish_policy("padmin", POLICY_V2)
        self._seed_entities()

    def tearDown(self) -> None:
        self.connection.close()

    def _seed_entities(self) -> None:
        s = self.service
        s.register_beneficiary("editor", "b1", "受益企业甲", "enterprise", "BJ",
                               legal_identity_code="91-B1")
        s.register_beneficiary("editor", "b2", "受益企业乙", "enterprise", "GD")
        s.register_applicant("editor", "park", "产业园", "park", "BJ", channel="park_ch",
                             contact_fingerprint="fp-1")
        s.register_applicant("editor", "assoc", "协会", "association", "BJ", channel="assoc_ch",
                             contact_fingerprint="fp-1")
        s.register_applicant("editor", "sub", "子公司", "subsidiary", "GD", channel="sub_ch",
                             legal_identity_code="91-B1")
        s.register_applicant("editor", "direct", "直营", "enterprise", "BJ", channel="direct_ch",
                             legal_identity_code="91-B1")
        s.register_org_relation("editor", "rel-park", "park_tenant", "park", "b1",
                                "2026-01-01T00:00:00Z")
        s.register_org_relation("editor", "rel-assoc", "association_member", "assoc", "b1",
                                "2026-01-01T00:00:00Z")
        s.register_resource("editor", "res-shared", "advisory", "prog", "BJ")
        s.register_resource("editor", "res-training", "training", "prog", "BJ")
        s.register_evidence("editor", "ev-same", "training_completed", "2026-03-01T00:00:00Z",
                            "培训机构")
        s.register_evidence("editor", "ev-a", "business_outcome", "2026-08-01T00:00:00Z", "协会")
        s.register_evidence("editor", "ev-b", "business_outcome", "2026-08-02T00:00:00Z", "园区")

    def _outcome_pair(self) -> list[str]:
        self.service.register_claim(
            "editor", "c-a", "assoc", "b1", "res-shared", "business_outcome", 30000, "ev-a",
            "协会", attributes={"w": 3})
        self.service.register_claim(
            "editor", "c-b", "park", "b1", "res-shared", "business_outcome", 30000, "ev-b",
            "园区", attributes={"w": 1})
        return ["c-a", "c-b"]

    # ---- 冻结与校验 -----------------------------------------------------

    def test_frozen_entities_cannot_be_duplicated_and_inputs_validated(self) -> None:
        with self.assertRaises(Conflict):
            self.service.register_beneficiary("editor", "b1", "重复", "enterprise", "BJ")
        with self.assertRaises(ValidationFailed):
            self.service.register_applicant(
                "editor", "x", "x", "not_a_kind", "BJ", channel="ch")
        with self.assertRaises(ValidationFailed):
            self.service.register_claim(
                "editor", "c-x", "park", "b1", "res-shared", "business_outcome", -1, "ev-a", "协会")
        with self.assertRaises(NotFound):
            self.service.register_claim(
                "editor", "c-x", "park", "b1", "missing-res", "business_outcome", 1, "ev-a", "协会")

    def test_relation_requires_known_endpoints(self) -> None:
        with self.assertRaises(NotFound):
            self.service.register_org_relation(
                "editor", "rel-x", "park_tenant", "ghost", "b1", "2026-01-01T00:00:00Z")

    def test_relation_supersede_requires_same_edge(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.register_org_relation(
                "editor", "rel-park-v2", "park_tenant", "park", "b2",
                "2026-02-01T00:00:00Z", supersedes_relation_id="rel-park")

    # ---- 回调幂等 -------------------------------------------------------

    def test_duplicate_callback_not_counted_again(self) -> None:
        payload = {
            "claim_id": "c-a", "applicant_id": "assoc", "beneficiary_id": "b1",
            "resource_id": "res-shared", "milestone_kind": "business_outcome",
            "outcome_value": 30000, "evidence_id": "ev-a", "reported_by_org": "协会",
            "attributes": {"w": 3},
        }
        first = self.service.ingest_callback("editor", "scope", "cb-1", payload)
        second = self.service.ingest_callback("editor", "scope", "cb-1", payload)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM benefit_claims").fetchone()[0], 1)
        changed = dict(payload, outcome_value=999)
        with self.assertRaises(Conflict):
            self.service.ingest_callback("editor", "scope", "cb-1", changed)

    # ---- 归属计算与守恒 -------------------------------------------------

    def test_duplicate_cluster_counts_once_and_conserves(self) -> None:
        self.service.register_claim(
            "editor", "c-t1", "park", "b1", "res-training", "training_completed", 1,
            "ev-same", "园区")
        self.service.register_claim(
            "editor", "c-t2", "assoc", "b2", "res-training", "training_completed", 1,
            "ev-same", "协会")
        result = self.service.run_attribution(
            "analyst", "attr-dup", "p", ["c-t1", "c-t2"], policy_version=1)
        cluster = result["clusters"][0]
        self.assertEqual(cluster["relation"], "duplicate")
        self.assertEqual(cluster["total_value"], "1.00")
        shares = self.connection.execute(
            "SELECT claim_id,share_value FROM attribution_shares "
            "WHERE attribution_id='attr-dup'"
        ).fetchall()
        self.assertEqual(sum(Decimal(r["share_value"]) for r in shares), Decimal("1.00"))

    def test_shared_cluster_equal_split_conserves_total(self) -> None:
        ids = self._outcome_pair()
        result = self.service.run_attribution("analyst", "attr-x", "p", ids, policy_version=1)
        shared = [c for c in result["clusters"] if c["relation"] == "shared"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["total_value"], "30000.00")
        rows = self.connection.execute(
            "SELECT share_value FROM attribution_shares WHERE attribution_id='attr-x'"
        ).fetchall()
        self.assertEqual(sum(Decimal(r["share_value"]) for r in rows), Decimal("30000.00"))

    def test_weighted_policy_rounding_remainder_conserves(self) -> None:
        self.service.register_claim(
            "editor", "c1", "park", "b1", "res-shared", "business_outcome", 100, "ev-a", "x",
            attributes={"w": 1})
        self.service.register_claim(
            "editor", "c2", "assoc", "b1", "res-shared", "business_outcome", 100, "ev-b", "x",
            attributes={"w": 1})
        self.service.register_claim(
            "editor", "c3", "sub", "b1", "res-shared", "business_outcome", 100, "ev-b", "x",
            attributes={"w": 1})
        result = self.service.run_attribution(
            "analyst", "attr-third", "p", ["c1", "c2", "c3"], policy_version=2)
        rows = self.connection.execute(
            "SELECT share_value FROM attribution_shares WHERE attribution_id='attr-third'"
        ).fetchall()
        self.assertEqual(sum(Decimal(r["share_value"]) for r in rows), Decimal("100.00"))

    def test_same_scope_replays_without_new_version(self) -> None:
        ids = self._outcome_pair()
        first = self.service.run_attribution("analyst", "attr-r", "p", ids, policy_version=1)
        second = self.service.run_attribution("analyst", "attr-r", "p", ids, policy_version=1)
        self.assertEqual(first["version_no"], 1)
        self.assertTrue(second.get("replayed"))
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM attribution_versions WHERE attribution_id='attr-r'"
            ).fetchone()[0], 1)

    # ---- 版本化与发布冻结 ----------------------------------------------

    def test_late_evidence_creates_new_version_and_keeps_old(self) -> None:
        ids = self._outcome_pair()
        v1 = self.service.run_attribution("analyst", "attr-v", "p", ids, policy_version=1)
        self.service.register_evidence(
            "editor", "ev-late", "business_outcome", "2026-09-10T00:00:00Z", "迟到机构")
        self.service.register_claim(
            "editor", "c-late", "sub", "b1", "res-shared", "business_outcome", 30000, "ev-late",
            "迟到机构", attributes={"w": 2})
        v2 = self.service.run_attribution(
            "analyst", "attr-v", "p", ids + ["c-late"], policy_version=1,
            change_reason="证据迟到")
        self.assertEqual(v2["version_no"], 2)
        old = self.connection.execute(
            "SELECT status FROM attribution_versions WHERE attribution_id='attr-v' AND version_no=1"
        ).fetchone()
        self.assertEqual(old["status"], "superseded")
        chain = self.service.source_chain("auditor", "attr-v", 1)
        self.assertEqual(len(chain["clusters"]), 1)

    def test_published_version_is_never_rewritten(self) -> None:
        ids = self._outcome_pair()
        self.service.run_attribution("analyst", "attr-p", "p", ids, policy_version=1)
        self.service.publish_attribution("boss", "attr-p", 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE attribution_versions SET status='superseded' "
                "WHERE attribution_id='attr-p' AND version_no=1")
        self.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM attribution_shares WHERE attribution_id='attr-p' AND version_no=1")
        self.connection.rollback()
        # 新证据到达：只追加新版本，已发布的 v1 原样有效。
        self.service.register_evidence(
            "editor", "ev-late2", "business_outcome", "2026-09-10T00:00:00Z", "迟到机构")
        self.service.register_claim(
            "editor", "c-late2", "sub", "b1", "res-shared", "business_outcome", 30000, "ev-late2",
            "迟到机构", attributes={"w": 2})
        v2 = self.service.run_attribution(
            "analyst", "attr-p", "p", ids + ["c-late2"], policy_version=1)
        self.assertEqual(v2["version_no"], 2)
        v1_row = self.connection.execute(
            "SELECT status,published_at FROM attribution_versions "
            "WHERE attribution_id='attr-p' AND version_no=1"
        ).fetchone()
        self.assertEqual(v1_row["status"], "effective")
        self.assertIsNotNone(v1_row["published_at"])

    def test_disputed_version_blocks_plain_recompute(self) -> None:
        ids = self._outcome_pair()
        self.service.run_attribution("analyst", "attr-d", "p", ids, policy_version=1)
        self.service.raise_dispute("boss", "attr-d", 1, "有异议")
        with self.assertRaises(InvalidState):
            self.service.run_attribution("analyst", "attr-d", "p", ids, policy_version=1,
                                         change_reason="试图绕过复核")

    def test_dispute_rejected_restores_effective_and_keeps_conclusion(self) -> None:
        ids = self._outcome_pair()
        run = self.service.run_attribution("analyst", "attr-dr", "p", ids, policy_version=1)
        before = self.service.source_chain("auditor", "attr-dr", run["version_no"])
        dispute = self.service.raise_dispute("boss", "attr-dr", 1, "不认可")
        self.service.start_review("reviewer", dispute["dispute_id"])
        result = self.service.resolve_dispute(
            "reviewer", dispute["dispute_id"], False, "原结论正确")
        self.assertEqual(result["status"], "rejected")
        row = self.connection.execute(
            "SELECT status FROM attribution_versions WHERE attribution_id='attr-dr'"
        ).fetchone()
        self.assertEqual(row["status"], "effective")
        after = self.service.source_chain("auditor", "attr-dr", 1)
        self.assertEqual(
            [c["total_value"] for c in before["clusters"]],
            [c["total_value"] for c in after["clusters"]])

    def test_dispute_upheld_preserves_original_and_adds_revised_version(self) -> None:
        ids = self._outcome_pair()
        self.service.run_attribution("analyst", "attr-du", "p", ids, policy_version=1)
        self.service.publish_attribution("boss", "attr-du", 1)
        dispute = self.service.raise_dispute("boss", "attr-du", 1, "应按贡献权重")
        with self.assertRaises(Forbidden):
            self.service.start_review("boss", dispute["dispute_id"])
        self.service.start_review("reviewer", dispute["dispute_id"])
        resolution = self.service.resolve_dispute(
            "reviewer", dispute["dispute_id"], True, "改按 v2 权重",
            policy_id="p", policy_version=2, claim_ids=ids)
        self.assertEqual(resolution["status"], "upheld")
        self.assertEqual(resolution["new_version_no"], 2)
        self.assertEqual(resolution["original_preserved"]["version_no"], 1)
        v1 = self.connection.execute(
            "SELECT status,published_at FROM attribution_versions "
            "WHERE attribution_id='attr-du' AND version_no=1"
        ).fetchone()
        self.assertIsNotNone(v1["published_at"])
        self.assertEqual(v1["status"], "effective")
        chain = self.service.source_chain("auditor", "attr-du", 2)
        self.assertEqual(chain["status"], "revised")
        shares = {line["claim_id"]: line["share_value"]
                  for line in chain["clusters"][0]["claims"]}
        self.assertEqual(shares, {"c-a": "22500.00", "c-b": "7500.00"})
        self.assertEqual(self.service.open_disputes("auditor"), [])

    def test_uphold_requires_a_real_change(self) -> None:
        ids = self._outcome_pair()
        self.service.run_attribution("analyst", "attr-same", "p", ids, policy_version=1)
        dispute = self.service.raise_dispute("boss", "attr-same", 1, "无变化的复核")
        self.service.start_review("reviewer", dispute["dispute_id"])
        with self.assertRaises(InvalidState):
            self.service.resolve_dispute(
                "reviewer", dispute["dispute_id"], True, "但依据一模一样",
                policy_id="p", policy_version=1, claim_ids=ids)

    # ---- 关系随时间变化 -------------------------------------------------

    def test_relation_validity_window_changes_clustering_by_as_of(self) -> None:
        # 两个申请主体仅由一条在 7 月失效的协会关系相连，资源与证据均不同。
        self.service.register_applicant("editor", "x1", "成员企业甲", "enterprise", "BJ",
                                        channel="ch1")
        self.service.register_applicant("editor", "x2", "成员企业乙", "enterprise", "BJ",
                                        channel="ch2")
        self.service.register_org_relation(
            "editor", "rel-x", "association_member", "x1", "x2",
            "2026-01-01T00:00:00Z", valid_to="2026-07-01T00:00:00Z")
        self.service.register_resource("editor", "res-a", "advisory", "prog", "BJ")
        self.service.register_resource("editor", "res-b", "data_access", "prog", "BJ")
        self.service.register_claim(
            "editor", "c1", "x1", "b1", "res-a", "business_outcome", 100, "ev-a", "机构甲")
        self.service.register_claim(
            "editor", "c2", "x2", "b1", "res-b", "business_outcome", 100, "ev-b", "机构乙")
        run_march = self.service.run_attribution(
            "analyst", "attr-time", "p", ["c1", "c2"], policy_version=1,
            as_of="2026-03-01T00:00:00Z")
        self.assertEqual(len(run_march["clusters"]), 1)
        run_october = self.service.run_attribution(
            "analyst", "attr-time", "p", ["c1", "c2"], policy_version=1,
            as_of="2026-10-01T00:00:00Z", change_reason="协会关系已失效")
        self.assertEqual(run_october["version_no"], 2)
        self.assertEqual(len(run_october["clusters"]), 2)

    # ---- 管理视图 -------------------------------------------------------

    def test_coverage_and_effective_beneficiaries_by_policy_version(self) -> None:
        ids = self._outcome_pair()
        self.service.run_attribution("analyst", "attr-cov", "p", ids, policy_version=1)
        self.service.run_attribution(
            "analyst", "attr-cov", "p", ids, policy_version=2, change_reason="改用权重政策")
        cov1 = self.service.coverage("boss", "p", 1)
        cov2 = self.service.coverage("boss", "p", 2)
        self.assertEqual(cov1["policy_version"], 1)
        self.assertEqual(cov2["regions"][0]["region_code"], "BJ")
        self.assertEqual(cov2["regions"][0]["effective_beneficiaries"], ["b1"])
        self.assertEqual(cov2["covered_region_count"], 1)
        # 重复簇中零份额的受益企业不计为有效受益者。
        self.service.register_claim(
            "editor", "dup1", "park", "b1", "res-training", "training_completed", 1, "ev-same", "园区")
        self.service.register_claim(
            "editor", "dup2", "assoc", "b2", "res-training", "training_completed", 1, "ev-same", "协会")
        self.service.run_attribution(
            "analyst", "attr-dup2", "p", ["dup1", "dup2"], policy_version=2)
        cov = self.service.coverage("boss", "p", 2)
        effective = {bid for region in cov["regions"] for bid in region["effective_beneficiaries"]}
        self.assertIn("b1", effective)
        self.assertNotIn("b2", effective)

    def test_dashboard_and_source_chain(self) -> None:
        ids = self._outcome_pair()
        self.service.run_attribution("analyst", "attr-dash", "p", ids, policy_version=1)
        self.service.raise_dispute("boss", "attr-dash", 1, "待处理争议")
        board = self.service.dashboard("boss", "p", 1)
        self.assertEqual(board["open_dispute_count"], 1)
        self.assertEqual(board["open_disputes"][0]["reason"], "待处理争议")
        chain = self.service.source_chain("auditor", "attr-dash")
        self.assertEqual(chain["policy"]["policy_version"], 1)
        self.assertTrue(chain["basis"]["claim_sha256"])
        self.assertTrue(chain["clusters"][0]["matched_rules"])
        line = chain["clusters"][0]["claims"][0]
        self.assertEqual(line["resource"]["resource_id"], "res-shared")
        self.assertEqual(len(line["evidence"]["content_sha256"]), 64)
        self.assertEqual(len(chain["disputes"]), 1)
        versions = self.service.list_versions("auditor", "attr-dash")
        self.assertEqual([v["version_no"] for v in versions], [1])

    # ---- 权限 -----------------------------------------------------------

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.publish_policy("editor", POLICY_V1)
        with self.assertRaises(Forbidden):
            self.service.run_attribution("editor", "a", "p", [], policy_version=1)
        with self.assertRaises(Forbidden):
            self.service.publish_attribution("analyst", "a", 1)
        with self.assertRaises(Forbidden):
            self.service.coverage("reviewer", "p", 1)
        with self.assertRaises(NotFound):
            self.service.source_chain("auditor", "missing")


if __name__ == "__main__":
    unittest.main()
