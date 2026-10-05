from __future__ import annotations

import unittest
from decimal import Decimal

from benefit_attribution.engine import (
    AttributionRuleError,
    attribute_cluster,
    detect_clusters,
    equal_shares,
    normalize_name,
)
from benefit_attribution.jsonio import load_json
from benefit_attribution.policy import AttributionPolicy, PolicyValidationError


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def policy(version: int = 1) -> AttributionPolicy:
    name = "demo_attribution_policy_v1.json" if version == 1 else "demo_attribution_policy_v2.json"
    return AttributionPolicy.from_dict(load_json(ROOT / "fixtures" / name))


def applicant(
    applicant_id: str,
    name: str,
    *,
    identities=None,
    region: str = "330100",
    weak=None,
) -> dict:
    return {
        "applicant_id": applicant_id,
        "name": name,
        "kind": "enterprise",
        "region_code": region,
        "identities": identities or [],
        "weak": weak or {},
    }


def occ(outcome_id, applicant, beneficiary, *, value="100", at="2026-03-01", level=3, late=False):
    return {
        "outcome_id": outcome_id,
        "outcome_key": "k1",
        "applicant_id": applicant,
        "beneficiary_id": beneficiary,
        "value": value,
        "occurred_at": at,
        "evidence_level": level,
        "late_evidence": late,
        "milestone_ids": [],
    }


class PolicyTests(unittest.TestCase):
    def test_demo_policy_loads(self) -> None:
        loaded = policy()
        self.assertEqual(loaded.policy_id, "ai-support-attribution")
        self.assertEqual({r.outcome_type for r in loaded.outcome_rules},
                         {"training_completed", "platform_online", "business_improvement"})

    def test_rejects_bad_rule(self) -> None:
        raw = {
            "policy_id": "p", "version": 1, "title": "t", "effective_from": "2026-01-01",
            "rules": {"identity": [], "affiliation": [], "fuzzy": [], "outcome": []},
        }
        with self.assertRaises(PolicyValidationError):
            AttributionPolicy.from_dict(raw)

    def test_rejects_bad_regex_and_unknown_kind(self) -> None:
        raw = {
            "policy_id": "p", "version": 1, "title": "t", "effective_from": "2026-01-01",
            "rules": {
                "identity": [],
                "affiliation": [
                    {"rule_id": "X", "relation_kinds": ["not_a_kind"], "note": "n"}
                ],
                "fuzzy": [
                    {"rule_id": "F", "name_pattern": "([", "pattern_flags": "",
                     "weak_fields": [], "require_weak_match": False, "note": "n"}
                ],
                "outcome": [
                    {"outcome_type": "t", "dedup_window_days": 0, "min_evidence_level": 3,
                     "max_shareholders": 1, "late_evidence_creates_revision": True,
                     "publication_locks_version": True}
                ],
            },
        }
        with self.assertRaises(PolicyValidationError):
            AttributionPolicy.from_dict(raw)


class ClusterTests(unittest.TestCase):
    def test_hard_identity_merges_and_fuzzy_emits_suspect(self) -> None:
        applicants = [
            applicant("a", "星河智造", identities=[{"identity_type": "business_license", "value": "L1"}]),
            applicant("b", "星河智造经营部", identities=[{"identity_type": "business_license", "value": "L1"}],
                      weak={"address": "x"}),
            applicant("e", "星河工作室", weak={"address": "x"}),
        ]
        clusters = detect_clusters(policy(), applicants, [], "2026-03-01")
        merged = next(c for c in clusters if set(c["applicant_ids"]) == {"a", "b"})
        self.assertTrue(merged["duplicate_confirmed"])
        self.assertEqual(merged["identity_components"], [["a", "b"]])
        # a 没有地址弱标识，疑似对在 b 与 e 之间成立。
        suspect = next(c for c in clusters if c["applicant_ids"] == ["b", "e"])
        self.assertTrue(suspect["suspects"])
        self.assertFalse(suspect["duplicate_confirmed"])

    def test_relation_respects_validity_window(self) -> None:
        applicants = [applicant("a", "甲"), applicant("c", "甲子")]
        relations = [{
            "relation_id": "r1", "left_applicant_id": "a", "right_applicant_id": "c",
            "organization_id": None, "kind": "subsidiary",
            "valid_from": "2026-02-01", "valid_to": None,
        }]
        self.assertEqual(detect_clusters(policy(), applicants, relations, "2026-01-31"), [])
        clusters = detect_clusters(policy(), applicants, relations, "2026-02-02")
        self.assertEqual(len(clusters), 1)
        self.assertTrue(clusters[0]["shared_group"])
        self.assertFalse(clusters[0]["duplicate_confirmed"])

    def test_dangling_relation_rejected(self) -> None:
        with self.assertRaises(AttributionRuleError):
            detect_clusters(
                policy(), [applicant("a", "甲")],
                [{"relation_id": "r", "left_applicant_id": "a", "right_applicant_id": "zzz",
                  "kind": "subsidiary", "valid_from": "2026-01-01", "valid_to": None}],
                "2026-02-01",
            )

    def test_name_normalization(self) -> None:
        self.assertEqual(normalize_name(" 星河 ＡＩ "), normalize_name("星河ai"))


class AttributionEngineTests(unittest.TestCase):
    def test_equal_shares_conserve_for_many_sizes(self) -> None:
        for n in range(1, 12):
            shares = equal_shares([f"m{i}" for i in range(n)])
            self.assertEqual(sum(shares, Decimal(0)), Decimal(1))
            self.assertTrue(all(s > 0 for s in shares))

    def test_three_seven_split_remainder(self) -> None:
        shares = equal_shares(["m0", "m1", "m2"])
        self.assertEqual(shares, [Decimal("0.333334"), Decimal("0.333333"), Decimal("0.333333")])

    def test_duplicate_same_identity_is_not_shared(self) -> None:
        clusters = [{
            "applicant_ids": ["a", "b"],
            "duplicate_confirmed": True,
            "identity_components": [["a", "b"]],
        }]
        result = attribute_cluster(
            policy(), "business_improvement",
            [occ("o1", "a", "ben-a"), occ("o2", "b", "ben-a")],
            clusters=clusters,
        )
        self.assertEqual(result["status"], "single")
        self.assertEqual(len(result["allocations"]), 1)
        self.assertEqual(result["duplicates"][0]["reason"], "same_hard_identity")
        self.assertEqual(result["total_share"], "1.000000")

    def test_distinct_subsidiary_shares_equally(self) -> None:
        clusters = [{
            "applicant_ids": ["a", "c"],
            "duplicate_confirmed": False,
            "identity_components": [],
        }]
        result = attribute_cluster(
            policy(), "business_improvement",
            [occ("o1", "a", "ben-a", value="100"), occ("o2", "c", "ben-c", value="100")],
            clusters=clusters,
        )
        self.assertEqual(result["status"], "shared")
        self.assertEqual({item["share"] for item in result["allocations"]}, {"0.500000"})
        self.assertEqual(Decimal(result["total_value"]), Decimal("100.000000"))
        self.assertEqual(
            sum(Decimal(item["value_share"]) for item in result["allocations"]),
            Decimal("100.000000"),
        )

    def test_declared_shares_must_conserve(self) -> None:
        with self.assertRaises(AttributionRuleError):
            attribute_cluster(
                policy(), "business_improvement",
                [occ("o1", "a", "b1"), occ("o2", "c", "b2")],
                declared_shares={"a": "0.6", "c": "0.3"},
            )

    def test_evidence_gate_excludes_weak_evidence(self) -> None:
        result = attribute_cluster(
            policy(), "platform_online",
            [occ("o1", "a", "b1", level=3), occ("o2", "c", "b2", level=2)],
        )
        self.assertEqual(result["status"], "single")
        self.assertIn("evidence-gate", str(result["explanation"]))

    def test_window_overflow_rejected(self) -> None:
        with self.assertRaises(AttributionRuleError):
            attribute_cluster(
                policy(), "training_completed",
                [occ("o1", "a", "b1", at="2026-03-01"), occ("o2", "c", "b2", at="2026-05-01")],
            )

    def test_too_many_shareholders(self) -> None:
        occurrences = [occ(f"o{i}", f"m{i}", f"b{i}") for i in range(4)]
        with self.assertRaises(AttributionRuleError):
            attribute_cluster(policy(), "training_completed", occurrences)

    def test_weak_anchor_is_rejected(self) -> None:
        with self.assertRaises(AttributionRuleError):
            attribute_cluster(
                policy(), "platform_online",
                [occ("o1", "a", "b1", level=1)],
            )

    def test_late_evidence_gate_can_be_disabled_by_policy(self) -> None:
        raw = load_json(ROOT / "fixtures" / "demo_attribution_policy_v1.json")
        for rule in raw["rules"]["outcome"]:
            rule["late_evidence_creates_revision"] = False
        strict = AttributionPolicy.from_dict(raw)
        result = attribute_cluster(
            strict, "business_improvement",
            [occ("o1", "a", "b1", level=3), occ("o2", "c", "b2", at="2026-03-02", late=True)],
        )
        self.assertEqual(result["status"], "single")
        self.assertIn("late-evidence-gate", str(result["explanation"]))


if __name__ == "__main__":
    unittest.main()
