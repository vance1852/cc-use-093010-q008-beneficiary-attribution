"""受益关系与成效归属完整产品流程的离线验收入口。

叙事：同一家小企业“星河智造”通过园区、协会和子公司在多个人工智能支持项目中
被报送——培训完成、平台上线、业务改善来自不同机构。验收脚本验证：
R1 硬标识去重、R2 关联共享、守恒分摊、重复回调不计、发布锁定、迟到证据
形成新版本、异议保留原结论并独立复核，以及按任意政策版本查看覆盖与来源链。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .clock import FrozenClock
from .jsonio import load_json
from .service import AttributionService
from .storage import connect, inspect_schema


SHA = "a" * 64


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    policy_v1 = load_json(fixtures / "demo_attribution_policy_v1.json")
    policy_v2 = load_json(fixtures / "demo_attribution_policy_v2.json")

    with tempfile.TemporaryDirectory(prefix="benefit-attribution-") as temporary:
        database = Path(temporary) / "attribution.sqlite3"
        connection = connect(database)
        try:
            clock = FrozenClock(datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc))
            service = AttributionService(connection, clock)

            service.create_user("steward-1", "登记员", "steward")
            service.create_user("analyst-1", "归属分析师", "analyst")
            service.create_user("reviewer-1", "独立复核人", "reviewer")
            service.create_user("auditor-1", "审计人员", "auditor")

            service.publish_policy("analyst-1", policy_v1)
            service.publish_policy("analyst-1", policy_v2)

            # 冻结关联组织：园区、协会、子公司集团。
            service.register_organization("steward-1", "park-x", "星河数字贸易园区", "park", "330100")
            service.register_organization("steward-1", "assoc-x", "跨境数字服务协会", "association", "330000")

            # 冻结申请主体：ent-a 与 ent-b 共用营业执照（同一实际主体），
            # ent-c 是子公司，ent-e 仅名称地址疑似。
            service.register_applicant(
                "steward-1", "ent-a", "星河智造（杭州）有限公司", "enterprise", "330100",
                identities=[{"identity_type": "business_license", "value": "LIC-XINGHE-001"}],
                weak={"address": "杭州文三路100号"},
            )
            service.register_applicant(
                "steward-1", "ent-b", "星河智造杭州经营部", "enterprise", "330100",
                identities=[{"identity_type": "business_license", "value": "LIC-XINGHE-001"}],
                weak={"address": "杭州文三路100号"},
            )
            service.register_applicant(
                "steward-1", "ent-c", "星河智造（义乌）有限公司", "enterprise", "330782",
                identities=[{"identity_type": "business_license", "value": "LIC-YIWU-009"}],
                weak={"address": "义乌商贸城8区"},
            )
            service.register_applicant(
                "steward-1", "ent-e", "星河智造工作室", "enterprise", "330100",
                identities=[{"identity_type": "business_license", "value": "LIC-STUDIO-7"}],
                weak={"address": "杭州文三路100号"},
            )

            # 关联关系：ent-c 是 ent-a 的子公司；ent-b 通过协会报送。
            # 园区与协会的受益传导在申请链路 claim_chain 中冻结。
            service.register_relation(
                "steward-1", "rel-subsidiary", "ent-a", "ent-c", "subsidiary", "2026-01-01"
            )
            service.register_relation(
                "steward-1", "rel-assoc", "ent-b", "ent-a", "association_member", "2026-01-01",
                organization_id="assoc-x",
            )

            # 最终受益人：ben-a 是星河智造本体，ben-c 是义乌子公司招用的商户群体。
            service.register_beneficiary(
                "steward-1", "ben-a", "星河智造（杭州）有限公司", "enterprise", "330100"
            )
            service.register_beneficiary(
                "steward-1", "ben-c", "义乌星河商户组", "micro_business", "330782"
            )

            # 支持资源与申请（含受益传导链）。
            service.register_resource(
                "steward-1", "res-park", "ai-2026", "training", "330100",
                detail={"name": "园区AI应用培训"},
            )
            service.register_resource(
                "steward-1", "res-assoc", "ai-2026", "platform", "330000",
                detail={"name": "协会撮合平台席位"},
            )
            service.register_claim(
                "steward-1", "claim-a", "ent-a", "ben-a", "ai-2026", "330100",
                resource_id="res-park", chain=["park-x"],
            )
            service.register_claim(
                "steward-1", "claim-b", "ent-b", "ben-a", "ai-2026", "330000",
                resource_id="res-assoc", chain=["assoc-x"],
            )

            # 里程碑证据：培训完成与平台上线由不同机构报送，统一冻结归一。
            service.register_milestone(
                "steward-1", "m-train", "training_completed", 2, "2026-02-20", SHA,
                claim_id="claim-a",
            )
            service.register_milestone(
                "steward-1", "m-platform", "platform_online", 3, "2026-02-25", SHA,
                claim_id="claim-b",
            )
            service.register_milestone(
                "steward-1", "m-biz-a", "business_improvement", 3, "2026-03-01", SHA,
                claim_id="claim-a",
            )

            # 成效通过机构回调进入；园区与协会各报一次业务改善。
            payload_a = {
                "outcome_id": "out-a", "outcome_type": "business_improvement",
                "outcome_key": "biz-xinghe-2026", "claim_id": "claim-a",
                "value": "100000", "occurred_at": "2026-03-01", "evidence_level": 3,
                "milestone_ids": ["m-biz-a"],
            }
            first = service.ingest_callback("steward-1", "cb-1", "park-portal", payload_a)
            replay = service.ingest_callback("steward-1", "cb-1-replay", "park-portal", payload_a)
            assert first["status"] == "processed" and replay["status"] == "duplicate"
            assert replay["counted"] is False

            payload_b = {
                "outcome_id": "out-b", "outcome_type": "business_improvement",
                "outcome_key": "biz-xinghe-2026", "claim_id": "claim-b",
                "value": "100000", "occurred_at": "2026-03-05", "evidence_level": 3,
                "milestone_ids": ["m-platform"],
            }
            service.ingest_callback("steward-1", "cb-2", "association-hub", payload_b)

            # 关系识别：R1 确认重复簇、R2 共享组、R3 仅列为疑似簇。
            detected = service.detect_duplicates("auditor-1", "ai-support-attribution", 1)
            cluster_members = {tuple(item["applicant_ids"]): item for item in detected["clusters"]}
            assert ("ent-a", "ent-b") in cluster_members or ("ent-a", "ent-b", "ent-c") in cluster_members
            big = next(
                item for item in detected["clusters"] if "ent-a" in item["applicant_ids"]
                and "ent-c" in item["applicant_ids"]
            )
            assert big["duplicate_confirmed"] is True and big["shared_group"] is True
            assert big["identity_components"] == [["ent-a", "ent-b"]]
            suspect_pairs = {
                tuple(item["applicant_ids"])
                for item in detected["clusters"]
                if not item["duplicate_confirmed"] and not item["shared_group"]
            }
            assert ("ent-a", "ent-e") in suspect_pairs or ("ent-b", "ent-e") in suspect_pairs
            # 在关系生效之前，R2 簇不应出现。
            before = service.detect_duplicates(
                "auditor-1", "ai-support-attribution", 1, as_of="2025-12-31"
            )
            assert all(not c["shared_group"] for c in before["clusters"])

            # 初次归属：ent-b 因同一硬标识判重，只有 ent-a 计得全部成果。
            v1 = service.run_attribution(
                "analyst-1", "biz-xinghe-2026", "ai-support-attribution", version=1,
                reason="首轮评估归属",
            )
            assert v1["result"]["status"] == "single"
            assert Decimal(v1["result"]["total_share"]) == 1
            assert len(v1["result"]["duplicates"]) == 1
            published = service.publish_attribution("analyst-1", v1["attribution_id"], "public-dashboard", SHA)
            assert published["status"] == "published"

            # 子公司迟到的业务改善证据到达（证据迟到不回写，只形成新版本）。
            clock.advance(days=30)
            service.register_resource(
                "steward-1", "res-grant", "ai-2026", "grant", "330782",
                detail={"name": "子公司数字化补贴"},
            )
            service.register_claim(
                "steward-1", "claim-c", "ent-c", "ben-c", "ai-2026", "330782",
                resource_id="res-grant",
            )
            service.register_milestone(
                "steward-1", "m-biz-c", "business_improvement", 3, "2026-03-10", SHA,
                claim_id="claim-c", late=True,
            )
            service.register_outcome("steward-1", {
                "outcome_id": "out-c", "outcome_type": "business_improvement",
                "outcome_key": "biz-xinghe-2026", "claim_id": "claim-c",
                "value": "100000", "occurred_at": "2026-03-10", "evidence_level": 3,
                "milestone_ids": ["m-biz-c"], "late_evidence": True,
            })
            v2 = service.run_attribution(
                "analyst-1", "biz-xinghe-2026", "ai-support-attribution", version=1,
                reason="子公司迟到证据到达，共同贡献分摊",
            )
            assert v2["seq_no"] == 2 and v2["result"]["status"] == "shared"
            shares = sorted(item["share"] for item in v2["result"]["allocations"])
            assert shares == ["0.500000", "0.500000"]
            assert v2["result"]["total_value"] == "100000.000000"
            # 已发布的 v1 原样保留，状态仍是 published。
            old = service.get_attribution("auditor-1", v1["attribution_id"])
            assert old["status"] == "published"
            assert len(old["allocations"]) == 1

            # 利益相关方对 v2 提出异议；原结论保留，独立复核人受理。
            dispute = service.raise_dispute("steward-1", v2["attribution_id"], "子公司认为应按三家分摊")
            review_started = service.start_review("reviewer-1", dispute["dispute_id"])
            assert review_started["status"] == "in_review"
            review = service.complete_review(
                "reviewer-1", dispute["dispute_id"], "uphold",
                "复核认定应适用第二期政策窗口", policy_id="ai-support-attribution", version=2,
            )
            assert review["original_conclusion_preserved"] is True
            v3 = service.get_attribution("auditor-1", review["new_attribution_id"])
            assert v3["policy"]["version"] == 2 and v3["seq_no"] == 3
            kept = service.get_attribution("auditor-1", v2["attribution_id"])
            assert kept["status"] == "superseded" and len(kept["allocations"]) == 2

            # 管理视图：按任意政策版本查看覆盖、争议与来源链。
            coverage_v1 = service.coverage_report("auditor-1", "ai-support-attribution", 1)
            coverage_v2 = service.coverage_report("auditor-1", "ai-support-attribution", 2)
            # v1 政策下只有已发布的第一版（1 个地区、1 个有效受益人）；
            # v2 政策下第三版按两地两受益人分摊。
            assert coverage_v1["covered_region_count"] == 1
            assert coverage_v1["effective_beneficiary_count"] == 1
            assert coverage_v2["covered_region_count"] == 2
            assert coverage_v2["effective_beneficiary_count"] == 2
            pending = service.list_disputes("auditor-1", "open")
            assert pending["disputes"] == []
            lineage = service.outcome_lineage("auditor-1", "biz-xinghe-2026")
            assert [item["seq_no"] for item in lineage["attribution_versions"]] == [1, 2, 3]
            assert lineage["attribution_versions"][0]["publication"] is not None
            assert lineage["outcomes"][0]["claim"]["chain"][0]["organization_id"] == "park-x"
            views = service.claim_views("auditor-1", "ai-2026")
            assert {v["claim_id"] for v in views["views"]} == {"claim-a", "claim-b", "claim-c"}

            schema = inspect_schema(connection)
        finally:
            connection.close()

    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "clusters": len(detected["clusters"]),
        "duplicate_callback": replay["status"],
        "attribution_versions": [item["seq_no"] for item in lineage["attribution_versions"]],
        "published_version_locked": old["status"] == "published",
        "v2_shares": shares,
        "v2_total_conserved": v2["result"]["total_value"],
        "dispute_final_status": review["status"],
        "coverage_v1_regions": coverage_v1["covered_region_count"],
        "coverage_v1_effective_beneficiaries": coverage_v1["effective_beneficiary_count"],
        "lineage_outcomes": len(lineage["outcomes"]),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行受益关系与成效归属的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
