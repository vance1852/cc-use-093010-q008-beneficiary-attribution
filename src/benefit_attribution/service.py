"""受益关系与成效归属的领域用例。

不变量：
1. 申请主体、最终受益人、关联组织、支持资源、里程碑证据、政策版本一经登记
   即冻结，服务不提供修改/删除入口；纠错只能追加新记录并重算归属。
2. 归属结果以版本链保存；证据迟到或关系变化产生新版本，已发布版本由
   数据库触发器禁止回写。
3. 每个簇的分摊份额之和严格等于簇总量（守恒），重复成果只计一处。
4. 外部回调按 callback_key 幂等，重复回调不会再次计入。
5. 异议不改写原结论，由独立复核人另起复核版本。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import SystemClock, isoformat
from .contracts import RULE_CODES, Policy, PolicyError
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .rules import ClaimView, RelationEdge, cluster_claims, split_conserved
from .storage import initialize, transaction

POLICY_VERSION_LATEST = "latest"

ROLE_PERMISSIONS = {
    "registry_editor": {
        "applicant.write", "beneficiary.write", "relation.write",
        "resource.write", "evidence.write", "claim.write",
        "callback.ingest", "dispute.raise",
    },
    "policy_admin": {"policy.publish"},
    "analyst": {"attribution.run", "report.read"},
    "reviewer": {"dispute.review"},
    "manager": {"attribution.publish", "dispute.raise", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

APPLICANT_KINDS = ("enterprise", "park", "association", "subsidiary", "public_body")
BENEFICIARY_KINDS = ("enterprise", "natural_person", "cooperative")
RELATION_KINDS = ("group_parent", "group_subsidiary", "park_tenant", "association_member", "trade_name")
RESOURCE_KINDS = ("training", "platform", "subsidy", "advisory", "data_access")
MILESTONE_KINDS = ("training_completed", "platform_online", "business_outcome", "certification", "other")


class AttributionService:
    """在单个 SQLite 连接上提供全部受益关系与成效归属操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ---- 基础辅助 -------------------------------------------------------

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

    @staticmethod
    def _decimal(value: object, path: str, *, minimum: Decimal | None = None) -> Decimal:
        if isinstance(value, bool):
            raise ValidationFailed(f"{path} 必须是数值")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationFailed(f"{path} 必须是十进制数值") from exc
        if not result.is_finite():
            raise ValidationFailed(f"{path} 必须是有限数值")
        if minimum is not None and result < minimum:
            raise ValidationFailed(f"{path} 不能小于 {minimum}")
        return result

    @staticmethod
    def _text(value: object, path: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{path} 必须是非空字符串")
        return value.strip()

    @staticmethod
    def _optional_text(value: object, path: str) -> str | None:
        if value is None:
            return None
        return AttributionService._text(value, path)

    @staticmethod
    def _choice(value: object, path: str, choices: Sequence[str]) -> str:
        text = AttributionService._text(value, path)
        if text not in choices:
            raise ValidationFailed(f"{path} 必须是 {list(choices)} 之一")
        return text

    def _mapping(self, value: object, path: str) -> Mapping[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValidationFailed(f"{path} 必须是对象")
        return value

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

    # ---- 政策版本（冻结） ----------------------------------------------

    def publish_policy(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "policy.publish")
        try:
            policy = Policy.from_dict(raw)
        except PolicyError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        digest = content_digest([raw])
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO policies(policy_id,version,title,effective_from,canonical_json,content_sha256,"
                    "published_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (policy.policy_id, policy.version, policy.title, policy.effective_from, text, digest,
                     actor_id, now),
                )
                # 旧版本元数据置位（政策行本体仍全部可读、可按任意版本查询）。
                self.connection.execute(
                    "UPDATE policies SET superseded_at=? WHERE policy_id=? AND version<>?",
                    (now, policy.policy_id, policy.version),
                )
                self._audit(
                    "policy", f"{policy.policy_id}@{policy.version}", "policy.published",
                    actor_id, {"sha256": digest, "rules": list(policy.rules)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("政策版本或内容摘要已经存在") from exc
        return {"policy_id": policy.policy_id, "version": policy.version, "sha256": digest}

    def _policy(self, policy_id: str, policy_version: int | str) -> tuple[Policy, int, str]:
        if policy_version == POLICY_VERSION_LATEST:
            row = self.connection.execute(
                "SELECT canonical_json,version,content_sha256 FROM policies "
                "WHERE policy_id=? ORDER BY version DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT canonical_json,version,content_sha256 FROM policies WHERE policy_id=? AND version=?",
                (policy_id, str(policy_version)),
            ).fetchone()
        if row is None:
            raise NotFound(f"政策版本不存在: {policy_id}@{policy_version}")
        try:
            policy = Policy.from_dict(json.loads(row["canonical_json"]))
        except PolicyError as exc:  # pragma: no cover - 入库时已校验
            raise ValidationFailed(str(exc)) from exc
        return policy, row["version"], row["content_sha256"]

    def list_policies(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT policy_id,version,title,effective_from,content_sha256,published_by,created_at,"
            "superseded_at FROM policies ORDER BY policy_id,version"
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 六类冻结主体登记 ----------------------------------------------

    def _freeze(
        self, table: str, identity_column: str, identity: str, columns: Mapping[str, Any],
        digest_payload: Mapping[str, Any],
    ) -> None:
        fields = dict(columns)
        fields[identity_column] = identity
        fields["content_sha256"] = content_digest([digest_payload])
        names = ", ".join(fields)
        placeholders = ", ".join(f":{key}" for key in fields)
        try:
            self.connection.execute(f"INSERT INTO {table}({names}) VALUES({placeholders})", fields)
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"{table} 编号或内容摘要冲突: {identity}") from exc

    def register_applicant(
        self, actor_id: str, applicant_id: str, name: str, applicant_kind: str, region_code: str,
        *, channel: str, legal_identity_code: str | None = None,
        contact_fingerprint: str | None = None, detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "applicant.write")
        name = self._text(name, "name")
        kind = self._choice(applicant_kind, "applicant_kind", APPLICANT_KINDS)
        region_code = self._text(region_code, "region_code")
        channel = self._text(channel, "channel")
        legal = self._optional_text(legal_identity_code, "legal_identity_code")
        contact = self._optional_text(contact_fingerprint, "contact_fingerprint")
        detail = self._mapping(detail, "detail")
        payload = {
            "applicant_id": applicant_id, "legal_identity_code": legal, "name": name,
            "applicant_kind": kind, "region_code": region_code, "contact_fingerprint": contact,
            "channel": channel, "detail": dict(detail),
        }
        try:
            with transaction(self.connection, immediate=True):
                self._freeze(
                    "applicants", "applicant_id", self._text(applicant_id, "applicant_id"),
                    {
                        "legal_identity_code": legal, "name": name, "applicant_kind": kind,
                        "region_code": region_code, "contact_fingerprint": contact, "channel": channel,
                        "detail_json": canonical_json(detail), "registered_by": actor_id,
                        "created_at": self._now(),
                    },
                    payload,
                )
                self._audit("applicant", applicant_id, "applicant.registered", actor_id,
                            {"kind": kind, "region_code": region_code, "channel": channel})
        except Conflict:
            raise
        return {"applicant_id": applicant_id, "name": name, "applicant_kind": kind,
                "region_code": region_code, "channel": channel}

    def register_beneficiary(
        self, actor_id: str, beneficiary_id: str, name: str, beneficiary_kind: str, region_code: str,
        *, legal_identity_code: str | None = None, detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "beneficiary.write")
        name = self._text(name, "name")
        kind = self._choice(beneficiary_kind, "beneficiary_kind", BENEFICIARY_KINDS)
        region_code = self._text(region_code, "region_code")
        legal = self._optional_text(legal_identity_code, "legal_identity_code")
        detail = self._mapping(detail, "detail")
        payload = {
            "beneficiary_id": beneficiary_id, "beneficiary_kind": kind,
            "legal_identity_code": legal, "name": name, "region_code": region_code,
            "detail": dict(detail),
        }
        try:
            with transaction(self.connection, immediate=True):
                self._freeze(
                    "beneficiaries", "beneficiary_id", self._text(beneficiary_id, "beneficiary_id"),
                    {
                        "beneficiary_kind": kind, "legal_identity_code": legal, "name": name,
                        "region_code": region_code, "detail_json": canonical_json(detail),
                        "registered_by": actor_id, "created_at": self._now(),
                    },
                    payload,
                )
                self._audit("beneficiary", beneficiary_id, "beneficiary.registered", actor_id,
                            {"kind": kind, "region_code": region_code})
        except Conflict:
            raise
        return {"beneficiary_id": beneficiary_id, "name": name, "beneficiary_kind": kind,
                "region_code": region_code}

    def register_org_relation(
        self, actor_id: str, relation_id: str, relation_kind: str, left_org_id: str, right_org_id: str,
        valid_from: str, *, valid_to: str | None = None, supersedes_relation_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "relation.write")
        relation_id = self._text(relation_id, "relation_id")
        kind = self._choice(relation_kind, "relation_kind", RELATION_KINDS)
        left = self._text(left_org_id, "left_org_id")
        right = self._text(right_org_id, "right_org_id")
        valid_from = self._text(valid_from, "valid_from")
        valid_to = self._optional_text(valid_to, "valid_to")
        if valid_to is not None and valid_to <= valid_from:
            raise ValidationFailed("valid_to 必须晚于 valid_from")
        detail = self._mapping(detail, "detail")
        if not self._org_exists(left) or not self._org_exists(right):
            raise NotFound("关联关系端点组织必须先登记为申请主体或最终受益人")
        with transaction(self.connection, immediate=True):
            prior = self.connection.execute(
                "SELECT relation_id,version FROM org_relations WHERE relation_id=?",
                (supersedes_relation_id,),
            ).fetchone() if supersedes_relation_id else None
            if supersedes_relation_id and prior is None:
                raise NotFound(f"被取代的关联关系不存在: {supersedes_relation_id}")
            if prior is not None:
                same_edge = self.connection.execute(
                    "SELECT 1 FROM org_relations WHERE relation_id=? "
                    "AND relation_kind=? AND left_org_id=? AND right_org_id=?",
                    (prior["relation_id"], kind, left, right),
                ).fetchone()
                if same_edge is None:
                    raise ValidationFailed("新版本关联关系的端点与类型必须与被取代版本一致")
                version_no = prior["version"] + 1
            else:
                version_no = self.connection.execute(
                    "SELECT count(*) FROM org_relations WHERE relation_kind=? AND left_org_id=? AND right_org_id=?",
                    (kind, left, right),
                ).fetchone()[0] + 1
            payload = {
                "relation_id": relation_id, "relation_kind": kind, "left_org_id": left,
                "right_org_id": right, "version": version_no,
                "supersedes_relation_id": supersedes_relation_id, "valid_from": valid_from,
                "valid_to": valid_to, "detail": dict(detail),
            }
            self._freeze(
                "org_relations", "relation_id", relation_id,
                {
                    "relation_kind": kind, "left_org_id": left, "right_org_id": right,
                    "version": version_no, "supersedes_relation_id": supersedes_relation_id,
                    "valid_from": valid_from, "valid_to": valid_to,
                    "detail_json": canonical_json(detail), "registered_by": actor_id,
                    "created_at": self._now(),
                },
                payload,
            )
            self._audit("org_relation", relation_id, "org_relation.registered", actor_id,
                        {"kind": kind, "left": left, "right": right, "version": version_no,
                         "supersedes": supersedes_relation_id})
        return {"relation_id": relation_id, "relation_kind": kind, "left_org_id": left,
                "right_org_id": right, "version": version_no}

    def _org_exists(self, org_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM applicants WHERE applicant_id=? "
            "UNION ALL SELECT 1 FROM beneficiaries WHERE beneficiary_id=? LIMIT 1",
            (org_id, org_id),
        ).fetchone()
        return row is not None

    def register_resource(
        self, actor_id: str, resource_id: str, resource_kind: str, program_id: str, region_code: str,
        *, batch_ref: str | None = None, detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        kind = self._choice(resource_kind, "resource_kind", RESOURCE_KINDS)
        program_id = self._text(program_id, "program_id")
        region_code = self._text(region_code, "region_code")
        batch_ref = self._optional_text(batch_ref, "batch_ref")
        detail = self._mapping(detail, "detail")
        payload = {
            "resource_id": resource_id, "resource_kind": kind, "program_id": program_id,
            "region_code": region_code, "batch_ref": batch_ref, "detail": dict(detail),
        }
        try:
            with transaction(self.connection, immediate=True):
                self._freeze(
                    "support_resources", "resource_id", self._text(resource_id, "resource_id"),
                    {
                        "resource_kind": kind, "program_id": program_id, "region_code": region_code,
                        "batch_ref": batch_ref, "detail_json": canonical_json(detail),
                        "registered_by": actor_id, "created_at": self._now(),
                    },
                    payload,
                )
                self._audit("resource", resource_id, "resource.registered", actor_id,
                            {"kind": kind, "program_id": program_id, "region_code": region_code})
        except Conflict:
            raise
        return {"resource_id": resource_id, "resource_kind": kind, "program_id": program_id,
                "region_code": region_code}

    def register_evidence(
        self, actor_id: str, evidence_id: str, milestone_kind: str, occurred_at: str,
        reporting_org: str, *, detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "evidence.write")
        kind = self._choice(milestone_kind, "milestone_kind", MILESTONE_KINDS)
        occurred_at = self._text(occurred_at, "occurred_at")
        reporting_org = self._text(reporting_org, "reporting_org")
        detail = self._mapping(detail, "detail")
        payload = {
            "evidence_id": evidence_id, "milestone_kind": kind, "occurred_at": occurred_at,
            "reporting_org": reporting_org, "detail": dict(detail),
        }
        try:
            with transaction(self.connection, immediate=True):
                self._freeze(
                    "milestone_evidence", "evidence_id", self._text(evidence_id, "evidence_id"),
                    {
                        "milestone_kind": kind, "occurred_at": occurred_at,
                        "reporting_org": reporting_org, "detail_json": canonical_json(detail),
                        "registered_by": actor_id, "created_at": self._now(),
                    },
                    payload,
                )
                self._audit("evidence", evidence_id, "evidence.registered", actor_id,
                            {"milestone_kind": kind, "reporting_org": reporting_org})
        except Conflict:
            raise
        return {"evidence_id": evidence_id, "milestone_kind": kind, "reporting_org": reporting_org}

    def register_claim(
        self, actor_id: str, claim_id: str, applicant_id: str, beneficiary_id: str, resource_id: str,
        milestone_kind: str, outcome_value: object, evidence_id: str, reported_by_org: str,
        *, attributes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "claim.write")
        fields = self._claim_fields(
            claim_id, applicant_id, beneficiary_id, resource_id, milestone_kind, outcome_value,
            evidence_id, reported_by_org, attributes,
        )
        self._require_claim_refs(fields)
        try:
            with transaction(self.connection, immediate=True):
                self._insert_claim(actor_id, fields)
                self._audit_claim(actor_id, fields)
        except Conflict:
            raise
        return {"claim_id": fields["claim_id"], "outcome_value": fields["outcome_value"], "replayed": False}

    def _claim_fields(
        self, claim_id: str, applicant_id: str, beneficiary_id: str, resource_id: str,
        milestone_kind: str, outcome_value: object, evidence_id: str, reported_by_org: str,
        attributes: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        claim_id = self._text(claim_id, "claim_id")
        applicant_id = self._text(applicant_id, "applicant_id")
        beneficiary_id = self._text(beneficiary_id, "beneficiary_id")
        resource_id = self._text(resource_id, "resource_id")
        evidence_id = self._text(evidence_id, "evidence_id")
        kind = self._choice(milestone_kind, "milestone_kind", MILESTONE_KINDS)
        value = self._decimal(outcome_value, "outcome_value", minimum=Decimal(0))
        reported_by_org = self._text(reported_by_org, "reported_by_org")
        attributes = dict(self._mapping(attributes, "attributes"))
        return {
            "claim_id": claim_id, "applicant_id": applicant_id, "beneficiary_id": beneficiary_id,
            "resource_id": resource_id, "milestone_kind": kind,
            "outcome_value": format(value, "f"), "evidence_id": evidence_id,
            "reported_by_org": reported_by_org, "attributes": attributes,
        }

    def _insert_claim(self, actor_id: str, fields: Mapping[str, Any]) -> None:
        """向 benefit_claims 插入冻结申报；调用方负责事务。"""
        payload = dict(fields)
        attributes = payload.pop("attributes")
        payload["attributes_json"] = canonical_json(attributes)
        payload["registered_by"] = actor_id
        payload["created_at"] = self._now()
        try:
            self._freeze("benefit_claims", "claim_id", payload["claim_id"], payload, dict(fields))
        except Conflict:
            raise

    def _audit_claim(self, actor_id: str, fields: Mapping[str, Any]) -> None:
        self._audit("claim", fields["claim_id"], "claim.registered", actor_id,
                    {"applicant_id": fields["applicant_id"], "beneficiary_id": fields["beneficiary_id"],
                     "resource_id": fields["resource_id"], "evidence_id": fields["evidence_id"],
                     "outcome_value": fields["outcome_value"]})

    def _require_claim_refs(self, fields: Mapping[str, Any]) -> None:
        checks = (
            ("applicants", "applicant_id", fields["applicant_id"]),
            ("beneficiaries", "beneficiary_id", fields["beneficiary_id"]),
            ("support_resources", "resource_id", fields["resource_id"]),
            ("milestone_evidence", "evidence_id", fields["evidence_id"]),
        )
        for table, column, value in checks:
            row = self.connection.execute(
                f"SELECT 1 FROM {table} WHERE {column}=?", (value,)
            ).fetchone()
            if row is None:
                raise NotFound(f"引用的{column}不存在: {value}")

    def ingest_callback(
        self, actor_id: str, scope: str, callback_key: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """幂等接收机构回调；同一 callback_key 的重复推送不会再次计入。

        单事务内先占回调键、再落申报：并发重复推送在占键处被唯一约束挡住，
        整笔回滚后按回放处理，申报绝不会被第二次插入。
        """
        self._require(actor_id, "callback.ingest")
        scope = self._text(scope, "scope")
        callback_key = self._text(callback_key, "callback_key")
        request_digest = content_digest([payload])
        fields = self._claim_fields(
            payload.get("claim_id"), payload.get("applicant_id"), payload.get("beneficiary_id"),
            payload.get("resource_id"), payload.get("milestone_kind"), payload.get("outcome_value"),
            payload.get("evidence_id"), payload.get("reported_by_org"), payload.get("attributes"),
        )
        self._require_claim_refs(fields)
        for attempt in (1, 2):
            with transaction(self.connection, immediate=True):
                existing = self.connection.execute(
                    "SELECT request_sha256,claim_id FROM processed_callbacks WHERE callback_key=?",
                    (callback_key,),
                ).fetchone()
                if existing is not None:
                    if existing["request_sha256"] != request_digest:
                        raise Conflict("同一回调键对应了不同回调内容")
                    return {"callback_key": callback_key, "claim_id": existing["claim_id"],
                            "replayed": True}
                try:
                    self.connection.execute(
                        "INSERT INTO processed_callbacks(callback_key,scope,request_sha256,claim_id,"
                        "created_at) VALUES(?,?,?,?,?)",
                        (callback_key, scope, request_digest, fields["claim_id"], self._now()),
                    )
                    self._insert_claim(actor_id, fields)
                    self._audit("callback", callback_key, "callback.ingested", actor_id,
                                {"scope": scope, "claim_id": fields["claim_id"]})
                    self._audit_claim(actor_id, fields)
                except sqlite3.IntegrityError as exc:
                    # 并发下被另一笔相同回调抢先：整笔回滚后重试，走回放分支。
                    if attempt == 1:
                        continue
                    raise Conflict("回调键或申报内容并发冲突") from exc
            return {"callback_key": callback_key, "claim_id": fields["claim_id"], "replayed": False}
        raise Conflict("回调处理异常")  # pragma: no cover

    # ---- 归属版本化计算 -------------------------------------------------

    def _load_claim_views(
        self, claim_ids: Sequence[str], policy: Policy
    ) -> list[ClaimView]:
        if not claim_ids:
            raise ValidationFailed("归属范围 claim_ids 不能为空")
        if len(set(claim_ids)) != len(claim_ids):
            raise ValidationFailed("归属范围 claim_ids 不能重复")
        placeholders = ",".join("?" for _ in claim_ids)
        rows = self.connection.execute(
            f"SELECT c.claim_id,c.applicant_id,c.beneficiary_id,c.resource_id,c.evidence_id,"
            f"c.milestone_kind,c.outcome_value,c.attributes_json,c.content_sha256 AS claim_sha,"
            f"a.legal_identity_code AS applicant_legal,a.applicant_kind,a.region_code AS applicant_region,"
            f"a.contact_fingerprint,b.legal_identity_code AS beneficiary_legal "
            f"FROM benefit_claims c JOIN applicants a ON a.applicant_id=c.applicant_id "
            f"JOIN beneficiaries b ON b.beneficiary_id=c.beneficiary_id "
            f"WHERE c.claim_id IN ({placeholders})",
            tuple(claim_ids),
        ).fetchall()
        if len(rows) != len(claim_ids):
            found = {row["claim_id"] for row in rows}
            missing = sorted(set(claim_ids) - found)
            raise NotFound(f"申报不存在: {missing}")
        views: list[ClaimView] = []
        for row in sorted(rows, key=lambda r: r["claim_id"]):
            attributes = json.loads(row["attributes_json"])
            weight = Decimal(1)
            if policy.share_strategy == "applicant_weight" and policy.applicant_weight_field:
                raw_weight = attributes.get(policy.applicant_weight_field)
                if raw_weight is not None:
                    weight = self._decimal(
                        raw_weight, f"attributes.{policy.applicant_weight_field}", minimum=Decimal(0)
                    )
            views.append(ClaimView(
                claim_id=row["claim_id"], applicant_id=row["applicant_id"],
                applicant_legal_code=row["applicant_legal"], applicant_kind=row["applicant_kind"],
                applicant_region=row["applicant_region"], contact_fingerprint=row["contact_fingerprint"],
                beneficiary_id=row["beneficiary_id"], beneficiary_legal_code=row["beneficiary_legal"],
                resource_id=row["resource_id"], evidence_id=row["evidence_id"],
                milestone_kind=row["milestone_kind"],
                outcome_value=Decimal(row["outcome_value"]), weight=weight,
            ))
        return views

    def _active_relations(self, org_ids: set[str], as_of: str) -> list[sqlite3.Row]:
        """返回基准时点生效的关联关系。

        关系变化不回写旧行，而是登记新版本；同一 (类型,左,右) 边只取
        valid_from<=as_of 的最新版本，再按其自身 valid_to 判断是否仍有效。
        """
        if not org_ids:
            return []
        rows = self.connection.execute(
            "SELECT * FROM org_relations WHERE valid_from<=? ORDER BY relation_id",
            (as_of,),
        ).fetchall()
        latest: dict[tuple[str, str, str], sqlite3.Row] = {}
        for row in rows:
            if row["left_org_id"] not in org_ids or row["right_org_id"] not in org_ids:
                continue
            key = (row["relation_kind"], row["left_org_id"], row["right_org_id"])
            current = latest.get(key)
            if current is None or row["version"] > current["version"]:
                latest[key] = row
        return [
            row for row in latest.values()
            if row["valid_to"] is None or row["valid_to"] > as_of
        ]

    def _compute(
        self, actor_id: str, attribution_id: str, policy: Policy, policy_version: int,
        policy_digest: str, claim_ids: Sequence[str], as_of: str, change_reason: str | None,
        initial_status: str, *, allow_disputed: bool = False,
    ) -> dict[str, Any]:
        """计算并追加一个归属版本（调用方已持事务或即将开事务）。"""
        views = self._load_claim_views(claim_ids, policy)
        org_ids = set()
        for view in views:
            org_ids.add(view.applicant_id)
            org_ids.add(view.beneficiary_id)
        relation_rows = self._active_relations(org_ids, as_of)
        edges = [
            RelationEdge(row["left_org_id"], row["right_org_id"], row["relation_kind"])
            for row in relation_rows
        ]
        result = cluster_claims(views, edges, policy)
        views_by_id = {view.claim_id: view for view in views}

        basis = {
            "as_of": as_of,
            "claim_ids": sorted(view.claim_id for view in views),
            "claim_sha256": {
                view.claim_id: self.connection.execute(
                    "SELECT content_sha256 FROM benefit_claims WHERE claim_id=?", (view.claim_id,)
                ).fetchone()[0]
                for view in views
            },
            "relations": [
                {"relation_id": row["relation_id"], "version": row["version"],
                 "relation_kind": row["relation_kind"], "left_org_id": row["left_org_id"],
                 "right_org_id": row["right_org_id"], "content_sha256": row["content_sha256"]}
                for row in relation_rows
            ],
        }
        fingerprint = content_digest([{
            "attribution_id": attribution_id,
            "policy_id": policy.policy_id,
            "policy_version": policy_version,
            "policy_sha256": policy_digest,
            "claims": basis["claim_ids"],
            "relations": [(item["relation_id"], item["version"]) for item in basis["relations"]],
        }])

        prior = self.connection.execute(
            "SELECT version_no,status,scope_fingerprint,policy_version,published_at "
            "FROM attribution_versions WHERE attribution_id=? ORDER BY version_no DESC LIMIT 1",
            (attribution_id,),
        ).fetchone()
        if prior is not None:
            # 未决异议优先：即使请求范围未变，也不能用普通重算掩盖复核流程。
            if prior["status"] == "disputed" and not allow_disputed:
                raise InvalidState("当前版本存在未决异议，须经独立复核形成新版本")
            if prior["scope_fingerprint"] == fingerprint:
                return {"attribution_id": attribution_id, "version_no": prior["version_no"],
                        "replayed": True, "unchanged": True}
        version_no = 1 if prior is None else prior["version_no"] + 1

        cluster_payloads: list[dict[str, Any]] = []
        conservation_check = Decimal(0)
        for cluster in result.clusters:
            members = [views_by_id[claim_id] for claim_id in cluster.claim_ids]
            relation = "unique" if len(members) == 1 else cluster.relation
            total, allocation, notes = split_conserved(
                members, relation, policy.share_strategy
            )
            allocated = sum((item[1] for item in allocation), Decimal(0))
            if allocated != total:  # pragma: no cover - 规则层已保证
                raise InvalidState(f"簇 {cluster.key} 分摊不守恒: {allocated} != {total}")
            conservation_check += allocated
            rule_codes = sorted({hit.rule_code for hit in cluster.rule_hits})
            explanations = [
                {"code": code, "description": RULE_CODES[code],
                 "merges": [
                     {"rule_code": hit.rule_code, "claim_a": hit.claim_a, "claim_b": hit.claim_b,
                      "via_org": hit.via_org}
                     for hit in cluster.rule_hits if hit.rule_code == code
                 ]}
                for code in rule_codes
            ]
            cluster_payloads.append({
                "cluster_key": cluster.key, "relation": relation,
                "suspected_duplicate": len(members) > 1 and cluster.suspected_duplicate,
                "matched_rules": explanations, "total_value": format(total, "f"),
                "notes": notes,
                "shares": [
                    {"claim_id": item.claim_id, "share_value": format(share, "f"),
                     "share_weight": format(weight, "f")}
                    for item, share, weight in allocation
                ],
            })

        basis_json = canonical_json({"basis": basis, "clusters": cluster_payloads})
        now = self._now()
        self.connection.execute(
            "INSERT INTO attribution_versions(attribution_id,version_no,scope_fingerprint,policy_id,"
            "policy_version,policy_sha256,basis_json,status,change_reason,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (attribution_id, version_no, fingerprint, policy.policy_id, policy_version, policy_digest,
             basis_json, initial_status, change_reason, actor_id, now),
        )
        for payload in cluster_payloads:
            matched = ",".join(item["code"] for item in payload["matched_rules"]) or None
            self.connection.execute(
                "INSERT INTO attribution_clusters(attribution_id,version_no,cluster_key,relation,"
                "matched_rule,total_value) VALUES(?,?,?,?,?,?)",
                (attribution_id, version_no, payload["cluster_key"], payload["relation"], matched,
                 payload["total_value"]),
            )
            for share in payload["shares"]:
                self.connection.execute(
                    "INSERT INTO attribution_shares(attribution_id,version_no,cluster_key,claim_id,"
                    "share_value,share_weight) VALUES(?,?,?,?,?,?)",
                    (attribution_id, version_no, payload["cluster_key"], share["claim_id"],
                     share["share_value"], share["share_weight"]),
                )
        # 上一版本处置：已发布版本永不回写（原结论对外可查），只追加新版本；
        # 未发布版本统一置为 superseded，新版本的 revised 状态与 change_reason
        # 会标明它来自异议复核纠正。
        if prior is not None and prior["published_at"] is None and prior["status"] in {"effective", "disputed"}:
            updated = self.connection.execute(
                "UPDATE attribution_versions SET status='superseded' "
                "WHERE attribution_id=? AND version_no=? AND published_at IS NULL",
                (attribution_id, prior["version_no"]),
            )
            if updated.rowcount != 1:  # pragma: no cover - 行内并发保护
                raise InvalidState("上一版本状态已变化，需经复核流程处理")
        self._audit("attribution", f"{attribution_id}@{version_no}", "attribution.computed",
                    actor_id, {"policy": f"{policy.policy_id}@{policy_version}",
                               "cluster_count": len(cluster_payloads),
                               "conservation_total": format(conservation_check, "f"),
                               "change_reason": change_reason})
        return {"attribution_id": attribution_id, "version_no": version_no, "replayed": False,
                "clusters": cluster_payloads, "conservation_total": format(conservation_check, "f")}

    def run_attribution(
        self, actor_id: str, attribution_id: str, policy_id: str, claim_ids: Sequence[str],
        *, policy_version: int | str = POLICY_VERSION_LATEST, as_of: str | None = None,
        change_reason: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "attribution.run")
        attribution_id = self._text(attribution_id, "attribution_id")
        change_reason = self._optional_text(change_reason, "change_reason")
        as_of = self._optional_text(as_of, "as_of") or self._now()
        policy, resolved_version, policy_digest = self._policy(policy_id, policy_version)
        with transaction(self.connection, immediate=True):
            return self._compute(
                actor_id, attribution_id, policy, resolved_version, policy_digest,
                list(claim_ids), as_of, change_reason, "effective",
            )

    def publish_attribution(self, actor_id: str, attribution_id: str, version_no: int) -> dict[str, Any]:
        """对外发布：发布后该版本冻结，任何回写都会被数据库拒绝。"""
        self._require(actor_id, "attribution.publish")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT version_no,status,published_at FROM attribution_versions "
                "WHERE attribution_id=? AND version_no=?",
                (attribution_id, version_no),
            ).fetchone()
            if row is None:
                raise NotFound("归属版本不存在")
            if row["published_at"] is not None:
                return {"attribution_id": attribution_id, "version_no": version_no, "replayed": True}
            if row["status"] == "disputed":
                raise InvalidState("存在未决异议的版本不能发布")
            now = self._now()
            self.connection.execute(
                "UPDATE attribution_versions SET published_at=? WHERE attribution_id=? AND version_no=?",
                (now, attribution_id, version_no),
            )
            self._audit("attribution", f"{attribution_id}@{version_no}", "attribution.published",
                        actor_id, {"published_at": now})
        return {"attribution_id": attribution_id, "version_no": version_no, "published_at": now}

    # ---- 异议与独立复核 -------------------------------------------------

    def raise_dispute(
        self, actor_id: str, attribution_id: str, version_no: int, reason: str,
        *, cluster_key: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "dispute.raise")
        reason = self._text(reason, "reason")
        version = self.connection.execute(
            "SELECT 1 FROM attribution_versions WHERE attribution_id=? AND version_no=?",
            (attribution_id, version_no),
        ).fetchone()
        if version is None:
            raise NotFound("归属版本不存在")
        if cluster_key is not None:
            exists = self.connection.execute(
                "SELECT 1 FROM attribution_clusters WHERE attribution_id=? AND version_no=? AND cluster_key=?",
                (attribution_id, version_no, cluster_key),
            ).fetchone()
            if exists is None:
                raise NotFound("异议指向的归属簇不存在")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO disputes(attribution_id,version_no,cluster_key,reason,raised_by,raised_at,"
                    "status) VALUES(?,?,?,?,?,?,'open')",
                    (attribution_id, version_no, cluster_key, reason, actor_id, self._now()),
                )
                dispute_id = cursor.lastrowid
                # 未发布版本用状态字段标记；已发布版本行触发器禁止更新，异议状态以 disputes 表为准。
                self.connection.execute(
                    "UPDATE attribution_versions SET status='disputed' "
                    "WHERE attribution_id=? AND version_no=? AND published_at IS NULL AND status='effective'",
                    (attribution_id, version_no),
                )
                self._audit("dispute", str(dispute_id), "dispute.raised", actor_id,
                            {"attribution_id": attribution_id, "version_no": version_no,
                             "cluster_key": cluster_key, "reason": reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该版本/簇已有未决异议") from exc
        return {"dispute_id": dispute_id, "status": "open",
                "attribution_id": attribution_id, "version_no": version_no}

    def start_review(self, actor_id: str, dispute_id: int) -> dict[str, Any]:
        self._require(actor_id, "dispute.review")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if row is None:
                raise NotFound("异议不存在")
            if row["status"] != "open":
                raise InvalidState("异议不在待受理状态")
            if row["raised_by"] == actor_id:
                raise Forbidden("异议发起人不能独立复核自己的异议")
            self.connection.execute(
                "UPDATE disputes SET status='review',reviewer_id=?,review_started_at=? WHERE dispute_id=?",
                (actor_id, self._now(), dispute_id),
            )
            self._audit("dispute", str(dispute_id), "dispute.review_started", actor_id, {})
        return {"dispute_id": dispute_id, "status": "review"}

    def resolve_dispute(
        self, actor_id: str, dispute_id: int, uphold: bool, determination_note: str,
        *, policy_id: str | None = None, policy_version: int | str | None = None,
        claim_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "dispute.review")
        note = self._text(determination_note, "determination_note")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if row is None:
                raise NotFound("异议不存在")
            if row["status"] != "review":
                raise InvalidState("异议不在复核中状态")
            if row["reviewer_id"] != actor_id:
                raise Forbidden("只有受理复核人可以出具结论")
            now = self._now()
            if not uphold:
                self.connection.execute(
                    "UPDATE disputes SET status='rejected',closed_at=?,determination_note=? WHERE dispute_id=?",
                    (now, note, dispute_id),
                )
                # 未发布版本从 disputed 恢复为 effective；已发布版本状态本就未被改写。
                self.connection.execute(
                    "UPDATE attribution_versions SET status='effective' "
                    "WHERE attribution_id=? AND version_no=? AND published_at IS NULL AND status='disputed'",
                    (row["attribution_id"], row["version_no"]),
                )
                self._audit("dispute", str(dispute_id), "dispute.rejected", actor_id, {"note": note})
                return {"dispute_id": dispute_id, "status": "rejected", "original_conclusion": "unchanged"}

            version = self.connection.execute(
                "SELECT policy_id,policy_version,basis_json FROM attribution_versions "
                "WHERE attribution_id=? AND version_no=?",
                (row["attribution_id"], row["version_no"]),
            ).fetchone()
            target_policy_id = policy_id or version["policy_id"]
            target_policy_version = policy_version if policy_version is not None else version["policy_version"]
            policy, resolved_version, digest = self._policy(target_policy_id, target_policy_version)
            if claim_ids is None:
                claim_ids = json.loads(version["basis_json"])["basis"]["claim_ids"]
            new_version = self._compute(
                actor_id, row["attribution_id"], policy, resolved_version, digest, list(claim_ids),
                self._now(), f"异议 #{dispute_id} 成立：{note}", "revised", allow_disputed=True,
            )
            if new_version.get("unchanged"):
                raise InvalidState("复核依据与原版本完全一致，无法形成纠正版本；应驳回异议")
            self.connection.execute(
                "UPDATE disputes SET status='upheld',closed_at=?,determination_note=?,"
                "new_attribution_id=?,new_version_no=? WHERE dispute_id=?",
                (now, note, row["attribution_id"], new_version["version_no"], dispute_id),
            )
            self._audit("dispute", str(dispute_id), "dispute.upheld", actor_id,
                        {"note": note, "new_version": new_version["version_no"]})
            return {"dispute_id": dispute_id, "status": "upheld",
                    "new_version_no": new_version["version_no"],
                    "original_preserved": {"attribution_id": row["attribution_id"],
                                           "version_no": row["version_no"]}}

    # ---- 管理视图 -------------------------------------------------------

    def _require_report(self, actor_id: str) -> None:
        self._require(actor_id, "report.read")

    def _current_versions(self, policy_id: str, policy_version: int, as_of: str | None) -> list[sqlite3.Row]:
        """每个归属在指定政策版本下的当前版本（跳过未决争议版本）。"""
        conditions = ["policy_id=?", "policy_version=?"]
        params: list[Any] = [policy_id, policy_version]
        if as_of is not None:
            conditions.append("created_at<=?")
            params.append(as_of)
        where = " AND ".join(conditions)
        rows = self.connection.execute(
            f"SELECT attribution_id,MAX(version_no) AS version_no FROM attribution_versions "
            f"WHERE {where} AND status<>'disputed' GROUP BY attribution_id",
            tuple(params),
        ).fetchall()
        result = []
        for row in rows:
            full = self.connection.execute(
                "SELECT * FROM attribution_versions WHERE attribution_id=? AND version_no=?",
                (row["attribution_id"], row["version_no"]),
            ).fetchone()
            result.append(full)
        return result

    def coverage(
        self, actor_id: str, policy_id: str, policy_version: int | str = POLICY_VERSION_LATEST,
        *, as_of: str | None = None, region_code: str | None = None,
    ) -> dict[str, Any]:
        """按任意政策版本查看覆盖地区与有效受益者（份额大于零的最终受益人）。"""
        self._require_report(actor_id)
        _, resolved_version, _ = self._policy(policy_id, policy_version)
        as_of = self._optional_text(as_of, "as_of")
        region_code = self._optional_text(region_code, "region_code")
        versions = self._current_versions(policy_id, resolved_version, as_of)
        per_region: dict[str, set[str]] = {}
        for version in versions:
            rows = self.connection.execute(
                "SELECT DISTINCT b.beneficiary_id,b.region_code FROM attribution_shares s "
                "JOIN benefit_claims c ON c.claim_id=s.claim_id "
                "JOIN beneficiaries b ON b.beneficiary_id=c.beneficiary_id "
                "WHERE s.attribution_id=? AND s.version_no=? AND CAST(s.share_value AS REAL)>0",
                (version["attribution_id"], version["version_no"]),
            ).fetchall()
            for row in rows:
                if region_code is not None and row["region_code"] != region_code:
                    continue
                per_region.setdefault(row["region_code"], set()).add(row["beneficiary_id"])
        return {
            "policy_id": policy_id,
            "policy_version": resolved_version,
            "as_of": as_of,
            "regions": [
                {"region_code": code, "effective_beneficiaries": sorted(ids)}
                for code, ids in sorted(per_region.items())
            ],
            "covered_region_count": len(per_region),
            "effective_beneficiary_count": len(set().union(*per_region.values()) if per_region else set()),
        }

    def open_disputes(self, actor_id: str) -> list[dict[str, Any]]:
        self._require_report(actor_id)
        rows = self.connection.execute(
            "SELECT dispute_id,attribution_id,version_no,cluster_key,reason,raised_by,raised_at,"
            "status,reviewer_id,review_started_at FROM disputes "
            "WHERE status IN ('open','review') ORDER BY dispute_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def dashboard(
        self, actor_id: str, policy_id: str, policy_version: int | str = POLICY_VERSION_LATEST,
        *, as_of: str | None = None,
    ) -> dict[str, Any]:
        coverage_report = self.coverage(actor_id, policy_id, policy_version, as_of=as_of)
        disputes = self.open_disputes(actor_id)
        attributions = self.connection.execute(
            "SELECT attribution_id,MAX(version_no) AS latest_version,COUNT(*) AS version_count,"
            "SUM(CASE WHEN published_at IS NOT NULL THEN 1 ELSE 0 END) AS published_versions "
            "FROM attribution_versions WHERE policy_id=? GROUP BY attribution_id ORDER BY attribution_id",
            (policy_id,),
        ).fetchall()
        return {
            **coverage_report,
            "open_dispute_count": len(disputes),
            "open_disputes": disputes,
            "attributions": [dict(row) for row in attributions],
        }

    def list_versions(self, actor_id: str, attribution_id: str) -> list[dict[str, Any]]:
        self._require_report(actor_id)
        rows = self.connection.execute(
            "SELECT attribution_id,version_no,policy_id,policy_version,status,change_reason,created_by,"
            "created_at,published_at FROM attribution_versions WHERE attribution_id=? ORDER BY version_no",
            (attribution_id,),
        ).fetchall()
        if not rows:
            raise NotFound("归属记录不存在")
        return [dict(row) for row in rows]

    def source_chain(
        self, actor_id: str, attribution_id: str, version_no: int | None = None
    ) -> dict[str, Any]:
        """每项成果的完整来源链：政策→关系→申报→资源/证据→份额→异议→版本链。"""
        self._require_report(actor_id)
        if version_no is None:
            row = self.connection.execute(
                "SELECT * FROM attribution_versions WHERE attribution_id=? ORDER BY version_no DESC LIMIT 1",
                (attribution_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM attribution_versions WHERE attribution_id=? AND version_no=?",
                (attribution_id, version_no),
            ).fetchone()
        if row is None:
            raise NotFound("归属版本不存在")
        basis_doc = json.loads(row["basis_json"])
        chain_clusters: list[dict[str, Any]] = []
        for cluster in basis_doc["clusters"]:
            claim_lines = []
            for share in cluster["shares"]:
                claim = self.connection.execute(
                    "SELECT c.*,a.name AS applicant_name,a.applicant_kind,a.channel,a.region_code AS "
                    "applicant_region,b.name AS beneficiary_name,b.beneficiary_kind,b.region_code AS "
                    "beneficiary_region,r.resource_kind,r.program_id,r.batch_ref,e.milestone_kind,"
                    "e.occurred_at,e.reporting_org,e.content_sha256 AS evidence_sha256 "
                    "FROM benefit_claims c JOIN applicants a ON a.applicant_id=c.applicant_id "
                    "JOIN beneficiaries b ON b.beneficiary_id=c.beneficiary_id "
                    "JOIN support_resources r ON r.resource_id=c.resource_id "
                    "JOIN milestone_evidence e ON e.evidence_id=c.evidence_id "
                    "WHERE c.claim_id=?",
                    (share["claim_id"],),
                ).fetchone()
                claim_lines.append({
                    "claim_id": share["claim_id"],
                    "share_value": share["share_value"],
                    "share_weight": share["share_weight"],
                    "applicant": {
                        "applicant_id": claim["applicant_id"], "name": claim["applicant_name"],
                        "kind": claim["applicant_kind"], "channel": claim["channel"],
                        "region_code": claim["applicant_region"],
                    },
                    "beneficiary": {
                        "beneficiary_id": claim["beneficiary_id"], "name": claim["beneficiary_name"],
                        "kind": claim["beneficiary_kind"], "region_code": claim["beneficiary_region"],
                    },
                    "resource": {
                        "resource_id": claim["resource_id"], "kind": claim["resource_kind"],
                        "program_id": claim["program_id"], "batch_ref": claim["batch_ref"],
                    },
                    "evidence": {
                        "evidence_id": claim["evidence_id"], "milestone_kind": claim["milestone_kind"],
                        "occurred_at": claim["occurred_at"], "reporting_org": claim["reporting_org"],
                        "content_sha256": claim["evidence_sha256"],
                    },
                    "reported_by_org": claim["reported_by_org"],
                    "outcome_value": claim["outcome_value"],
                })
            chain_clusters.append({
                "cluster_key": cluster["cluster_key"],
                "relation": cluster["relation"],
                "total_value": cluster["total_value"],
                "matched_rules": cluster["matched_rules"],
                "notes": cluster["notes"],
                "claims": claim_lines,
            })
        disputes = self.connection.execute(
            "SELECT dispute_id,cluster_key,status,reason,raised_by,raised_at,reviewer_id,closed_at,"
            "determination_note,new_version_no FROM disputes "
            "WHERE attribution_id=? AND version_no=? ORDER BY dispute_id",
            (attribution_id, row["version_no"]),
        ).fetchall()
        versions = [
            {key: version_row[key] for key in
             ("version_no", "policy_id", "policy_version", "status", "change_reason", "created_by",
              "created_at", "published_at")}
            for version_row in self.connection.execute(
                "SELECT version_no,policy_id,policy_version,status,change_reason,created_by,created_at,"
                "published_at FROM attribution_versions WHERE attribution_id=? ORDER BY version_no",
                (attribution_id,),
            ).fetchall()
        ]
        escaped_id = attribution_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='attribution' AND (entity_id=? OR entity_id LIKE ? ESCAPE '\\') "
            "ORDER BY event_id",
            (f"{attribution_id}@{row['version_no']}", f"{escaped_id}@%"),
        ).fetchall()
        return {
            "attribution_id": attribution_id,
            "version_no": row["version_no"],
            "status": row["status"],
            "published": row["published_at"] is not None,
            "published_at": row["published_at"],
            "policy": {"policy_id": row["policy_id"], "policy_version": row["policy_version"],
                       "content_sha256": row["policy_sha256"]},
            "basis": basis_doc["basis"],
            "clusters": chain_clusters,
            "disputes": [dict(item) for item in disputes],
            "version_chain": versions,
            "events": [dict(item) | {"payload": json.loads(item["payload_json"])} for item in events],
        }
