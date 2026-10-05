"""受益关系与成效归属的领域用例。

核心不变量：
- 申请主体、最终受益人、关联组织、支持资源、里程碑证据、政策版本一经冻结即不可改；
- 关系变化写入新关系行（supersedes 链），证据迟到只产生新的归属版本；
- 共同贡献可分摊，分摊份额与价值总量严格守恒；
- 已对外发布的归属版本永远锁定，任何流程都不得回写；
- 异议期间原结论保持有效，复核独立进行；重复回调只计一次。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, isoformat
from .engine import ENGINE_VERSION, AttributionRuleError, attribute_cluster, build_claim_views, detect_clusters
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .policy import AttributionPolicy, PolicyValidationError
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "steward": {
        "registry.freeze", "outcome.ingest", "dispute.raise",
    },
    "analyst": {
        "policy.publish", "attribution.run", "attribution.publish", "report.read",
    },
    "reviewer": {"dispute.review", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

_APPLICANT_KINDS = {"enterprise", "cooperative", "institution", "other"}
_BENEFICIARY_KINDS = {"enterprise", "micro_business", "cooperative", "household", "person", "other"}
_ORGANIZATION_KINDS = {"park", "association", "subsidiary", "group", "platform", "other"}
_IDENTITY_TYPES = {"business_license", "tax_number", "social_credit", "foreign_registry", "other"}


class AttributionService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    @staticmethod
    def _text(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{field} 必须是非空字符串")
        return value.strip()

    @staticmethod
    def _choice(value: object, field: str, choices: set[str]) -> str:
        text = AttributionService._text(value, field)
        if text not in choices:
            raise ValidationFailed(f"{field} 取值非法: {text}")
        return text

    @staticmethod
    def _digest_fields(**fields: Any) -> str:
        return content_digest([{key: value for key, value in fields.items() if value is not None}])

    # -------------------------------------------------------------- 政策版本

    def publish_policy(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "policy.publish")
        try:
            policy = AttributionPolicy.from_dict(raw)
        except PolicyValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(policy.to_catalog_dict())
        digest = content_digest([policy.to_catalog_dict()])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO policy_versions(policy_id,version,title,effective_from,canonical_json,"
                    "content_sha256,published_by,published_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        policy.policy_id, policy.version, policy.title, policy.effective_from,
                        text, digest, actor_id, self._now(),
                    ),
                )
                identity = f"{policy.policy_id}@{policy.version}"
                self._audit("policy", identity, "policy.published", actor_id, {"sha256": digest})
        except sqlite3.IntegrityError as exc:
            raise Conflict("政策版本或内容摘要已经存在") from exc
        return {
            "policy_id": policy.policy_id,
            "version": policy.version,
            "sha256": digest,
            "title": policy.title,
        }

    def _policy(self, policy_id: str, version: int | None = None) -> tuple[AttributionPolicy, int, str]:
        if version is None:
            row = self.connection.execute(
                "SELECT canonical_json,version,content_sha256 FROM policy_versions "
                "WHERE policy_id=? ORDER BY version DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT canonical_json,version,content_sha256 FROM policy_versions "
                "WHERE policy_id=? AND version=?",
                (policy_id, version),
            ).fetchone()
        if row is None:
            raise NotFound("政策版本不存在")
        return AttributionPolicy.from_dict(json.loads(row["canonical_json"])), row["version"], row["content_sha256"]

    # ------------------------------------------------------------ 冻结登记簿

    def register_applicant(
        self,
        actor_id: str,
        applicant_id: str,
        name: str,
        applicant_kind: str,
        region_code: str,
        identities: Sequence[Mapping[str, Any]] = (),
        weak: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "registry.freeze")
        applicant_id = self._text(applicant_id, "applicant_id")
        name = self._text(name, "name")
        applicant_kind = self._choice(applicant_kind, "applicant_kind", _APPLICANT_KINDS)
        region_code = self._text(region_code, "region_code")
        cleaned_identities: list[tuple[str, str]] = []
        for raw in identities:
            itype = self._choice(raw.get("identity_type"), "identity_type", _IDENTITY_TYPES)
            ivalue = self._text(raw.get("value"), "identity value")
            cleaned_identities.append((itype, ivalue))
        weak = weak or {}
        if not isinstance(weak, Mapping):
            raise ValidationFailed("weak 必须是对象")
        digest = self._digest_fields(
            id=applicant_id, name=name, kind=applicant_kind, region=region_code,
            identities=sorted(cleaned_identities), weak=dict(weak),
        )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO applicants(applicant_id,name,applicant_kind,region_code,weak_json,"
                    "content_sha256,frozen_by,frozen_at) VALUES(?,?,?,?,?,?,?,?)",
                    (applicant_id, name, applicant_kind, region_code, canonical_json(weak),
                     digest, actor_id, self._now()),
                )
                for itype, ivalue in cleaned_identities:
                    self.connection.execute(
                        "INSERT INTO applicant_identities(applicant_id,identity_type,identity_value,"
                        "normalized_value,frozen_at) VALUES(?,?,?,?,?)",
                        (applicant_id, itype, ivalue, ivalue.casefold(), self._now()),
                    )
                self._audit("applicant", applicant_id, "applicant.frozen", actor_id,
                            {"identities": [item[0] for item in cleaned_identities]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("申请主体已冻结或硬标识重复") from exc
        return {"applicant_id": applicant_id, "frozen": True}

    def register_beneficiary(
        self,
        actor_id: str,
        beneficiary_id: str,
        name: str,
        beneficiary_kind: str,
        region_code: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "registry.freeze")
        beneficiary_id = self._text(beneficiary_id, "beneficiary_id")
        name = self._text(name, "name")
        beneficiary_kind = self._choice(beneficiary_kind, "beneficiary_kind", _BENEFICIARY_KINDS)
        region_code = self._text(region_code, "region_code")
        digest = self._digest_fields(
            id=beneficiary_id, name=name, kind=beneficiary_kind, region=region_code
        )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO beneficiaries(beneficiary_id,name,beneficiary_kind,region_code,"
                    "content_sha256,frozen_by,frozen_at) VALUES(?,?,?,?,?,?,?)",
                    (beneficiary_id, name, beneficiary_kind, region_code, digest, actor_id, self._now()),
                )
                self._audit("beneficiary", beneficiary_id, "beneficiary.frozen", actor_id, {})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"最终受益人已冻结: {beneficiary_id}") from exc
        return {"beneficiary_id": beneficiary_id, "frozen": True}

    def register_organization(
        self,
        actor_id: str,
        organization_id: str,
        name: str,
        organization_kind: str,
        region_code: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "registry.freeze")
        organization_id = self._text(organization_id, "organization_id")
        name = self._text(name, "name")
        organization_kind = self._choice(organization_kind, "organization_kind", _ORGANIZATION_KINDS)
        region_code = self._text(region_code, "region_code")
        digest = self._digest_fields(
            id=organization_id, name=name, kind=organization_kind, region=region_code
        )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO organizations(organization_id,name,organization_kind,region_code,"
                    "content_sha256,frozen_by,frozen_at) VALUES(?,?,?,?,?,?,?)",
                    (organization_id, name, organization_kind, region_code, digest, actor_id, self._now()),
                )
                self._audit("organization", organization_id, "organization.frozen", actor_id,
                            {"kind": organization_kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"关联组织已冻结: {organization_id}") from exc
        return {"organization_id": organization_id, "frozen": True}

    def register_relation(
        self,
        actor_id: str,
        relation_id: str,
        left_applicant_id: str,
        right_applicant_id: str,
        kind: str,
        valid_from: str,
        *,
        organization_id: str | None = None,
        valid_to: str | None = None,
        supersedes_relation_id: str | None = None,
    ) -> dict[str, Any]:
        """登记关联关系。关系变化不回写旧行，而是追加 supersedes 新行。"""

        self._require(actor_id, "registry.freeze")
        relation_id = self._text(relation_id, "relation_id")
        left = self._text(left_applicant_id, "left_applicant_id")
        right = self._text(right_applicant_id, "right_applicant_id")
        kind = self._choice(kind, "kind", {
            "parent", "subsidiary", "branch", "association_member", "park_tenant",
            "joint_venture", "same_address", "other",
        })
        valid_from = self._text(valid_from, "valid_from")
        if valid_to is not None:
            valid_to = self._text(valid_to, "valid_to")
            if valid_to <= valid_from:
                raise ValidationFailed("valid_to 必须晚于 valid_from")
        digest = self._digest_fields(
            id=relation_id, left=left, right=right, kind=kind, valid_from=valid_from,
            valid_to=valid_to, organization_id=organization_id, supersedes=supersedes_relation_id,
        )
        try:
            with transaction(self.connection, immediate=True):
                if supersedes_relation_id is not None:
                    old = self.connection.execute(
                        "SELECT relation_id FROM relations WHERE relation_id=?",
                        (supersedes_relation_id,),
                    ).fetchone()
                    if old is None:
                        raise NotFound(f"被取代的关系不存在: {supersedes_relation_id}")
                self.connection.execute(
                    "INSERT INTO relations(relation_id,left_applicant_id,right_applicant_id,"
                    "organization_id,kind,valid_from,valid_to,content_sha256,supersedes_relation_id,"
                    "frozen_by,frozen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (relation_id, left, right, organization_id, kind, valid_from, valid_to, digest,
                     supersedes_relation_id, actor_id, self._now()),
                )
                self._audit("relation", relation_id, "relation.frozen", actor_id,
                            {"kind": kind, "supersedes": supersedes_relation_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("关系编号冲突或引用了未冻结主体/组织") from exc
        return {"relation_id": relation_id, "supersedes": supersedes_relation_id}

    def register_resource(
        self,
        actor_id: str,
        resource_id: str,
        program_id: str,
        resource_type: str,
        region_code: str,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "registry.freeze")
        resource_id = self._text(resource_id, "resource_id")
        program_id = self._text(program_id, "program_id")
        resource_type = self._choice(resource_type, "resource_type", {
            "training", "platform", "grant", "voucher", "mentoring", "credit_line", "other",
        })
        region_code = self._text(region_code, "region_code")
        detail = detail or {}
        digest = self._digest_fields(
            id=resource_id, program=program_id, type=resource_type,
            region=region_code, detail=dict(detail),
        )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO resources(resource_id,program_id,resource_type,region_code,detail_json,"
                    "content_sha256,frozen_by,frozen_at) VALUES(?,?,?,?,?,?,?,?)",
                    (resource_id, program_id, resource_type, region_code, canonical_json(detail),
                     digest, actor_id, self._now()),
                )
                self._audit("resource", resource_id, "resource.frozen", actor_id,
                            {"type": resource_type})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"支持资源已冻结: {resource_id}") from exc
        return {"resource_id": resource_id, "frozen": True}

    def register_claim(
        self,
        actor_id: str,
        claim_id: str,
        applicant_id: str,
        beneficiary_id: str,
        program_id: str,
        region_code: str,
        *,
        resource_id: str | None = None,
        chain: Sequence[str] = (),
    ) -> dict[str, Any]:
        """冻结一条支持申请及其受益传导链（园区→协会→子公司…→最终受益人）。"""

        self._require(actor_id, "registry.freeze")
        claim_id = self._text(claim_id, "claim_id")
        applicant_id = self._text(applicant_id, "applicant_id")
        beneficiary_id = self._text(beneficiary_id, "beneficiary_id")
        program_id = self._text(program_id, "program_id")
        region_code = self._text(region_code, "region_code")
        chain = [self._text(value, "chain[]") for value in chain]
        digest = self._digest_fields(
            id=claim_id, applicant=applicant_id, beneficiary=beneficiary_id,
            program=program_id, resource=resource_id, region=region_code, chain=chain,
        )
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO claims(claim_id,applicant_id,beneficiary_id,program_id,resource_id,"
                    "region_code,content_sha256,frozen_by,frozen_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (claim_id, applicant_id, beneficiary_id, program_id, resource_id, region_code,
                     digest, actor_id, self._now()),
                )
                cursor.close()
                for position, organization_id in enumerate(chain):
                    self.connection.execute(
                        "INSERT INTO claim_chain(claim_id,position,organization_id) VALUES(?,?,?)",
                        (claim_id, position, organization_id),
                    )
                self._audit("claim", claim_id, "claim.frozen", actor_id,
                            {"resource_id": resource_id, "chain_length": len(chain)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("申请编号冲突或引用了未冻结主体/受益人/资源/组织") from exc
        return {"claim_id": claim_id, "frozen": True, "chain_length": len(chain)}

    def register_milestone(
        self,
        actor_id: str,
        milestone_id: str,
        evidence_type: str,
        evidence_level: int,
        occurred_at: str,
        content_sha256: str,
        *,
        claim_id: str | None = None,
        late: bool = False,
    ) -> dict[str, Any]:
        """冻结里程碑证据（培训完成、平台上线、业务改善等报送均在此归一）。"""

        self._require(actor_id, "registry.freeze")
        milestone_id = self._text(milestone_id, "milestone_id")
        evidence_type = self._choice(evidence_type, "evidence_type", {
            "training_completed", "platform_online", "business_improvement",
            "certificate", "filing", "other",
        })
        try:
            evidence_level = int(evidence_level)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("evidence_level 必须是 1..5 的整数") from exc
        if not 1 <= evidence_level <= 5:
            raise ValidationFailed("evidence_level 必须在 1..5")
        occurred_at = self._text(occurred_at, "occurred_at")
        content_sha256 = self._text(content_sha256, "content_sha256").lower()
        if len(content_sha256) != 64:
            raise ValidationFailed("证据摘要必须是 64 位 SHA-256")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO milestones(milestone_id,claim_id,evidence_type,evidence_level,"
                    "occurred_at,content_sha256,late,registered_by,registered_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (milestone_id, claim_id, evidence_type, evidence_level, occurred_at,
                     content_sha256, 1 if late else 0, actor_id, self._now()),
                )
                self._audit("milestone", milestone_id, "milestone.frozen", actor_id,
                            {"evidence_type": evidence_type, "late": late})
        except sqlite3.IntegrityError as exc:
            raise Conflict("里程碑编号冲突或引用了未冻结申请") from exc
        return {"milestone_id": milestone_id, "frozen": True}

    # ------------------------------------------------------------ 成效与回调

    def ingest_callback(
        self,
        actor_id: str,
        callback_id: str,
        source: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """接收外部机构回调并登记成效；重复回调幂等返回，绝不再次计入。"""

        self._require(actor_id, "outcome.ingest")
        callback_id = self._text(callback_id, "callback_id")
        source = self._text(source, "source")
        if not isinstance(payload, Mapping):
            raise ValidationFailed("payload 必须是对象")
        payload_digest = content_digest([payload])
        now = self._now()
        with transaction(self.connection, immediate=True):
            same_id = self.connection.execute(
                "SELECT callback_id,payload_sha256,outcome_id,status,duplicate_of_callback_id "
                "FROM callback_receipts WHERE callback_id=?",
                (callback_id,),
            ).fetchone()
            if same_id is not None:
                # 同一投递编号重放：内容一致则幂等返回，绝不二次计入；
                # 内容不一致则拒绝，防止编号被复用。
                if same_id["payload_sha256"] != payload_digest:
                    raise Conflict(f"回调编号已用于不同内容: {callback_id}")
                self._audit("callback", callback_id, "callback.replayed", actor_id,
                            {"status": same_id["status"]})
                return {
                    "callback_id": callback_id,
                    "status": same_id["status"],
                    "duplicate_of": same_id["duplicate_of_callback_id"],
                    "outcome_id": same_id["outcome_id"],
                    "counted": False,
                }
            prior = self.connection.execute(
                "SELECT callback_id,outcome_id,status FROM callback_receipts "
                "WHERE source=? AND payload_sha256=? AND status='processed'",
                (source, payload_digest),
            ).fetchone()
            if prior is not None:
                self.connection.execute(
                    "INSERT INTO callback_receipts(callback_id,source,payload_sha256,outcome_id,status,"
                    "duplicate_of_callback_id,received_by,received_at) VALUES(?,?,?,?,?,?,?,?)",
                    (callback_id, source, payload_digest, prior["outcome_id"], "duplicate",
                     prior["callback_id"], actor_id, now),
                )
                self._audit("callback", callback_id, "callback.duplicate", actor_id,
                            {"duplicate_of": prior["callback_id"]})
                return {
                    "callback_id": callback_id,
                    "status": "duplicate",
                    "duplicate_of": prior["callback_id"],
                    "outcome_id": prior["outcome_id"],
                    "counted": False,
                }
            try:
                outcome_id = self.connection.execute(
                    "INSERT INTO callback_receipts(callback_id,source,payload_sha256,status,"
                    "received_by,received_at) VALUES(?,?,?,?,?,?)",
                    (callback_id, source, payload_digest, "processed", actor_id, now),
                )
                outcome_id.close()
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"回调编号已存在: {callback_id}") from exc
            outcome = self._insert_outcome(actor_id, payload, {"callback_id": callback_id, "source": source})
            self.connection.execute(
                "UPDATE callback_receipts SET outcome_id=? WHERE callback_id=?",
                (outcome["outcome_id"], callback_id),
            )
        return {"callback_id": callback_id, "status": "processed", "outcome_id": outcome["outcome_id"],
                "counted": True}

    def register_outcome(self, actor_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """不经过回调通道直接登记成效（同样经过全部一致性校验）。"""

        self._require(actor_id, "outcome.ingest")
        with transaction(self.connection, immediate=True):
            outcome = self._insert_outcome(actor_id, payload, {"direct": True})
        return outcome

    def _insert_outcome(self, actor_id: str, payload: Mapping[str, Any], channel: Mapping[str, Any]) -> dict[str, Any]:
        outcome_id = self._text(payload.get("outcome_id"), "outcome_id")
        outcome_type = self._text(payload.get("outcome_type"), "outcome_type")
        outcome_key = self._text(payload.get("outcome_key"), "outcome_key")
        claim_id = self._text(payload.get("claim_id"), "claim_id")
        claim = self.connection.execute(
            "SELECT applicant_id,beneficiary_id FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if claim is None:
            raise NotFound(f"成效引用的申请不存在: {claim_id}")
        value = self._text(payload.get("value"), "value")
        try:
            decimal_value = Decimal(value)
        except Exception as exc:
            raise ValidationFailed("value 必须是十进制数值字符串") from exc
        if not decimal_value.is_finite() or decimal_value < 0:
            raise ValidationFailed("value 必须是非负有限数值")
        occurred_at = self._text(payload.get("occurred_at"), "occurred_at")
        try:
            evidence_level = int(payload.get("evidence_level"))
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("evidence_level 必须是 1..5 的整数") from exc
        if not 1 <= evidence_level <= 5:
            raise ValidationFailed("evidence_level 必须在 1..5")
        milestone_ids = payload.get("milestone_ids", [])
        if not isinstance(milestone_ids, Sequence) or isinstance(milestone_ids, str):
            raise ValidationFailed("milestone_ids 必须是数组")
        for milestone_id in milestone_ids:
            row = self.connection.execute(
                "SELECT claim_id FROM milestones WHERE milestone_id=?", (milestone_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"里程碑证据不存在: {milestone_id}")
            if row["claim_id"] != claim_id:
                raise ValidationFailed(f"里程碑 {milestone_id} 不属于申请 {claim_id}")
        late = 1 if bool(payload.get("late_evidence", False)) else 0
        digest = content_digest([dict(payload) | {"channel": dict(channel)}])
        try:
            self.connection.execute(
                "INSERT INTO outcomes(outcome_id,outcome_type,outcome_key,claim_id,applicant_id,"
                "beneficiary_id,value,occurred_at,evidence_level,milestone_ids_json,late_evidence,"
                "content_sha256,registered_by,registered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (outcome_id, outcome_type, outcome_key, claim_id, claim["applicant_id"],
                 claim["beneficiary_id"], value, occurred_at, evidence_level,
                 canonical_json(list(milestone_ids)), late, digest, actor_id, self._now()),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"成效已登记: {outcome_id}") from exc
        self._audit("outcome", outcome_id, "outcome.registered", actor_id,
                    {"outcome_key": outcome_key, "channel": dict(channel), "late_evidence": bool(late)})
        return {"outcome_id": outcome_id, "outcome_key": outcome_key, "counted": True}

    # ------------------------------------------------------------- 重复识别

    def detect_duplicates(
        self, actor_id: str, policy_id: str, version: int | None = None, *, as_of: str | None = None
    ) -> dict[str, Any]:
        """按指定政策版本识别重复（R1）与共享（R2）主体簇，R3 仅列为疑似。"""

        self._require(actor_id, "report.read")
        policy, resolved_version, policy_digest = self._policy(policy_id, version)
        as_of = as_of or self._now()
        applicants = self._applicant_snapshots()
        relations = self._relation_snapshots(as_of)
        clusters = detect_clusters(policy, applicants, relations, as_of)
        return {
            "policy_id": policy_id,
            "policy_version": resolved_version,
            "policy_sha256": policy_digest,
            "as_of": as_of,
            "clusters": clusters,
        }

    def _applicant_snapshots(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT applicant_id,name,applicant_kind,region_code,weak_json FROM applicants ORDER BY applicant_id"
        ).fetchall()
        identity_rows = self.connection.execute(
            "SELECT applicant_id,identity_type,identity_value FROM applicant_identities "
            "ORDER BY applicant_id,identity_type,identity_value"
        ).fetchall()
        identities: dict[str, list[dict[str, str]]] = {}
        for row in identity_rows:
            identities.setdefault(row["applicant_id"], []).append(
                {"identity_type": row["identity_type"], "value": row["identity_value"]}
            )
        return [
            {
                "applicant_id": row["applicant_id"],
                "name": row["name"],
                "kind": row["applicant_kind"],
                "region_code": row["region_code"],
                "weak": json.loads(row["weak_json"]),
                "identities": identities.get(row["applicant_id"], []),
            }
            for row in rows
        ]

    def _relation_snapshots(self, as_of: str) -> list[dict[str, Any]]:
        """还原只追加关系表在 as_of 的生效状态：被新行取代者在新行生效时终止。"""

        rows = self.connection.execute(
            "SELECT relation_id,left_applicant_id,right_applicant_id,organization_id,kind,"
            "valid_from,valid_to,supersedes_relation_id FROM relations ORDER BY relation_id"
        ).fetchall()
        successors = {
            row["supersedes_relation_id"]: row["valid_from"]
            for row in rows
            if row["supersedes_relation_id"]
        }
        result = []
        for row in rows:
            valid_to = row["valid_to"]
            cutoff = successors.get(row["relation_id"])
            if cutoff is not None and (valid_to is None or cutoff < valid_to):
                valid_to = cutoff
            result.append(
                {
                    "relation_id": row["relation_id"],
                    "left_applicant_id": row["left_applicant_id"],
                    "right_applicant_id": row["right_applicant_id"],
                    "organization_id": row["organization_id"],
                    "kind": row["kind"],
                    "valid_from": row["valid_from"],
                    "valid_to": valid_to,
                    "supersedes": row["supersedes_relation_id"],
                }
            )
        return result

    # ------------------------------------------------------------- 成效归属

    def run_attribution(
        self,
        actor_id: str,
        outcome_key: str,
        policy_id: str,
        *,
        version: int | None = None,
        reason: str = "",
        declared_shares: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """对一个成效键执行归属；事实变化只追加新版本，绝不回写历史版本。"""

        self._require(actor_id, "attribution.run")
        reason = self._text(reason or "初次归属", "reason")
        with transaction(self.connection, immediate=True):
            return self._store_attribution(
                actor_id, outcome_key, policy_id, version, reason, declared_shares
            )

    def _store_attribution(
        self,
        actor_id: str,
        outcome_key: str,
        policy_id: str,
        version: int | None,
        reason: str,
        declared_shares: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        """写入归属版本；调用方必须已持有 IMMEDIATE 事务。"""

        policy, resolved_version, policy_digest = self._policy(policy_id, version)
        as_of = self._now()
        outcome_rows = self.connection.execute(
            "SELECT * FROM outcomes WHERE outcome_key=? ORDER BY occurred_at,outcome_id",
            (outcome_key,),
        ).fetchall()
        if not outcome_rows:
            raise NotFound(f"成效键下没有登记记录: {outcome_key}")
        outcome_types = {row["outcome_type"] for row in outcome_rows}
        if len(outcome_types) != 1:
            raise ValidationFailed(f"成效键 {outcome_key} 混合了多种成效类型: {sorted(outcome_types)}")
        occurrences = [
            {
                "outcome_id": row["outcome_id"],
                "outcome_key": row["outcome_key"],
                "claim_id": row["claim_id"],
                "applicant_id": row["applicant_id"],
                "beneficiary_id": row["beneficiary_id"],
                "value": row["value"],
                "occurred_at": row["occurred_at"],
                "evidence_level": row["evidence_level"],
                "late_evidence": bool(row["late_evidence"]),
                "milestone_ids": json.loads(row["milestone_ids_json"]),
            }
            for row in outcome_rows
        ]
        applicants = self._applicant_snapshots()
        relations = self._relation_snapshots(as_of)
        clusters = detect_clusters(policy, applicants, relations, as_of)

        try:
            result = attribute_cluster(
                policy,
                next(iter(outcome_types)),
                occurrences,
                declared_shares=dict(declared_shares) if declared_shares else None,
                clusters=clusters,
            )
        except AttributionRuleError as exc:
            raise ValidationFailed(str(exc)) from exc

        input_snapshot = {
            "policy_sha256": policy_digest,
            "engine_version": ENGINE_VERSION,
            "as_of": as_of,
            "occurrences": occurrences,
            "clusters": clusters,
            "declared_shares": dict(declared_shares) if declared_shares else None,
        }
        # 版本摘要只覆盖冻结事实与规则；评估时刻本身不构成事实变化，
        # 因此墙上时间不同不会产生新版本，只有证据/关系/政策变化才会。
        input_digest = content_digest([{
            key: value for key, value in input_snapshot.items() if key != "as_of"
        }])

        prior = self.connection.execute(
            "SELECT attribution_id,seq_no,status,input_sha256 FROM attributions "
            "WHERE outcome_key=? ORDER BY seq_no DESC LIMIT 1",
            (outcome_key,),
        ).fetchone()
        if prior is not None and prior["input_sha256"] == input_digest:
            raise InvalidState(
                f"冻结事实与第 {prior['seq_no']} 版完全一致（{prior['attribution_id']}），无需形成新版本"
            )
        seq_no = 1 if prior is None else prior["seq_no"] + 1
        attribution_id = f"attr:{outcome_key}:v{seq_no}"
        # 只有未发布的活动版本可被标记为 superseded；已发布版本永久保留。
        if prior is not None and prior["status"] == "active":
            self.connection.execute(
                "UPDATE attributions SET status='superseded',superseded_at=? "
                "WHERE attribution_id=? AND status='active'",
                (as_of, prior["attribution_id"]),
            )
        supersedes_id = prior["attribution_id"] if prior is not None else None
        self.connection.execute(
            "INSERT INTO attributions(attribution_id,outcome_key,outcome_type,policy_id,policy_version,"
            "engine_version,seq_no,window_anchor_outcome_id,status,result_json,explanations_json,"
            "input_sha256,total_share,total_value,supersedes_attribution_id,reason,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?)",
            (
                attribution_id, outcome_key, result["outcome_type"], policy_id, resolved_version,
                ENGINE_VERSION, seq_no, result["anchor_outcome_id"],
                canonical_json(result["allocations"]),
                canonical_json({"clusters": clusters, "trace": result["explanation"],
                                "duplicates": result["duplicates"],
                                "late_evidence_outcomes": result["late_evidence_outcomes"]}),
                input_digest, result["total_share"], result["total_value"],
                supersedes_id, reason, actor_id, as_of,
            ),
        )
        anchor_id = result["anchor_outcome_id"]
        duplicate_ids = {item["outcome_id"] for item in result["duplicates"]}
        for occurrence in occurrences:
            oid = occurrence["outcome_id"]
            if oid in duplicate_ids:
                role = "duplicate"
            elif oid == anchor_id:
                role = "anchor"
            else:
                role = "member"
            self.connection.execute(
                "INSERT INTO attribution_outcomes(attribution_id,outcome_id,role) VALUES(?,?,?)",
                (attribution_id, oid, role),
            )
        self._audit("attribution", attribution_id, "attribution.created", actor_id,
                    {"outcome_key": outcome_key, "seq_no": seq_no, "reason": reason,
                     "supersedes": supersedes_id})
        return {
            "attribution_id": attribution_id,
            "outcome_key": outcome_key,
            "seq_no": seq_no,
            "status": "active",
            "result": result,
            "input_sha256": input_digest,
        }

    def get_attribution(self, actor_id: str, attribution_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        return self._attribution_view(attribution_id)

    def _attribution_view(self, attribution_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM attributions WHERE attribution_id=?", (attribution_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"归属版本不存在: {attribution_id}")
        allocations = json.loads(row["result_json"])
        explanation = json.loads(row["explanations_json"])
        outcomes = self.connection.execute(
            "SELECT outcome_id,role FROM attribution_outcomes "
            "WHERE attribution_id=? ORDER BY outcome_id",
            (attribution_id,),
        ).fetchall()
        publication = self.connection.execute(
            "SELECT publication_id,channel,published_by,published_at FROM publications "
            "WHERE attribution_id=?",
            (attribution_id,),
        ).fetchone()
        return {
            "attribution_id": attribution_id,
            "outcome_key": row["outcome_key"],
            "outcome_type": row["outcome_type"],
            "policy": {"policy_id": row["policy_id"], "version": row["policy_version"]},
            "engine_version": row["engine_version"],
            "seq_no": row["seq_no"],
            "status": row["status"],
            "window_anchor_outcome_id": row["window_anchor_outcome_id"],
            "allocations": allocations,
            "total_share": row["total_share"],
            "total_value": row["total_value"],
            "reason": row["reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "superseded_at": row["superseded_at"],
            "supersedes_attribution_id": row["supersedes_attribution_id"],
            "explanation": explanation,
            "outcomes": [dict(item) for item in outcomes],
            "publication": None if publication is None else dict(publication),
            "input_sha256": row["input_sha256"],
        }

    def publish_attribution(
        self, actor_id: str, attribution_id: str, channel: str, receipt_sha256: str
    ) -> dict[str, Any]:
        """对外发布归属结果；发布即永久锁定，任何后续流程不得回写。"""

        self._require(actor_id, "attribution.publish")
        channel = self._text(channel, "channel")
        receipt_sha256 = self._text(receipt_sha256, "receipt_sha256").lower()
        if len(receipt_sha256) != 64:
            raise ValidationFailed("发布回执摘要必须是 64 位 SHA-256")
        row = self.connection.execute(
            "SELECT status FROM attributions WHERE attribution_id=?", (attribution_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"归属版本不存在: {attribution_id}")
        if row["status"] == "published":
            raise Conflict("该版本已经对外发布，禁止重复发布或回写")
        if row["status"] == "superseded":
            raise InvalidState("已被新版本取代的结果不能对外发布")
        publication_id = f"pub:{attribution_id}"
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "UPDATE attributions SET status='published',published_at=? "
                    "WHERE attribution_id=? AND status='active'",
                    (now, attribution_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("归属版本状态已变化，发布被拒绝")
                self.connection.execute(
                    "INSERT INTO publications(publication_id,attribution_id,channel,receipt_sha256,"
                    "published_by,published_at) VALUES(?,?,?,?,?,?)",
                    (publication_id, attribution_id, channel, receipt_sha256, actor_id, now),
                )
                self._audit("attribution", attribution_id, "attribution.published", actor_id,
                            {"channel": channel, "publication_id": publication_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("发布记录冲突") from exc
        return {"attribution_id": attribution_id, "status": "published", "publication_id": publication_id}

    # --------------------------------------------------------- 异议与独立复核

    def raise_dispute(self, actor_id: str, attribution_id: str, reason: str) -> dict[str, Any]:
        """利益相关方提出异议；原结论在整个复核期间保持不变。"""

        self._require(actor_id, "dispute.raise")
        reason = self._text(reason, "reason")
        row = self.connection.execute(
            "SELECT outcome_key FROM attributions WHERE attribution_id=?", (attribution_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"归属版本不存在: {attribution_id}")
        open_row = self.connection.execute(
            "SELECT dispute_id FROM disputes WHERE attribution_id=? AND status IN ('open','in_review')",
            (attribution_id,),
        ).fetchone()
        if open_row is not None:
            raise Conflict("该版本已有未决异议")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO disputes(attribution_id,outcome_key,reason,status,raised_by,raised_at) "
                    "VALUES(?, ?,?,'open',?,?)",
                    (attribution_id, row["outcome_key"], reason, actor_id, self._now()),
                )
                dispute_id = cursor.lastrowid
                self._audit("dispute", str(dispute_id), "dispute.raised", actor_id,
                            {"attribution_id": attribution_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("异议登记冲突") from exc
        return {
            "dispute_id": dispute_id,
            "status": "open",
            "attribution_id": attribution_id,
            "original_conclusion_preserved": True,
        }

    def start_review(self, actor_id: str, dispute_id: int) -> dict[str, Any]:
        """启动独立复核：复核人既不能是原归属制作人，也不能是异议提出人。"""

        self._require(actor_id, "dispute.review")
        dispute = self.connection.execute(
            "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
        ).fetchone()
        if dispute is None:
            raise NotFound("异议不存在")
        if dispute["status"] != "open":
            raise InvalidState("异议不在待受理状态")
        attribution = self.connection.execute(
            "SELECT created_by FROM attributions WHERE attribution_id=?",
            (dispute["attribution_id"],),
        ).fetchone()
        if actor_id == attribution["created_by"]:
            raise Forbidden("原归属制作人不能独立复核自己的结论")
        if actor_id == dispute["raised_by"]:
            raise Forbidden("异议提出人不能复核自己的异议")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE disputes SET status='in_review' WHERE dispute_id=? AND status='open'",
                (dispute_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("异议状态已变化")
            review_cursor = self.connection.execute(
                "INSERT INTO reviews(dispute_id,reviewer_id,original_attribution_id,started_at) "
                "VALUES(?,?,?,?)",
                (dispute_id, actor_id, dispute["attribution_id"], now),
            )
            review_id = review_cursor.lastrowid
            self._audit("dispute", str(dispute_id), "review.started", actor_id,
                        {"review_id": review_id})
        return {"dispute_id": dispute_id, "review_id": review_id, "status": "in_review"}

    def complete_review(
        self,
        actor_id: str,
        dispute_id: int,
        decision: str,
        finding: str,
        *,
        policy_id: str | None = None,
        version: int | None = None,
        declared_shares: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """完成独立复核。

        - dismiss：驳回异议，原结论维持；
        - uphold：异议成立，依据当前冻结事实形成新的归属版本，
          原版本（含已发布版本）原样保留。
        """

        self._require(actor_id, "dispute.review")
        decision = self._choice(decision, "decision", {"uphold", "dismiss"})
        finding = self._text(finding, "finding")
        review = self.connection.execute(
            "SELECT * FROM reviews WHERE dispute_id=?", (dispute_id,)
        ).fetchone()
        if review is None:
            raise NotFound("复核记录不存在")
        if review["reviewer_id"] != actor_id:
            raise Forbidden("只有受理该异议的复核人可以出具结论")
        if review["completed_at"] is not None:
            raise InvalidState("复核已经完成")
        dispute = self.connection.execute(
            "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
        ).fetchone()
        if dispute["status"] != "in_review":
            raise InvalidState("异议不在复核中")

        new_attribution_id: str | None = None
        now = self._now()
        with transaction(self.connection, immediate=True):
            if decision == "uphold":
                if policy_id is None:
                    raise ValidationFailed("异议成立时必须指定用于重新归属的政策版本")
                # 复核人通过复核流程获得重算授权，直接复用事务内写入逻辑，
                # 不再做 attribution.run 角色检查，也不开启嵌套事务。
                run = self._store_attribution(
                    actor_id,
                    dispute["outcome_key"],
                    policy_id,
                    version,
                    f"独立复核支持异议 dispute#{dispute_id}: {finding}",
                    declared_shares,
                )
                new_attribution_id = run["attribution_id"]
                dispute_status = "upheld"
            else:
                dispute_status = "rejected"
            self.connection.execute(
                "UPDATE disputes SET status=?,closed_by=?,closed_at=?,close_note=? WHERE dispute_id=?",
                (dispute_status, actor_id, now, finding, dispute_id),
            )
            self.connection.execute(
                "UPDATE reviews SET decision=?,new_attribution_id=?,finding=?,completed_at=? "
                "WHERE review_id=?",
                (decision, new_attribution_id, finding, now, review["review_id"]),
            )
            self._audit("dispute", str(dispute_id), f"review.{decision}", actor_id,
                        {"new_attribution_id": new_attribution_id})
        return {
            "dispute_id": dispute_id,
            "status": dispute_status,
            "decision": decision,
            "original_attribution_id": review["original_attribution_id"],
            "new_attribution_id": new_attribution_id,
            "original_conclusion_preserved": True,
        }

    def list_disputes(self, actor_id: str, status: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        sql = (
            "SELECT d.dispute_id,d.attribution_id,d.outcome_key,d.status,d.reason,d.raised_by,"
            "d.raised_at,d.closed_by,d.closed_at,r.reviewer_id,r.decision "
            "FROM disputes d LEFT JOIN reviews r ON r.dispute_id=d.dispute_id"
        )
        rows: list[sqlite3.Row]
        if status is None:
            rows = self.connection.execute(sql + " ORDER BY d.dispute_id").fetchall()
        else:
            status = self._choice(status, "status", {"open", "in_review", "upheld", "rejected"})
            rows = self.connection.execute(
                sql + " WHERE d.status=? ORDER BY d.dispute_id", (status,)
            ).fetchall()
        return {"disputes": [dict(row) for row in rows]}

    # ------------------------------------------------------------- 管理视图

    def coverage_report(self, actor_id: str, policy_id: str, version: int | None = None) -> dict[str, Any]:
        """按任意政策版本查看覆盖地区与有效受益者。

        有效受益者：在该政策版本形成的活动/已发布归属中实际分到份额的最终受益人；
        被判重复、证据门槛剔除者不计入。
        """

        self._require(actor_id, "report.read")
        _, resolved_version, policy_digest = self._policy(policy_id, version)
        rows = self.connection.execute(
            "WITH latest AS ("
            "  SELECT outcome_key, MAX(seq_no) AS seq_no FROM attributions "
            "  WHERE policy_id=? AND policy_version=? AND status IN ('active','published') "
            "  GROUP BY outcome_key"
            ") "
            "SELECT a.attribution_id,a.outcome_key,a.status,a.result_json,"
            "o.applicant_id,o.beneficiary_id,c.region_code "
            "FROM latest l "
            "JOIN attributions a ON a.outcome_key=l.outcome_key AND a.seq_no=l.seq_no "
            "JOIN attribution_outcomes ao ON ao.attribution_id=a.attribution_id "
            "JOIN outcomes o ON o.outcome_id=ao.outcome_id "
            "JOIN claims c ON c.claim_id=o.claim_id "
            "WHERE a.policy_id=? AND a.policy_version=? AND ao.role IN ('anchor','member')",
            (policy_id, resolved_version, policy_id, resolved_version),
        ).fetchall()
        regions: dict[str, dict[str, Any]] = {}
        effective: set[tuple[str, str]] = set()
        counted_outcomes: set[str] = set()
        for row in rows:
            region = regions.setdefault(
                row["region_code"],
                {"region_code": row["region_code"], "effective_beneficiaries": set(),
                 "outcomes": set(), "attributions": set()},
            )
            allocations = json.loads(row["result_json"])
            # 每条成员成效行只取本报送主体名下的分摊，避免整份清单被跨地区重复计算。
            own = [item for item in allocations if item["applicant_id"] == row["applicant_id"]]
            if not own:
                continue
            for item in own:
                effective.add((row["region_code"], item["beneficiary_id"]))
                region["effective_beneficiaries"].add(item["beneficiary_id"])
            region["outcomes"].add(row["outcome_key"])
            region["attributions"].add(row["attribution_id"])
            counted_outcomes.add(row["outcome_key"])
        region_list = []
        for code in sorted(regions):
            item = regions[code]
            region_list.append({
                "region_code": code,
                "effective_beneficiary_count": len(item["effective_beneficiaries"]),
                "outcome_key_count": len(item["outcomes"]),
                "attribution_count": len(item["attributions"]),
            })
        open_disputes = self.connection.execute(
            "SELECT count(*) FROM disputes WHERE status IN ('open','in_review')"
        ).fetchone()[0]
        return {
            "policy_id": policy_id,
            "policy_version": resolved_version,
            "policy_sha256": policy_digest,
            "generated_at": self._now(),
            "regions": region_list,
            "covered_region_count": len(region_list),
            "effective_beneficiary_count": len(effective),
            "counted_outcome_key_count": len(counted_outcomes),
            "open_dispute_count": open_disputes,
        }

    def outcome_lineage(self, actor_id: str, outcome_key: str) -> dict[str, Any]:
        """返回一项成果从证据到发布的完整来源链与全部归属版本。"""

        self._require(actor_id, "report.read")
        outcome_rows = self.connection.execute(
            "SELECT * FROM outcomes WHERE outcome_key=? ORDER BY occurred_at,outcome_id",
            (outcome_key,),
        ).fetchall()
        if not outcome_rows:
            raise NotFound(f"成效键不存在: {outcome_key}")
        outcomes = []
        claim_cache: dict[str, sqlite3.Row] = {}
        for row in outcome_rows:
            claim = claim_cache.get(row["claim_id"])
            if claim is None:
                claim = self.connection.execute(
                    "SELECT * FROM claims WHERE claim_id=?", (row["claim_id"],)
                ).fetchone()
                claim_cache[row["claim_id"]] = claim
            chain_rows = self.connection.execute(
                "SELECT cc.position,cc.organization_id,o.name,o.organization_kind,o.region_code "
                "FROM claim_chain cc JOIN organizations o ON o.organization_id=cc.organization_id "
                "WHERE cc.claim_id=? ORDER BY cc.position",
                (row["claim_id"],),
            ).fetchall()
            milestone_ids = json.loads(row["milestone_ids_json"])
            milestones = []
            for milestone_id in milestone_ids:
                mrow = self.connection.execute(
                    "SELECT milestone_id,evidence_type,evidence_level,occurred_at,content_sha256,late "
                    "FROM milestones WHERE milestone_id=?",
                    (milestone_id,),
                ).fetchone()
                milestones.append(dict(mrow) if mrow else {"milestone_id": milestone_id, "missing": True})
            applicant = self.connection.execute(
                "SELECT name,applicant_kind,region_code FROM applicants WHERE applicant_id=?",
                (row["applicant_id"],),
            ).fetchone()
            beneficiary = self.connection.execute(
                "SELECT name,beneficiary_kind,region_code FROM beneficiaries WHERE beneficiary_id=?",
                (row["beneficiary_id"],),
            ).fetchone()
            receipts = self.connection.execute(
                "SELECT callback_id,source,status,duplicate_of_callback_id,received_by,received_at "
                "FROM callback_receipts WHERE outcome_id=? ORDER BY received_at,callback_id",
                (row["outcome_id"],),
            ).fetchall()
            outcomes.append({
                "outcome_id": row["outcome_id"],
                "outcome_type": row["outcome_type"],
                "value": row["value"],
                "occurred_at": row["occurred_at"],
                "evidence_level": row["evidence_level"],
                "late_evidence": bool(row["late_evidence"]),
                "content_sha256": row["content_sha256"],
                "registered_by": row["registered_by"],
                "registered_at": row["registered_at"],
                "callbacks": [dict(item) for item in receipts],
                "applicant": dict(applicant) | {"applicant_id": row["applicant_id"]},
                "beneficiary": dict(beneficiary) | {"beneficiary_id": row["beneficiary_id"]},
                "claim": {
                    "claim_id": claim["claim_id"],
                    "program_id": claim["program_id"],
                    "resource_id": claim["resource_id"],
                    "region_code": claim["region_code"],
                    "chain": [dict(item) for item in chain_rows],
                },
                "milestones": milestones,
            })
        versions = []
        version_rows = self.connection.execute(
            "SELECT attribution_id FROM attributions WHERE outcome_key=? ORDER BY seq_no",
            (outcome_key,),
        ).fetchall()
        for vrow in version_rows:
            view = self._attribution_view(vrow["attribution_id"])
            dispute_rows = self.connection.execute(
                "SELECT d.dispute_id,d.status,d.reason,d.raised_by,d.raised_at,d.closed_by,"
                "d.closed_at,r.reviewer_id,r.decision,r.finding,r.new_attribution_id "
                "FROM disputes d LEFT JOIN reviews r ON r.dispute_id=d.dispute_id "
                "WHERE d.attribution_id=? ORDER BY d.dispute_id",
                (vrow["attribution_id"],),
            ).fetchall()
            view["disputes"] = [dict(item) for item in dispute_rows]
            versions.append(view)
        return {
            "outcome_key": outcome_key,
            "outcomes": outcomes,
            "attribution_versions": versions,
        }

    def claim_views(self, actor_id: str, program_id: str | None = None) -> dict[str, Any]:
        """还原申请主体—关联组织—最终受益人的冻结受益视图。"""

        self._require(actor_id, "report.read")
        sql = (
            "SELECT c.claim_id,c.applicant_id,c.beneficiary_id,c.program_id,c.resource_id,c.region_code "
            "FROM claims c"
        )
        params: tuple[Any, ...] = ()
        if program_id is not None:
            sql += " WHERE c.program_id=?"
            params = (program_id,)
        claim_rows = self.connection.execute(sql + " ORDER BY c.claim_id", params).fetchall()
        applicants = self._applicant_snapshots()
        beneficiaries = [
            dict(row) for row in self.connection.execute(
                "SELECT beneficiary_id,name,beneficiary_kind,region_code FROM beneficiaries"
            ).fetchall()
        ]
        organizations = [
            dict(row) for row in self.connection.execute(
                "SELECT organization_id,name,organization_kind,region_code FROM organizations"
            ).fetchall()
        ]
        claims = []
        for row in claim_rows:
            chain_rows = self.connection.execute(
                "SELECT organization_id FROM claim_chain WHERE claim_id=? ORDER BY position",
                (row["claim_id"],),
            ).fetchall()
            claims.append({
                "claim_id": row["claim_id"],
                "applicant_id": row["applicant_id"],
                "beneficiary_id": row["beneficiary_id"],
                "program_id": row["program_id"],
                "resource_id": row["resource_id"],
                "region_code": row["region_code"],
                "chain": [item["organization_id"] for item in chain_rows],
            })
        return {"views": build_claim_views(applicants, beneficiaries, organizations, claims)}
