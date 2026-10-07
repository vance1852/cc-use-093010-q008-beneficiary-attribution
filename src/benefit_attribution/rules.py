"""可解释的重复/共享成果识别与守恒分摊规则。

输入是已冻结的申报与关联关系，输出是 Union-Find 聚类结果：每个簇带有
触发的规则编号链（可解释性），unique / duplicate / shared 三种关系定性，
以及总量守恒的分摊份额。规则不访问数据库，便于独立单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import Mapping, Sequence

from .contracts import Policy

getcontext().prec = 28

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class ClaimView:
    """聚类所需的申报投影视图。"""

    claim_id: str
    applicant_id: str
    applicant_legal_code: str | None
    applicant_kind: str
    applicant_region: str
    contact_fingerprint: str | None
    beneficiary_id: str
    beneficiary_legal_code: str | None
    resource_id: str
    evidence_id: str
    milestone_kind: str
    outcome_value: Decimal
    weight: Decimal = ONE


@dataclass(frozen=True, slots=True)
class RelationEdge:
    """两个组织之间在归属基准时点有效的关联关系。"""

    left_org_id: str
    right_org_id: str
    relation_kind: str


@dataclass(slots=True)
class MergeRecord:
    """一次合并的可解释记录：哪两个申报因哪条规则、经哪个组织被合并。"""

    rule_code: str
    claim_a: str
    claim_b: str
    via_org: str | None = None

    def explain_key(self) -> str:
        via = f" via {self.via_org}" if self.via_org else ""
        return f"{self.rule_code}:{self.claim_a}|{self.claim_b}{via}"


@dataclass(slots=True)
class Cluster:
    key: str
    claim_ids: list[str] = field(default_factory=list)
    rule_hits: list[MergeRecord] = field(default_factory=list)

    # 硬身份信号才判定为重复；联系方式指纹只是待核软信号，不剥夺份额。
    HARD_DUPLICATE_CODES = frozenset({"SAME_EVIDENCE", "LEGAL_IDENTITY"})
    SOFT_DUPLICATE_CODES = frozenset({"CONTACT_FINGERPRINT"})

    @property
    def rule_codes(self) -> frozenset[str]:
        return frozenset(hit.rule_code for hit in self.rule_hits)

    @property
    def relation(self) -> str:
        if self.rule_codes & self.HARD_DUPLICATE_CODES:
            return "duplicate"
        return "shared"

    @property
    def suspected_duplicate(self) -> bool:
        return bool(self.rule_codes & self.SOFT_DUPLICATE_CODES) and self.relation == "shared"


@dataclass(slots=True)
class ClusterResult:
    clusters: list[Cluster]
    """每条申报 -> 所在簇的 key。"""
    membership: Mapping[str, str]


class _UnionFind:
    def __init__(self, items: Sequence[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        # 取编号字典序较小者为根，保证聚类结果对输入顺序不敏感。
        if rb < ra:
            ra, rb = rb, ra
        self.parent[rb] = ra
        return True


def _org_claims_index(claims: Sequence[ClaimView]) -> dict[str, list[str]]:
    """组织标识（申请主体或最终受益人）-> 涉及该组织的申报。"""

    index: dict[str, list[str]] = {}
    for claim in claims:
        index.setdefault(claim.applicant_id, []).append(claim.claim_id)
        index.setdefault(claim.beneficiary_id, []).append(claim.claim_id)
    return index


def cluster_claims(
    claims: Sequence[ClaimView],
    relations: Sequence[RelationEdge],
    policy: Policy,
) -> ClusterResult:
    """按政策声明的规则集合，对申报做重复/共享聚类。

    规则按固定顺序执行（编号即优先级说明），所有合并都留下 MergeRecord。
    """

    if not claims:
        return ClusterResult(clusters=[], membership={})

    by_id = {claim.claim_id: claim for claim in claims}
    uf = _UnionFind([claim.claim_id for claim in claims])
    hits: list[MergeRecord] = []
    hit_keys: set[tuple[str, str, str, str | None]] = set()

    def comparable(a: str, b: str) -> bool:
        # 只有同类里程碑成果才允许合并：培训完成不能与业务改善并为同一成果。
        return by_id[a].milestone_kind == by_id[b].milestone_kind

    def merge(rule_code: str, a: str, b: str, via_org: str | None = None) -> None:
        # 每条规则对每对申报只留一条痕（方向、自连去重），即使它们已被更早的
        # 规则连通——硬身份信号（同证据/同法人）不能被先发生的软连通掩盖。
        if a == b or not comparable(a, b):
            return
        record_key = (rule_code, frozenset((a, b)), via_org)
        if record_key not in hit_keys:
            hit_keys.add(record_key)
            first, second = sorted((a, b))
            hits.append(MergeRecord(rule_code, first, second, via_org))
        uf.union(a, b)

    # 规则 1：同一法人身份（申请主体侧），重复。
    if "LEGAL_IDENTITY" in policy.rule_set:
        seen: dict[str, str] = {}
        for claim in sorted(claims, key=lambda c: c.claim_id):
            code = claim.applicant_legal_code
            if not code:
                continue
            if code in seen:
                merge("LEGAL_IDENTITY", seen[code], claim.claim_id)
            else:
                seen[code] = claim.claim_id

    # 规则 2/3/4：经由冻结的关联组织关系合并（集团母子、园区入驻、协会成员）。
    relation_rules = {
        "group_parent": "GROUP_SUBSIDIARY",
        "group_subsidiary": "GROUP_SUBSIDIARY",
        "park_tenant": "PARK_TENANT",
        "association_member": "ASSOCIATION_MEMBER",
    }
    active_rules = policy.rule_set
    org_to_claims = _org_claims_index(claims)
    for edge in sorted(relations, key=lambda e: (e.left_org_id, e.right_org_id, e.relation_kind)):
        rule_code = relation_rules.get(edge.relation_kind)
        if rule_code is None or rule_code not in active_rules:
            continue
        left_claims = org_to_claims.get(edge.left_org_id, [])
        right_claims = org_to_claims.get(edge.right_org_id, [])
        for a in sorted(left_claims):
            for b in sorted(right_claims):
                merge(rule_code, a, b, via_org=edge.right_org_id)

    # 规则 5：同一支持资源被多渠道申报 -> 共享成果。
    if "SHARED_RESOURCE" in active_rules:
        buckets: dict[str, list[str]] = {}
        for claim in claims:
            buckets.setdefault(claim.resource_id, []).append(claim.claim_id)
        for resource_id, members in sorted(buckets.items()):
            if len(members) > 1:
                anchor = sorted(members)[0]
                for other in sorted(members)[1:]:
                    merge("SHARED_RESOURCE", anchor, other, via_org=resource_id)

    # 规则 6：同一份里程碑证据被多项申报引用 -> 重复。
    if "SAME_EVIDENCE" in active_rules:
        buckets = {}
        for claim in claims:
            buckets.setdefault(claim.evidence_id, []).append(claim.claim_id)
        for evidence_id, members in sorted(buckets.items()):
            if len(members) > 1:
                anchor = sorted(members)[0]
                for other in sorted(members)[1:]:
                    merge("SAME_EVIDENCE", anchor, other, via_org=evidence_id)

    # 规则 7：联系方式指纹一致且覆盖地区一致 -> 疑似重复。
    if "CONTACT_FINGERPRINT" in active_rules:
        buckets = {}
        for claim in claims:
            if claim.contact_fingerprint:
                buckets.setdefault((claim.contact_fingerprint, claim.applicant_region), []).append(
                    claim.claim_id
                )
        for _, members in sorted(buckets.items()):
            if len(members) > 1:
                anchor = sorted(members)[0]
                for other in sorted(members)[1:]:
                    merge("CONTACT_FINGERPRINT", anchor, other)

    groups: dict[str, list[str]] = {}
    for claim_id in sorted(by_id):
        groups.setdefault(uf.find(claim_id), []).append(claim_id)

    clusters: list[Cluster] = []
    membership: dict[str, str] = {}
    for root, members in sorted(groups.items()):
        members_sorted = tuple(sorted(members))
        key = _cluster_key(members_sorted)
        cluster = Cluster(key=key, claim_ids=list(members_sorted))
        for hit in hits:
            if uf.find(hit.claim_a) == root:
                cluster.rule_hits.append(hit)
        clusters.append(cluster)
        for claim_id in members_sorted:
            membership[claim_id] = key

    return ClusterResult(clusters=clusters, membership=membership)


def _cluster_key(members_sorted: Sequence[str]) -> str:
    return "C-" + "+".join(members_sorted)


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def split_conserved(
    members: Sequence[ClaimView],
    relation: str,
    share_strategy: str,
) -> tuple[Decimal, list[tuple[ClaimView, Decimal, Decimal]], list[str]]:
    """在簇内守恒分摊。

    返回 (总量, [(成员, 份额, 权重)], 规则说明)：
    - unique：单点申报独得全部；
    - duplicate：总量取各申报最大值（保守口径，绝不求和膨胀），仅保留一处
      归属，其余申报份额记零并留痕；
    - shared：同一总量在共同贡献者之间按策略（等权/申报权重）分摊，
      末位成员承担舍入差，保证分配合计严格等于总量。
    """

    members = tuple(sorted(members, key=lambda c: c.claim_id))
    values = {item.claim_id: item.outcome_value for item in members}
    notes: list[str] = []
    if len(members) == 1:
        only = members[0]
        value = only.outcome_value.quantize(CENT)
        return value, [(only, value, ONE)], ["unique: 单点申报独得"]

    # 多点簇的总量统一到最小币/分单位后守恒分摊。
    total = max(values.values()).quantize(CENT)
    notes.append(f"conserved_total: 簇总量取各申报最大值 {total}，禁止跨渠道求和")

    if relation == "duplicate":
        # 申报值最大者保留归属；并列时取 claim_id 字典序最小者，结果确定。
        owner = sorted(members, key=lambda c: (-c.outcome_value, c.claim_id))[0]
        allocation = [(owner, total, ONE)]
        for other in members:
            if other.claim_id != owner.claim_id:
                allocation.append((other, ZERO, ZERO))
        notes.append("duplicate: 重复成果只归一处，其余申报份额记零")
        return total, allocation, notes

    # shared
    if share_strategy == "equal":
        weights = {item.claim_id: ONE for item in members}
        notes.append("shared: 共同贡献按申报数等权分摊")
    else:
        weights = {}
        for item in members:
            weights[item.claim_id] = item.weight if item.weight > ZERO else ZERO
        if sum(weights.values(), ZERO) == ZERO:
            weights = {item.claim_id: ONE for item in members}
            notes.append("shared: 申报权重全为零，回退为等权")
        else:
            notes.append("shared: 共同贡献按申报权重分摊")

    weight_sum = sum(weights.values(), ZERO)
    raw = {
        claim_id: total * weights[claim_id] / weight_sum
        for claim_id in weights
    }
    allocation: list[tuple[ClaimView, Decimal, Decimal]] = []
    quantized = {claim_id: _quantize_money(value) for claim_id, value in raw.items()}
    remainder = total - sum(quantized.values(), ZERO)
    last_id = members[-1].claim_id
    quantized[last_id] += remainder
    for item in members:
        allocation.append((item, quantized[item.claim_id], weights[item.claim_id]))
    allocated_total = sum(quantized.values(), ZERO)
    assert allocated_total == total, (allocated_total, total)
    return total, allocation, notes
