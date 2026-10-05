from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from benefit_attribution.clock import FrozenClock
from benefit_attribution.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from benefit_attribution.jsonio import load_json
from benefit_attribution.service import AttributionService


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
SHA = "f" * 64


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc))
        self.service = AttributionService(self.connection, self.clock)
        for uid, role in (
            ("steward", "steward"), ("analyst", "analyst"),
            ("reviewer", "reviewer"), ("auditor", "auditor"),
        ):
            self.service.create_user(uid, uid, role)
        self.policy_v1 = load_json(ROOT / "fixtures" / "demo_attribution_policy_v1.json")
        self.policy_v2 = load_json(ROOT / "fixtures" / "demo_attribution_policy_v2.json")
        self.service.publish_policy("analyst", self.policy_v1)
        self._seed_registry()

    def tearDown(self) -> None:
        self.connection.close()

    def _seed_registry(self) -> None:
        s = self.service
        s.register_organization("steward", "park", "园区", "park", "R1")
        s.register_applicant(
            "steward", "a", "星河智造", "enterprise", "R1",
            identities=[{"identity_type": "business_license", "value": "L1"}],
        )
        s.register_applicant(
            "steward", "b", "星河经营部", "enterprise", "R1",
            identities=[{"identity_type": "business_license", "value": "L1"}],
        )
        s.register_applicant("steward", "c", "义乌星河", "enterprise", "R2")
        s.register_beneficiary("steward", "ben-a", "星河智造", "enterprise", "R1")
        s.register_beneficiary("steward", "ben-c", "义乌商户组", "micro_business", "R2")
        s.register_relation("steward", "rel-ac", "a", "c", "subsidiary", "2026-01-01")
        s.register_resource("steward", "res", "prog", "grant", "R1")
        s.register_claim("steward", "cl-a", "a", "ben-a", "prog", "R1",
                         resource_id="res", chain=["park"])
        s.register_claim("steward", "cl-b", "b", "ben-a", "prog", "R1")
        s.register_claim("steward", "cl-c", "c", "ben-c", "prog", "R2")
        s.register_milestone("steward", "m1", "business_improvement", 3, "2026-02-20", SHA,
                             claim_id="cl-a")
        s.register_milestone("steward", "m2", "business_improvement", 3, "2026-02-21", SHA,
                             claim_id="cl-c")

    def _payload(self, oid, claim, *, at="2026-03-01", value="1000", late=False, level=3,
                 milestones=("m1",), key="biz"):
        return {
            "outcome_id": oid, "outcome_type": "business_improvement", "outcome_key": key,
            "claim_id": claim, "value": value, "occurred_at": at, "evidence_level": level,
            "milestone_ids": list(milestones), "late_evidence": late,
        }

    def _first_attribution(self, key="biz"):
        s = self.service
        s.register_outcome("steward", self._payload("o1", "cl-a", key=key))
        return s.run_attribution("analyst", key, "ai-support-attribution", version=1, reason="初版")

    # ---------------------------------------------------------- 权限与冻结

    def test_roles_are_separated(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.publish_policy("steward", self.policy_v2)
        with self.assertRaises(Forbidden):
            self.service.run_attribution("steward", "biz", "ai-support-attribution")
        with self.assertRaises(Forbidden):
            self.service.coverage_report("steward", "ai-support-attribution")

    def test_frozen_registry_cannot_be_re_registered_but_shared_id_is_frozen_and_detected(self) -> None:
        # 同一主体编号不能重复冻结。
        with self.assertRaises(Conflict):
            self.service.register_applicant(
                "steward", "a", "改名", "enterprise", "R1",
                identities=[{"identity_type": "business_license", "value": "L9"}],
            )
        # 借牌复用同一硬标识：两方申报都冻结留痕，由 R1 规则判为同一实际主体。
        self.service.register_applicant(
            "steward", "zzz", "借牌", "enterprise", "R1",
            identities=[{"identity_type": "business_license", "value": "L1"}],
        )
        detected = self.service.detect_duplicates("auditor", "ai-support-attribution", 1)
        big = next(c for c in detected["clusters"] if "zzz" in c["applicant_ids"])
        self.assertIn("a", big["applicant_ids"])
        self.assertIn(["a", "b", "zzz"], big["identity_components"])

    def test_milestone_must_belong_to_claim(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.register_outcome(
                "steward", self._payload("ox", "cl-a", milestones=("m2",))
            )

    # ---------------------------------------------------------- 回调幂等

    def test_duplicate_callback_is_not_counted_again(self) -> None:
        payload = self._payload("o1", "cl-a")
        first = self.service.ingest_callback("steward", "cb1", "portal", payload)
        replay = self.service.ingest_callback("steward", "cb2", "portal", payload)
        self.assertEqual(first["status"], "processed")
        self.assertEqual(replay["status"], "duplicate")
        self.assertFalse(replay["counted"])
        rows = self.connection.execute("SELECT count(*) FROM outcomes").fetchone()[0]
        self.assertEqual(rows, 1)
        # 同一投递编号原样重放：幂等返回，仍不二次计入。
        again = self.service.ingest_callback("steward", "cb1", "portal", payload)
        self.assertFalse(again["counted"])
        rows = self.connection.execute("SELECT count(*) FROM outcomes").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_changed_payload_with_same_callback_id_conflicts(self) -> None:
        self.service.ingest_callback("steward", "cb1", "portal", self._payload("o1", "cl-a"))
        with self.assertRaises(Conflict):
            self.service.ingest_callback(
                "steward", "cb1", "portal", self._payload("o9", "cl-b", milestones=())
            )

    # ---------------------------------------------------------- 归属与守恒

    def test_hard_identity_duplicate_single_attribution(self) -> None:
        s = self.service
        s.register_outcome("steward", self._payload("o1", "cl-a"))
        s.register_outcome("steward", self._payload("o2", "cl-b", at="2026-03-02", milestones=()))
        result = s.run_attribution("analyst", "biz", "ai-support-attribution", version=1)
        self.assertEqual(result["result"]["status"], "single")
        self.assertEqual(len(result["result"]["allocations"]), 1)
        self.assertEqual(result["result"]["duplicates"][0]["reason"], "same_hard_identity")

    def test_shared_outcome_conserves_with_declared_shares(self) -> None:
        s = self.service
        s.register_outcome("steward", self._payload("o1", "cl-a", value="999"))
        s.register_outcome("steward", self._payload("o2", "cl-c", at="2026-03-02", value="999",
                                                    milestones=("m2",)))
        result = s.run_attribution(
            "analyst", "biz", "ai-support-attribution", version=1,
            declared_shares={"a": "0.7", "c": "0.3"},
        )
        values = {item["applicant_id"]: item["value_share"] for item in result["result"]["allocations"]}
        self.assertEqual(set(values), {"a", "c"})
        total = sum(int(v.replace(".", "")) for v in values.values())
        self.assertEqual(total, 999_000000)
        with self.assertRaises(ValidationFailed):
            s.run_attribution(
                "analyst", "biz", "ai-support-attribution", version=1,
                declared_shares={"a": "0.7", "c": "0.2"},
            )

    def test_rerun_without_fact_change_is_rejected_even_when_clock_moves(self) -> None:
        first = self._first_attribution()
        self.clock.advance(days=10)
        with self.assertRaises(InvalidState):
            self.service.run_attribution("analyst", "biz", "ai-support-attribution", version=1,
                                         reason="墙上时间流逝不应产生新版本")
        self.assertEqual(first["seq_no"], 1)

    # ---------------------------------------------------------- 发布锁定与新版本

    def test_published_version_is_never_rewritten(self) -> None:
        s = self.service
        first = self._first_attribution()
        s.publish_attribution("analyst", first["attribution_id"], "dashboard", SHA)
        with self.assertRaises(Conflict):
            s.publish_attribution("analyst", first["attribution_id"], "dashboard", SHA)
        # 迟到证据形成新版本；已发布旧版原样保留。
        self.clock.advance(days=20)
        s.register_outcome(
            "steward",
            self._payload("o2", "cl-c", at="2026-03-05", milestones=("m2",), late=True),
        )
        second = s.run_attribution("analyst", "biz", "ai-support-attribution", version=1,
                                   reason="迟到证据")
        self.assertEqual(second["seq_no"], 2)
        old = s.get_attribution("auditor", first["attribution_id"])
        self.assertEqual(old["status"], "published")
        self.assertEqual(len(old["allocations"]), 1)
        current = s.get_attribution("auditor", second["attribution_id"])
        self.assertEqual(current["status"], "active")
        # 旧版已发布，任何时点再发布都被拒绝，且内容不被回写。
        with self.assertRaises(Conflict):
            s.publish_attribution("analyst", first["attribution_id"], "dashboard2", SHA)
        old_after = s.get_attribution("auditor", first["attribution_id"])
        self.assertEqual(len(old_after["allocations"]), 1)

    def test_superseded_version_cannot_be_published(self) -> None:
        s = self.service
        first = self._first_attribution(key="k")
        self.clock.advance(days=20)
        s.register_outcome("steward", self._payload("o2", "cl-c", at="2026-03-05",
                                                    milestones=("m2",), late=True, key="k"))
        second = s.run_attribution("analyst", "k", "ai-support-attribution", version=1)
        self.assertEqual(second["seq_no"], 2)
        with self.assertRaises(InvalidState):
            s.publish_attribution("analyst", first["attribution_id"], "dashboard", SHA)

    # ---------------------------------------------------------- 异议与复核

    def test_dispute_preserves_original_and_independent_review(self) -> None:
        s = self.service
        first = self._first_attribution(key="d")
        dispute = s.raise_dispute("steward", first["attribution_id"], "归属有误")
        self.assertTrue(dispute["original_conclusion_preserved"])
        # 原归属制作人不能复核。
        with self.assertRaises(Forbidden):
            s.start_review("analyst", dispute["dispute_id"])
        # 异议提出人不能复核。
        with self.assertRaises(Forbidden):
            s.start_review("steward", dispute["dispute_id"])
        started = s.start_review("reviewer", dispute["dispute_id"])
        with self.assertRaises(InvalidState):
            s.start_review("reviewer", dispute["dispute_id"])
        # 复核期间原结论仍可读取且不变。
        self.assertEqual(s.get_attribution("auditor", first["attribution_id"])["status"], "active")
        done = s.complete_review(
            "reviewer", dispute["dispute_id"], "dismiss", "证据不足，维持原结论"
        )
        self.assertIsNone(done["new_attribution_id"])
        self.assertEqual(done["status"], "rejected")
        with self.assertRaises(InvalidState):
            s.complete_review("reviewer", dispute["dispute_id"], "dismiss", "重复结论")

    def test_upheld_review_creates_new_version_under_new_policy(self) -> None:
        s = self.service
        s.publish_policy("analyst", self.policy_v2)
        first = self._first_attribution(key="u")
        s.publish_attribution("analyst", first["attribution_id"], "dashboard", SHA)
        dispute = s.raise_dispute("steward", first["attribution_id"], "应适用新政策")
        s.start_review("reviewer", dispute["dispute_id"])
        done = s.complete_review(
            "reviewer", dispute["dispute_id"], "uphold", "适用第二期政策",
            policy_id="ai-support-attribution", version=2,
        )
        self.assertEqual(done["status"], "upheld")
        new = s.get_attribution("auditor", done["new_attribution_id"])
        self.assertEqual(new["policy"]["version"], 2)
        old = s.get_attribution("auditor", first["attribution_id"])
        self.assertEqual(old["status"], "published")  # 已发布旧版永不回写
        self.assertEqual(s.list_disputes("auditor", "open")["disputes"], [])

    def test_only_reviewer_assigned_may_complete(self) -> None:
        s = self.service
        self.service.create_user("reviewer2", "第二位复核人", "reviewer")
        first = self._first_attribution(key="r")
        dispute = s.raise_dispute("steward", first["attribution_id"], "x")
        s.start_review("reviewer", dispute["dispute_id"])
        with self.assertRaises(Forbidden):
            s.complete_review("reviewer2", dispute["dispute_id"], "dismiss", "越权")

    # ---------------------------------------------------------- 关系变化与视图

    def test_relation_change_only_appends_new_row(self) -> None:
        s = self.service
        detected = s.detect_duplicates("auditor", "ai-support-attribution", 1)
        self.assertTrue(any(c["shared_group"] for c in detected["clusters"]))
        # 关系在 6 月终止：新行取代旧行，历史行仍在。
        s.register_relation(
            "steward", "rel-ac-end", "a", "c", "subsidiary", "2026-01-01",
            valid_to="2026-06-01", supersedes_relation_id="rel-ac",
        )
        active = s.detect_duplicates("auditor", "ai-support-attribution", 1, as_of="2026-06-02")
        self.assertFalse(any(c["shared_group"] for c in active["clusters"]))
        rows = self.connection.execute("SELECT count(*) FROM relations").fetchone()[0]
        self.assertEqual(rows, 2)

    def test_claim_views_show_frozen_chain(self) -> None:
        views = self.service.claim_views("auditor", "prog")["views"]
        chained = next(v for v in views if v["claim_id"] == "cl-a")
        self.assertTrue(chained["chain"])
        self.assertEqual(chained["chain"][0]["organization_id"], "park")
        direct = next(v for v in views if v["claim_id"] == "cl-b")
        self.assertTrue(direct["direct"])

    def test_lineage_links_everything(self) -> None:
        s = self.service
        first = self._first_attribution(key="l")
        lineage = s.outcome_lineage("auditor", "l")
        self.assertEqual(lineage["attribution_versions"][0]["attribution_id"], first["attribution_id"])
        self.assertEqual(lineage["outcomes"][0]["milestones"][0]["milestone_id"], "m1")
        with self.assertRaises(NotFound):
            s.outcome_lineage("auditor", "missing")

    def test_coverage_reports_by_policy_version(self) -> None:
        s = self.service
        s.publish_policy("analyst", self.policy_v2)
        first = self._first_attribution(key="cov")
        s.publish_attribution("analyst", first["attribution_id"], "dashboard", SHA)
        self.clock.advance(days=20)
        s.register_outcome("steward", self._payload("o2", "cl-c", at="2026-03-05",
                                                    milestones=("m2",), late=True, key="cov"))
        second = s.run_attribution("analyst", "cov", "ai-support-attribution", version=1,
                                   reason="迟到证据")
        third = s.run_attribution("analyst", "cov", "ai-support-attribution", version=2,
                                  reason="新政策视角")
        v1 = s.coverage_report("auditor", "ai-support-attribution", 1)
        v2 = s.coverage_report("auditor", "ai-support-attribution", 2)
        # v1 视角：第二版已被 v2 第三版取代，生效的是已发布锁定的第一版（1 地 1 人）；
        # v2 视角：第三版活动，覆盖两地。
        self.assertEqual(v1["covered_region_count"], 1)
        self.assertEqual(v1["effective_beneficiary_count"], 1)
        self.assertEqual(v2["covered_region_count"], 2)
        # 两个版本都不应把同一成效键重复计数。
        self.assertEqual(v1["counted_outcome_key_count"], 1)
        self.assertEqual(v2["counted_outcome_key_count"], 1)
        self.assertEqual(third["seq_no"], 3)


if __name__ == "__main__":
    unittest.main()
