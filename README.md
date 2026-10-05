# 全球数字贸易合作运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理跨境数字合作中的资源流转、项目证据评估、统计资料质量和受益关系成效归属。平台把运营节点、合作通道、资源申请、项目版本、分析决定、样本观测、受益关系、归属版本、异议复核、权限和审计事件持久化到 SQLite，供秘书处、项目办公室、数据团队和审计人员协作使用。

## 目录

- src/trade_flow/：运营节点、合作通道、资源批次、额度申请、分配和情景分析；
- src/cooperation_assurance/：合作项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/metric_quality/：统计样本批次、指标观测、质量分析、账号权限和审批；
- src/benefit_attribution/：受益关系与成效归属——申请主体/最终受益人/关联组织/支持资源/里程碑证据/政策版本冻结，按可解释规则识别重复（R1 硬标识）与共享成果（R2 关联、R3 疑似），共同贡献守恒分摊，归属版本只追加、已发布结果锁定、异议独立复核；
- fixtures/：离线验收使用的评估协议、结构化观测与归属政策版本；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m trade_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m cooperation_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m metric_quality.acceptance
    PYTHONPATH=src python3 -m benefit_attribution.acceptance --workspace .

四条命令会在临时 SQLite 数据库中完成合作资源流转、项目证据评估、统计资料质量和受益成效归属（含重复回调、共享分摊、发布锁定、迟到证据新版本与异议复核）流程，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m trade_flow.api --database trade-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m cooperation_assurance.api --database cooperation-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m metric_quality.api --database metric-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m benefit_attribution.api --database benefit-attribution.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 受益关系与成效归属（benefit_attribution）

### 角色与权限

- steward（登记员）：冻结登记簿（主体/受益人/组织/关系/资源/申请/里程碑）、登记成效与回调、提出异议；
- analyst（归属分析师）：发布政策版本、执行归属、对外发布；
- reviewer（独立复核人）：受理异议并出具复核结论，不能是原归属制作人或异议提出人；
- auditor（审计人员）：读取全部报告、来源链与审计事件。

### 关键不变量

1. **冻结**：申请主体、最终受益人、关联组织、支持资源、里程碑证据、政策版本一经冻结不可修改；每条记录带内容 SHA-256。
2. **可解释去重**：R1 硬标识（营业执照/税号/统一社会信用代码）相同判同一实际主体；R2 园区、协会、母子公司等生效关系判共享组；R3 名称模式+弱标识仅列疑似，不自动判重。每条结论附规则编号与命中证据。
3. **守恒分摊**：共同贡献按声明比例或确定性最大余数法均摊，份额之和恒为 1，价值分摊余量计入主报方，总量严格守恒。
4. **只追加版本**：证据迟到或关系变化不回写，而是形成 `seq_no` 递增的新归属版本；输入摘要相同时拒绝产生新版本。
5. **发布锁定**：已对外发布（`publications` 留痕）的版本状态恒为 `published`，任何流程不得回写或再发布。
6. **异议与复核**：异议期间原结论保持有效；复核独立进行，支持异议时按指定政策版本追加新归属版本，原版本（含已发布版本）原样保留。
7. **回调幂等**：按来源+载荷摘要识别重复回调，重复回调落账标记 `duplicate` 但绝不再次计入成效。
8. **按版本查看**：管理者可按任意政策版本查询覆盖地区、有效受益者、未决争议及每项成果“回调→成效→里程碑→申请链→归属版本→发布”的完整来源链。

### 主要接口

- `POST /policies`、`POST /applicants|/beneficiaries|/organizations|/relations|/resources|/claims|/milestones`
- `POST /callbacks`、`POST /outcomes`
- `POST /duplicates/{policy_id}/detect?as_of=...`
- `POST /outcomes/{outcome_key}/attribute`、`GET /attributions/{id}`、`POST /attributions/{id}/publish`
- `POST /disputes`、`POST /disputes/{id}/review`、`POST /disputes/{id}/complete`、`GET /disputes?status=open`
- `GET /reports/coverage/{policy_id}?version=1`、`GET /outcomes/{outcome_key}/lineage`、`GET /claim_views?program_id=...`

