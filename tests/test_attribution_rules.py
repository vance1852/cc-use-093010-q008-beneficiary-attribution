from __future__ import annotations

import unittest
from decimal import Decimal

from benefit_attribution.contracts import Policy
from benefit_attribution.rules import ClaimView, RelationEdge, cluster_claims, split_conserved


def policy(rules: list[str], strategy: str = "equal", weight_field: str | None = None) -> Policy:
    raw = {
        "policy_id": "p",
        "version": 1,
        "title": "t",
        "effective_from": "2026-01-01T00:00:00Z",
        "rules": rules,
        "share_strategy": strategy,
    }
    if weight_field:
        raw["applicant_weight_field"] = weight_field
    return Policy.from_dict(raw)


def claim(
    claim_id: str, *, applicant: str = "a", beneficiary: str = "b", resource: str = "r",
    evidence: str = "e", milestone: str = "business_outcome", value: str = "100.00",
    legal_code: str | None = None, region: str = "R1", contact: str | None = None,
    weight: str = "1",
) -> ClaimView:
    return ClaimView(
        claim_id=claim_id, applicant_id=applicant, applicant_legal_code=legal_code,
        applicant_kind="enterprise", applicant_region=region, contact_fingerprint=contact,
        beneficiary_id=beneficiary, beneficiary_legal_code=None, resource_id=resource,
        evidence_id=evidence, milestone_kind=milestone, outcome_value=Decimal(value),
        weight=Decimal(weight),
    )


class ClusterTests(unittest.TestCase):
    def test_same_evidence_is_duplicate(self) -> None:
        claims = [
            claim("c1", applicant="park", evidence="E"),
            claim("c2", applicant="assoc", evidence="E"),
        ]
        result = cluster_claims(claims, [], policy(["SAME_EVIDENCE", "ASSOCIATION_MEMBER"]))
        self.assertEqual(len(result.clusters), 1)
        self.assertEqual(result.clusters[0].relation, "duplicate")
        codes = {hit.rule_code for hit in result.clusters[0].rule_hits}
        self.assertIn("SAME_EVIDENCE", codes)

    def test_legal_identity_merges_cross_channel(self) -> None:
        claims = [
            claim("c1", applicant="sub", resource="r1", evidence="e1", legal_code="X"),
            claim("c2", applicant="direct", resource="r2", evidence="e2", legal_code="X"),
        ]
        result = cluster_claims(claims, [], policy(["LEGAL_IDENTITY"]))
        self.assertEqual(len(result.clusters), 1)
        self.assertEqual(result.clusters[0].relation, "duplicate")

    def test_shared_resource_without_hard_signal_is_shared(self) -> None:
        claims = [
            claim("c1", applicant="a1", evidence="e1"),
            claim("c2", applicant="a2", evidence="e2"),
        ]
        result = cluster_claims(claims, [], policy(["SHARED_RESOURCE"]))
        self.assertEqual(len(result.clusters), 1)
        self.assertEqual(result.clusters[0].relation, "shared")

    def test_relation_edges_merge_via_park_and_association(self) -> None:
        claims = [
            claim("c1", applicant="park", resource="r1", evidence="e1"),
            claim("c2", applicant="assoc", resource="r2", evidence="e2"),
        ]
        edges = [
            RelationEdge("park", "b", "park_tenant"),
            RelationEdge("assoc", "b", "association_member"),
        ]
        result = cluster_claims(
            claims, edges, policy(["PARK_TENANT", "ASSOCIATION_MEMBER"])
        )
        self.assertEqual(len(result.clusters), 1)
        self.assertEqual(result.clusters[0].relation, "shared")
        self.assertEqual(len(result.clusters[0].rule_hits), 2)

    def test_hard_signal_recorded_even_when_soft_edge_connected_first(self) -> None:
        # 关系边先连通，随后同证据规则也要留痕，并把簇定性升级为 duplicate。
        claims = [
            claim("c1", applicant="assoc", evidence="SAME"),
            claim("c2", applicant="park", evidence="SAME"),
        ]
        edges = [RelationEdge("assoc", "b", "association_member"),
                 RelationEdge("park", "b", "park_tenant")]
        result = cluster_claims(
            claims, edges,
            policy(["ASSOCIATION_MEMBER", "PARK_TENANT", "SAME_EVIDENCE"]),
        )
        self.assertEqual(result.clusters[0].relation, "duplicate")
        self.assertIn("SAME_EVIDENCE", result.clusters[0].rule_codes)

    def test_different_milestones_never_merge(self) -> None:
        claims = [
            claim("c1", resource="r", evidence="e", milestone="training_completed", value="1"),
            claim("c2", resource="r", evidence="e", milestone="business_outcome", value="999"),
        ]
        result = cluster_claims(
            claims, [], policy(["SAME_EVIDENCE", "SHARED_RESOURCE"])
        )
        self.assertEqual(len(result.clusters), 2)

    def test_contact_fingerprint_is_soft_signal(self) -> None:
        claims = [
            claim("c1", applicant="a1", resource="r1", evidence="e1", contact="fp"),
            claim("c2", applicant="a2", resource="r2", evidence="e2", contact="fp"),
        ]
        result = cluster_claims(claims, [], policy(["CONTACT_FINGERPRINT"]))
        cluster = result.clusters[0]
        self.assertEqual(cluster.relation, "shared")
        self.assertTrue(cluster.suspected_duplicate)

    def test_deterministic_under_permutation(self) -> None:
        claims = [
            claim("c1", applicant="a1", resource="r", evidence="e1"),
            claim("c2", applicant="a2", resource="r", evidence="e2"),
            claim("c3", applicant="a3", resource="other", evidence="e3"),
        ]
        r1 = cluster_claims(claims, [], policy(["SHARED_RESOURCE"]))
        r2 = cluster_claims(list(reversed(claims)), [], policy(["SHARED_RESOURCE"]))
        keys1 = sorted(c.key for c in r1.clusters)
        keys2 = sorted(c.key for c in r2.clusters)
        self.assertEqual(keys1, keys2)


class SplitTests(unittest.TestCase):
    def test_unique_keeps_full_value(self) -> None:
        total, allocation, _ = split_conserved([claim("c1", value="42.00")], "unique", "equal")
        self.assertEqual(total, Decimal("42.00"))
        self.assertEqual(allocation[0][1], Decimal("42.00"))

    def test_duplicate_counts_once_with_zero_shares_recorded(self) -> None:
        members = [claim("c1", value="100.00"), claim("c2", value="80.00")]
        total, allocation, _ = split_conserved(members, "duplicate", "equal")
        self.assertEqual(total, Decimal("100.00"))
        shares = {item[0].claim_id: item[1] for item in allocation}
        self.assertEqual(sum(shares.values(), Decimal(0)), Decimal("100.00"))
        self.assertEqual(shares["c2"], Decimal("0.00"))

    def test_equal_split_conserves_with_rounding_remainder(self) -> None:
        members = [claim(f"c{i}", value="100.00") for i in range(3)]
        total, allocation, _ = split_conserved(members, "shared", "equal")
        self.assertEqual(total, Decimal("100.00"))
        self.assertEqual(sum((item[1] for item in allocation), Decimal(0)), Decimal("100.00"))

    def test_weighted_split_3_to_1(self) -> None:
        members = [claim("c1", value="30000", weight="3"), claim("c2", value="30000", weight="1")]
        total, allocation, _ = split_conserved(members, "shared", "applicant_weight")
        shares = {item[0].claim_id: item[1] for item in allocation}
        self.assertEqual(shares, {"c1": Decimal("22500.00"), "c2": Decimal("7500.00")})
        self.assertEqual(sum(shares.values(), Decimal(0)), total)

    def test_zero_weights_fall_back_to_equal(self) -> None:
        members = [claim("c1", weight="0"), claim("c2", weight="0")]
        _, allocation, notes = split_conserved(members, "shared", "applicant_weight")
        self.assertEqual({item[1] for item in allocation}, {Decimal("50.00")})
        self.assertTrue(any("回退为等权" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
