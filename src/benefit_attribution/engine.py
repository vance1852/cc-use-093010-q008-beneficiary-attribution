"""可解释的受益关系识别与成效分摊纯规则引擎。

引擎不访问数据库、不产生副作用：给定政策版本与冻结快照，永远得到相同结论。
每条结论附带 rule_id / 命中证据，分摊结果以确定性的最大余数法保证总量守恒。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping, Sequence

from .policy import AttributionPolicy, OutcomeTypeRule, validate_share


ENGINE_VERSION = "1.0.0"

SHARE_QUANTUM = Decimal("0.000001")
SHARE_PLACES = Decimal("0.000001")


class AttributionRuleError(ValueError):
    """输入快照无法满足归属规则。"""


@dataclass(frozen=True, slots=True, order=True)
class ClusterShare:
    applicant_id: str
    beneficiary_id: str
    share: Decimal
    value_share: Decimal
    role: str  # primary | contributor

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicant_id": self.applicant_id,
            "beneficiary_id": self.beneficiary_id,
            "share": format(self.share, "f"),
            "value_share": format(self.value_share, "f"),
            "role": self.role,
        }


def normalize_name(name: str) -> str:
    """归一化机构名称：去标点、空白、大小写与全角差异。"""

    folded = unicodedata.normalize("NFKC", name).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _relation_active(relation: Mapping[str, Any], as_of: str) -> bool:
    if relation.get("valid_from") and relation["valid_from"] > as_of:
        return False
    valid_to = relation.get("valid_to")
    if valid_to and valid_to <= as_of:
        return False
    return True


class _UnionFind:
    def __init__(self, nodes: Sequence[str]) -> None:
        self.parent = {node: node for node in nodes}

    def find(self, node: str) -> str:
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != node:
            node, self.parent[node] = self.parent[node], root
        return root

    def union(self, left: str, right: str) -> None:
        self.parent[self.find(left)] = self.find(right)


def detect_clusters(
    policy: AttributionPolicy,
    applicants: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    as_of: str,
) -> list[dict[str, Any]]:
    """按 R1/R2/R3 规则识别重复与共享主体簇，返回带解释的簇列表。"""

    ids = tuple(sorted(item["applicant_id"] for item in applicants))
    if len(ids) != len(set(ids)):
        raise AttributionRuleError("申请主体编号重复")
    by_id = {item["applicant_id"]: item for item in applicants}
    uf = _UnionFind(ids)
    uf_identity = _UnionFind(ids)
    explanations: list[dict[str, Any]] = []

    # R1：硬标识相同（营业执照/税号/统一社会信用代码等）。
    index: dict[tuple[str, str], str] = {}
    for applicant in applicants:
        for identity in applicant.get("identities", ()):
            itype = identity["identity_type"]
            ivalue = identity["value"].strip().casefold()
            if not ivalue:
                continue
            key = (itype, ivalue)
            if key in index and index[key] != applicant["applicant_id"]:
                other = index[key]
                if uf.find(other) != uf.find(applicant["applicant_id"]):
                    uf.union(other, applicant["applicant_id"])
                    for rule in policy.identity_rules:
                        if rule.matches({itype}):
                            explanations.append(
                                rule.explain(itype)
                                | {
                                    "applicant_ids": sorted([other, applicant["applicant_id"]]),
                                    "identity_type": itype,
                                }
                            )
                uf_identity.union(other, applicant["applicant_id"])
            else:
                index[key] = applicant["applicant_id"]

    # R2：登记在案且在 as_of 生效的关联关系。
    for relation in relations:
        if relation["left_applicant_id"] not in by_id or relation["right_applicant_id"] not in by_id:
            raise AttributionRuleError(
                f"关联关系 {relation.get('relation_id')} 引用了未冻结的申请主体"
            )
        if not _relation_active(relation, as_of):
            continue
        for rule in policy.affiliation_rules:
            if relation["kind"] in rule.relation_kinds and uf.find(
                relation["left_applicant_id"]
            ) != uf.find(relation["right_applicant_id"]):
                uf.union(relation["left_applicant_id"], relation["right_applicant_id"])
                explanations.append(
                    rule.explains(relation["kind"])
                    | {
                        "relation_id": relation["relation_id"],
                        "applicant_ids": sorted(
                            [relation["left_applicant_id"], relation["right_applicant_id"]]
                        ),
                    }
                )

    # R3：名称模式命中且弱标识一致（疑似重复，需人工确认后才升级为重复）。
    ordered = list(applicants)
    for pos, left in enumerate(ordered):
        for right in ordered[pos + 1 :]:
            left_name = normalize_name(left["name"])
            right_name = normalize_name(right["name"])
            for rule in policy.fuzzy_rules:
                if not (rule._compiled.search(left["name"]) and rule._compiled.search(right["name"])):
                    continue
                matched_field = None
                for field_name in sorted(rule.weak_fields):
                    left_value = str(left.get("weak", {}).get(field_name, "")).strip().casefold()
                    right_value = str(right.get("weak", {}).get(field_name, "")).strip().casefold()
                    if left_value and left_value == right_value:
                        matched_field = field_name
                        break
                if rule.require_weak_match and matched_field is None:
                    continue
                explanations.append(
                    {
                        "rule_id": rule.rule_id,
                        "rule_family": "R3-fuzzy-suspect",
                        "applicant_ids": sorted([left["applicant_id"], right["applicant_id"]]),
                        "normalized_names": [left_name, right_name],
                        "weak_field": matched_field,
                        "note": rule.note,
                        "suspect_only": True,
                    }
                )

    groups: dict[str, list[str]] = {}
    for applicant_id in ids:
        groups.setdefault(uf.find(applicant_id), []).append(applicant_id)
    clusters: list[dict[str, Any]] = []
    covered_pairs: set[tuple[str, str]] = set()
    for member_ids in groups.values():
        member_ids = sorted(member_ids)
        if len(member_ids) == 1:
            continue
        member_set = set(member_ids)
        for pos, left in enumerate(member_ids):
            for right in member_ids[pos + 1 :]:
                covered_pairs.add((left, right))
        related = [
            explanation
            for explanation in explanations
            if not explanation.get("suspect_only")
            and set(explanation["applicant_ids"]).issubset(member_set)
        ]
        suspects = [
            explanation
            for explanation in explanations
            if explanation.get("suspect_only")
            and set(explanation["applicant_ids"]).issubset(member_set)
        ]
        # R1 连通分量：仅由硬标识合并形成，供归属时判定同一实际主体。
        identity_groups: dict[str, list[str]] = {}
        for applicant_id in member_ids:
            identity_groups.setdefault(uf_identity.find(applicant_id), []).append(applicant_id)
        identity_components = [
            sorted(values) for values in identity_groups.values() if len(values) > 1
        ]
        clusters.append(
            {
                "cluster_id": "cluster:" + ",".join(member_ids),
                "applicant_ids": member_ids,
                "duplicate_confirmed": bool(identity_components),
                "shared_group": any(
                    item["rule_family"] == "R2-affiliation" for item in related
                ),
                "identity_components": identity_components,
                "explanations": related,
                "suspects": suspects,
            }
        )

    # R3 疑似对若不在任何 R1/R2 簇内，单独作为疑似簇输出，不参与自动判重。
    for explanation in explanations:
        if not explanation.get("suspect_only"):
            continue
        pair = tuple(sorted(explanation["applicant_ids"]))
        if pair in covered_pairs:
            continue
        covered_pairs.add(pair)
        clusters.append(
            {
                "cluster_id": "cluster:suspect:" + ",".join(pair),
                "applicant_ids": list(pair),
                "duplicate_confirmed": False,
                "shared_group": False,
                "identity_components": [],
                "explanations": [],
                "suspects": [explanation],
            }
        )
    return sorted(clusters, key=lambda item: item["cluster_id"])


def equal_shares(member_ids: Sequence[str]) -> list[Decimal]:
    """确定性的最大余数法：量化到百万分之一，余数按编号顺序补给。"""

    n = len(member_ids)
    if n == 0:
        raise AttributionRuleError("分摊成员不能为空")
    exact = [Decimal(1) / Decimal(n)] * n
    floored = [value.quantize(SHARE_PLACES, rounding=ROUND_DOWN) for value in exact]
    remainder = Decimal(1) - sum(floored, Decimal(0))
    steps = int((remainder / SHARE_QUANTUM).to_integral_value())
    for index in range(steps):
        floored[index % n] += SHARE_QUANTUM
    total = sum(floored, Decimal(0))
    if total != Decimal(1):
        raise AttributionRuleError(f"分摊总量不守恒: {format(total, 'f')}")
    return floored


def attribute_cluster(
    policy: AttributionPolicy,
    outcome_type: str,
    occurrences: Sequence[Mapping[str, Any]],
    *,
    declared_shares: Mapping[str, str] | None = None,
    clusters: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """对同一成效键的一组登记做去重/共享判定与守恒分摊。

    occurrences 元素：outcome_id / applicant_id / beneficiary_id / value /
    occurred_at / evidence_level / late_evidence / milestone_ids。
    必须已经按同一 outcome_key 过滤；时间间隔分组在本函数内完成。
    clusters 为 detect_clusters 的输出：R1 确认簇内的不同申请主体视为同一
    实际主体，其重复报送判为 duplicate；仅 R2 关联的主体之间才算共享分摊。
    """

    duplicate_sets = [
        frozenset(component)
        for item in clusters
        for component in item.get("identity_components", ())
    ]

    def same_actual_subject(left: str, right: str) -> bool:
        return any(left in group and right in group for group in duplicate_sets)

    rule = policy.outcome_rule(outcome_type)
    ordered = sorted(occurrences, key=lambda item: (item["occurred_at"], item["outcome_id"]))
    if not ordered:
        raise AttributionRuleError("成效登记不能为空")

    anchor_date = _as_date(ordered[0]["occurred_at"])
    window: list[dict[str, Any]] = []
    for item in ordered:
        day_distance = (_as_date(item["occurred_at"]) - anchor_date).days
        if day_distance <= rule.dedup_window_days:
            window.append(item)
        else:
            # 超出窗口期的登记在调用方拆成新组处理；引擎拒绝静默合并。
            raise AttributionRuleError(
                f"成效 {item['outcome_id']} 与首报间隔 {day_distance} 天，"
                f"超过 {rule.dedup_window_days} 天窗口，请另立成效键"
            )

    anchor = window[0]
    explanation: list[dict[str, Any]] = [
        {
            "rule_family": "outcome-window",
            "anchor_outcome_id": anchor["outcome_id"],
            "dedup_window_days": rule.dedup_window_days,
            "min_evidence_level": rule.min_evidence_level,
        }
    ]
    if int(anchor["evidence_level"]) < rule.min_evidence_level:
        raise AttributionRuleError(
            f"首报成效 {anchor['outcome_id']} 证据等级 {anchor['evidence_level']} "
            f"低于门槛 {rule.min_evidence_level}，不能作为归属锚点"
        )
    if anchor.get("late_evidence") and not rule.late_evidence_creates_revision:
        raise AttributionRuleError(
            f"首报成效 {anchor['outcome_id']} 为迟到证据，当前政策禁止其形成归属"
        )

    eligible: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for item in window:
        if int(item["evidence_level"]) < rule.min_evidence_level:
            explanation.append(
                {
                    "rule_family": "evidence-gate",
                    "outcome_id": item["outcome_id"],
                    "evidence_level": int(item["evidence_level"]),
                    "decision": "excluded",
                }
            )
            continue
        if item.get("late_evidence") and not rule.late_evidence_creates_revision:
            explanation.append(
                {
                    "rule_family": "late-evidence-gate",
                    "outcome_id": item["outcome_id"],
                    "decision": "excluded",
                }
            )
            continue
        same_applicant = item["applicant_id"] == anchor["applicant_id"]
        same_beneficiary = item["beneficiary_id"] == anchor["beneficiary_id"]
        same_subject = same_actual_subject(item["applicant_id"], anchor["applicant_id"])
        if item["outcome_id"] != anchor["outcome_id"] and (
            same_applicant or same_beneficiary or same_subject
        ):
            if same_applicant or same_subject:
                reason = "same_applicant" if same_applicant else "same_hard_identity"
            else:
                reason = "same_beneficiary"
            duplicates.append(
                {
                    "outcome_id": item["outcome_id"],
                    "duplicate_of": anchor["outcome_id"],
                    "reason": reason,
                    "rule_family": "dedup-identity",
                    "dedup_window_days": rule.dedup_window_days,
                }
            )
            continue
        eligible.append(item)

    # 同一主体重复报送只计一次；其余为共同贡献。
    contributors: list[dict[str, Any]] = []
    seen_applicants: set[str] = set()
    for item in eligible:
        if item["applicant_id"] in seen_applicants:
            duplicates.append(
                {
                    "outcome_id": item["outcome_id"],
                    "duplicate_of": anchor["outcome_id"],
                    "reason": "repeat_report",
                    "rule_family": "dedup-identity",
                }
            )
            continue
        seen_applicants.add(item["applicant_id"])
        contributors.append(item)

    if not contributors:
        raise AttributionRuleError("窗口内没有达到证据门槛的成效登记，无法形成归属")

    if len(contributors) > rule.max_shareholders:
        raise AttributionRuleError(
            f"{outcome_type} 共同贡献方 {len(contributors)} 家，"
            f"超过政策上限 {rule.max_shareholders} 家"
        )

    member_ids = [item["applicant_id"] for item in contributors]
    if declared_shares:
        unknown = set(declared_shares) - set(member_ids)
        if unknown:
            raise AttributionRuleError(f"分摊比例给了非贡献方: {sorted(unknown)}")
        if set(declared_shares) != set(member_ids):
            raise AttributionRuleError("共同贡献方必须全部给出分摊比例")
        shares = validate_share([declared_shares[member] for member in member_ids])
        if sum(shares, Decimal(0)) != Decimal(1):
            raise AttributionRuleError("分摊比例之和必须精确等于 1")
        share_map = dict(zip(member_ids, shares))
        explanation.append(
            {"rule_family": "declared-split", "total": format(sum(shares, Decimal(0)), "f")}
        )
    else:
        values = equal_shares(member_ids)
        share_map = dict(zip(member_ids, values))
        explanation.append(
            {
                "rule_family": "equal-split",
                "quantum": format(SHARE_QUANTUM, "f"),
                "remainder_rule": "按申请主体编号顺序以最大余数法补足",
            }
        )

    total_value = Decimal(str(anchor["value"]))
    allocations: list[ClusterShare] = []
    for position, item in enumerate(contributors):
        share = share_map[item["applicant_id"]]
        allocations.append(
            ClusterShare(
                applicant_id=item["applicant_id"],
                beneficiary_id=item["beneficiary_id"],
                share=share,
                value_share=(total_value * share).quantize(SHARE_PLACES, rounding=ROUND_DOWN),
                role="primary" if item["outcome_id"] == anchor["outcome_id"] else "contributor",
            )
        )
    # 价值分摊同样守恒：量化余值计入主报方。
    allocated_value = sum((item.value_share for item in allocations), Decimal(0))
    residual = total_value.quantize(SHARE_PLACES) - allocated_value
    if residual and allocations:
        primary = next(item for item in allocations if item.role == "primary")
        allocations = [
            ClusterShare(
                applicant_id=item.applicant_id,
                beneficiary_id=item.beneficiary_id,
                share=item.share,
                value_share=item.value_share + residual if item is primary else item.value_share,
                role=item.role,
            )
            for item in allocations
        ]
    conserved = sum((item.value_share for item in allocations), Decimal(0)) == total_value.quantize(
        SHARE_PLACES
    )
    if not conserved:
        raise AttributionRuleError("成效价值分摊总量不守恒")

    late = [item["outcome_id"] for item in window if item.get("late_evidence")]
    return {
        "outcome_type": outcome_type,
        "outcome_key": anchor["outcome_key"],
        "status": "single" if len(allocations) == 1 else "shared",
        "anchor_outcome_id": anchor["outcome_id"],
        "allocations": [item.to_dict() for item in allocations],
        "duplicates": duplicates,
        "late_evidence_outcomes": late,
        "explanation": explanation,
        "total_share": format(sum((item.share for item in allocations), Decimal(0)), "f"),
        "total_value": format(total_value.quantize(SHARE_PLACES), "f"),
        "conserved": True,
    }


def build_claim_views(
    applicants: Sequence[Mapping[str, Any]],
    beneficiaries: Sequence[Mapping[str, Any]],
    organizations: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """解析每条支持申请的受益视图：申请主体经关联组织传导到最终受益人。

    claims 元素：claim_id / program_id / resource_id / applicant_id /
    beneficiary_id / region_code / chain（按顺序经过的组织编号）。
    申请与链路均在登记时冻结，本函数只做只读还原，不随关系变化而改变。
    """

    by_id = {item["applicant_id"]: item for item in applicants}
    beneficiary_by_id = {item["beneficiary_id"]: item for item in beneficiaries}
    org_by_id = {item["organization_id"]: item for item in organizations}

    views: list[dict[str, Any]] = []
    for claim in claims:
        applicant_id = claim["applicant_id"]
        if applicant_id not in by_id:
            raise AttributionRuleError(f"申请 {claim.get('claim_id')} 引用了未冻结主体 {applicant_id}")
        beneficiary_id = claim["beneficiary_id"]
        if beneficiary_id not in beneficiary_by_id:
            raise AttributionRuleError(f"申请 {claim.get('claim_id')} 引用了未冻结受益人 {beneficiary_id}")
        chain: list[dict[str, str]] = []
        for position, organization_id in enumerate(claim.get("chain", ())):
            organization = org_by_id.get(organization_id)
            if organization is None:
                raise AttributionRuleError(
                    f"申请 {claim.get('claim_id')} 的链路引用了未冻结组织 {organization_id}"
                )
            chain.append(
                {
                    "position": position,
                    "organization_id": organization_id,
                    "organization_kind": organization["organization_kind"],
                }
            )
        applicant = by_id[applicant_id]
        beneficiary = beneficiary_by_id[beneficiary_id]
        views.append(
            {
                "claim_id": claim["claim_id"],
                "program_id": claim["program_id"],
                "resource_id": claim.get("resource_id"),
                "applicant_id": applicant_id,
                "applicant_name": applicant["name"],
                "region_code": claim.get("region_code") or applicant["region_code"],
                "beneficiary_id": beneficiary_id,
                "beneficiary_name": beneficiary["name"],
                "chain": chain,
                "direct": not chain,
            }
        )
    return sorted(views, key=lambda item: item["claim_id"])


def _as_date(value: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise AttributionRuleError(f"非法日期: {value}") from exc
