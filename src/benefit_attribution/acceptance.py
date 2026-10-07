"""受益关系与成效归属的完整产品流程离线验收。

场景：同一家小企业通过园区、协会和子公司在不同人工智能支持计划中被重复
计入；培训完成、平台上线、业务改善由不同机构报送。验收演示：
- 六类主体冻结登记；重复回调不会再次计入；
- 可解释规则识别重复与共享成果，分摊总量守恒；
- 证据迟到只追加新归属版本，已发布结果触发器级禁止回写；
- 异议保留原结论并由独立复核人形成复核版本；
- 管理者按任意政策版本查看覆盖地区、有效受益者、未决争议与来源链。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from .service import AttributionService
from .storage import connect, inspect_schema


POLICY_V1 = {
    "policy_id": "ai-attribution",
    "version": 1,
    "title": "人工智能支持计划成效归属规则（首期）",
    "effective_from": "2026-01-01T00:00:00Z",
    "rules": [
        "LEGAL_IDENTITY", "GROUP_SUBSIDIARY", "PARK_TENANT", "ASSOCIATION_MEMBER",
        "SHARED_RESOURCE", "SAME_EVIDENCE",
    ],
    "share_strategy": "equal",
}

POLICY_V2 = {
    "policy_id": "ai-attribution",
    "version": 2,
    "title": "人工智能支持计划成效归属规则（权重修订）",
    "effective_from": "2026-07-01T00:00:00Z",
    "rules": [
        "LEGAL_IDENTITY", "GROUP_SUBSIDIARY", "PARK_TENANT", "ASSOCIATION_MEMBER",
        "SHARED_RESOURCE", "SAME_EVIDENCE", "CONTACT_FINGERPRINT",
    ],
    "share_strategy": "applicant_weight",
    "applicant_weight_field": "contribution_weight",
}


def _seed(service: AttributionService) -> None:
    service.create_user("editor", "资料登记员", "registry_editor")
    service.create_user("padmin", "政策管理员", "policy_admin")
    service.create_user("analyst", "归属分析师", "analyst")
    service.create_user("boss", "项目管理机构负责人", "manager")
    service.create_user("reviewer", "独立复核人", "reviewer")
    service.create_user("auditor", "审计人员", "auditor")

    # 最终受益人：同一家小企业。
    service.register_beneficiary(
        "editor", "b-smart", "慧通科技有限公司", "enterprise", "BJ",
        legal_identity_code="91110000SMART0001X",
    )
    # 申请主体：园区、协会、子公司、直营渠道——同一受益人背后的多副面孔。
    service.register_applicant(
        "editor", "a-park", "中关村某产业园", "park", "BJ", channel="park_program",
        contact_fingerprint="fp-hotline-001",
    )
    service.register_applicant(
        "editor", "a-assoc", "数字贸易协会", "association", "BJ", channel="association_program",
        contact_fingerprint="fp-hotline-001",
    )
    service.register_applicant(
        "editor", "a-sub", "慧通南方子公司", "subsidiary", "GD", channel="subsidiary_program",
        legal_identity_code="91110000SMART0001X",
    )
    service.register_applicant(
        "editor", "a-direct", "慧通科技直营窗口", "enterprise", "BJ", channel="direct_program",
        legal_identity_code="91110000SMART0001X",
    )
    # 关联组织关系（冻结快照，含生效时间）。
    service.register_org_relation(
        "editor", "r-park-tenant", "park_tenant", "a-park", "b-smart", "2026-01-01T00:00:00Z",
    )
    service.register_org_relation(
        "editor", "r-assoc-member", "association_member", "a-assoc", "b-smart", "2026-01-01T00:00:00Z",
    )
    service.register_org_relation(
        "editor", "r-group", "group_parent", "a-direct", "a-sub", "2026-01-01T00:00:00Z",
    )
    # 支持资源：培训名额、平台上线支持、改善咨询批次。
    service.register_resource(
        "editor", "res-training", "training", "ai-skill-2026", "BJ", batch_ref="T-2026-01",
    )
    service.register_resource(
        "editor", "res-platform", "platform", "ai-platform-2026", "GD", batch_ref="P-2026-07",
    )
    service.register_resource(
        "editor", "res-outcome", "advisory", "ai-growth-2026", "BJ", batch_ref="G-2026-09",
    )
    # 里程碑证据：培训机构、平台运营机构、协会调查各自报送。
    service.register_evidence(
        "editor", "e-training", "training_completed", "2026-03-10T09:00:00Z", "培训机构",
    )
    service.register_evidence(
        "editor", "e-platform", "platform_online", "2026-08-01T10:00:00Z", "平台运营机构",
    )
    service.register_evidence(
        "editor", "e-platform-park", "platform_online", "2026-08-02T10:00:00Z", "园区统计员",
    )
    service.register_evidence(
        "editor", "e-outcome", "business_outcome", "2026-09-20T00:00:00Z", "协会调查组",
    )
    service.register_evidence(
        "editor", "e-outcome-late", "business_outcome", "2026-09-25T00:00:00Z", "园区统计员",
    )


def _claims_v1(service: AttributionService) -> None:
    # 培训完成：园区与协会引用同一份结业证据，各报 1 人——重复。
    service.register_claim(
        "editor", "c-train-park", "a-park", "b-smart", "res-training", "training_completed", 1,
        "e-training", "园区",
    )
    service.register_claim(
        "editor", "c-train-assoc", "a-assoc", "b-smart", "res-training", "training_completed", 1,
        "e-training", "协会",
    )
    # 平台上线：子公司与直营窗口同一法人（不同机构、不同证据）就同一资源报送——重复。
    service.register_claim(
        "editor", "c-platform-sub", "a-sub", "b-smart", "res-platform", "platform_online", 1,
        "e-platform", "平台运营机构",
    )
    service.register_claim(
        "editor", "c-platform-direct", "a-direct", "b-smart", "res-platform", "platform_online", 1,
        "e-platform-park", "园区统计员",
    )
    # 业务改善由协会调查系统通过回调首次报送（见 run 中的幂等演示）。


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="benefit-attribution-") as temporary:
        database = Path(temporary) / "attribution.sqlite3"
        connection = connect(database)
        try:
            service = AttributionService(connection)
            _seed(service)
            service.publish_policy("padmin", POLICY_V1)
            service.publish_policy("padmin", POLICY_V2)
            _claims_v1(service)

            # 业务改善的回调被两个系统重复推送：第二次回放，不再计入。
            callback_payload = {
                "claim_id": "c-outcome-assoc", "applicant_id": "a-assoc",
                "beneficiary_id": "b-smart", "resource_id": "res-outcome",
                "milestone_kind": "business_outcome", "outcome_value": 30000,
                "evidence_id": "e-outcome", "reported_by_org": "协会调查组",
                "attributes": {"contribution_weight": 3},
            }
            first = service.ingest_callback("editor", "growth", "cb-2026-0007", callback_payload)
            second = service.ingest_callback("editor", "growth", "cb-2026-0007", callback_payload)
            assert first["replayed"] is False and second["replayed"] is True
            claim_count_after_replay = connection.execute(
                "SELECT count(*) FROM benefit_claims"
            ).fetchone()[0]
            assert claim_count_after_replay == 5

            # 首期归属：锚定 v1 政策（之后可以按任意版本回看这一结论）。
            run1 = service.run_attribution(
                "analyst", "attr-2026", "ai-attribution",
                ["c-train-park", "c-train-assoc", "c-platform-sub", "c-platform-direct",
                 "c-outcome-assoc"],
                policy_version=1,
                change_reason="首轮成效归集",
            )
            assert run1["version_no"] == 1
            assert run1["conservation_total"] == "30002.00"
            relations = {c["relation"] for c in run1["clusters"]}
            assert relations == {"duplicate", "unique"}

            published = service.publish_attribution("boss", "attr-2026", 1)
            assert published["published_at"]

            # 已发布版本：直接回写被数据库拒绝。
            try:
                connection.execute(
                    "UPDATE attribution_versions SET status='superseded' "
                    "WHERE attribution_id='attr-2026' AND version_no=1"
                )
                raise RuntimeError("已发布归属版本竟然可以回写")
            except sqlite3.IntegrityError:
                connection.rollback()

            # 证据迟到：园区补报同一改善批次的另一份调查（共享资源），
            # 只形成 v2 归属版本；v1 原结论原样保留。
            service.register_claim(
                "editor", "c-outcome-park", "a-park", "b-smart", "res-outcome",
                "business_outcome", 30000, "e-outcome-late", "园区统计员",
                attributes={"contribution_weight": 1},
            )
            run2 = service.run_attribution(
                "analyst", "attr-2026", "ai-attribution",
                ["c-train-park", "c-train-assoc", "c-platform-sub", "c-platform-direct",
                 "c-outcome-assoc", "c-outcome-park"],
                policy_version=1,
                change_reason="园区补充调查证据迟到",
            )
            assert run2["version_no"] == 2
            shared = [c for c in run2["clusters"] if c["relation"] == "shared"]
            assert len(shared) == 1 and shared[0]["total_value"] == "30000.00"
            assert sum(Decimal_like(s["share_value"]) for s in shared[0]["shares"]) == 30000

            v1_row = connection.execute(
                "SELECT status,published_at FROM attribution_versions "
                "WHERE attribution_id='attr-2026' AND version_no=1"
            ).fetchone()
            assert v1_row["status"] == "effective" and v1_row["published_at"]

            # 利益相关方对共享分摊提出异议：原结论保留，进入独立复核。
            dispute = service.raise_dispute(
                "boss", "attr-2026", 2, "园区认为等权分摊不符合实际贡献",
                cluster_key=shared[0]["cluster_key"],
            )
            try:
                service.start_review("boss", dispute["dispute_id"])
                raise RuntimeError("异议发起人竟然可以复核自己的异议")
            except Exception as exc:
                assert getattr(exc, "code", "") == "forbidden"
            service.start_review("reviewer", dispute["dispute_id"])
            resolution = service.resolve_dispute(
                "reviewer", dispute["dispute_id"], True,
                "改用 v2 权重规则，按实际贡献 3:1 分摊",
                policy_id="ai-attribution", policy_version=2,
                claim_ids=["c-train-park", "c-train-assoc", "c-platform-sub", "c-platform-direct",
                           "c-outcome-assoc", "c-outcome-park"],
            )
            assert resolution["status"] == "upheld"
            assert resolution["original_preserved"]["version_no"] == 2
            v3_no = resolution["new_version_no"]

            # 管理视图：按任意政策版本查看覆盖与有效受益者。
            coverage_v1 = service.coverage("boss", "ai-attribution", 1)
            coverage_v2 = service.coverage("boss", "ai-attribution", 2)
            assert coverage_v1["covered_region_count"] >= 1
            assert coverage_v1["effective_beneficiary_count"] == 1
            assert coverage_v2["regions"][0]["effective_beneficiaries"] == ["b-smart"]

            open_disputes = service.open_disputes("auditor")
            assert open_disputes == []  # 异议已闭环

            chain = service.source_chain("auditor", "attr-2026", v3_no)
            assert chain["policy"]["policy_version"] == 2
            assert chain["published"] is False
            assert [v["version_no"] for v in chain["version_chain"]] == [1, 2, 3]
            weighted = [c for c in chain["clusters"] if c["relation"] == "shared"][0]
            shares = {line["claim_id"]: line["share_value"] for line in weighted["claims"]}
            assert shares == {"c-outcome-assoc": "22500.00", "c-outcome-park": "7500.00"}
            matched_codes = {rule["code"] for rule in weighted["matched_rules"]}
            assert {"ASSOCIATION_MEMBER", "PARK_TENANT", "SHARED_RESOURCE"} <= matched_codes
            # 来源链可回溯到冻结的资源与证据摘要。
            sample = weighted["claims"][0]
            assert len(sample["evidence"]["content_sha256"]) == 64
            assert sample["resource"]["batch_ref"] == "G-2026-09"

            dashboard = service.dashboard("boss", "ai-attribution", 2)
            assert dashboard["open_dispute_count"] == 0
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "policies": ["ai-attribution@1", "ai-attribution@2"],
        "attribution_versions": [v["version_no"] for v in chain["version_chain"]],
        "published_version_preserved": True,
        "duplicate_callback_replayed": True,
        "final_shared_split": shares,
        "coverage_v1_regions": coverage_v1["covered_region_count"],
        "effective_beneficiaries": coverage_v2["effective_beneficiary_count"],
        "open_disputes": 0,
        "schema": schema,
    }


def Decimal_like(text: str):  # 验收内部小工具，避免文件顶部额外导出
    from decimal import Decimal
    return Decimal(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行受益关系与成效归属服务的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
