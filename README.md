# 全球数字贸易合作运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理跨境数字合作中的资源流转、项目证据评估和统计资料质量。平台把运营节点、合作通道、资源申请、项目版本、分析决定、样本观测、权限和审计事件持久化到 SQLite，供秘书处、项目办公室、数据团队和审计人员协作使用。

## 目录

- src/trade_flow/：运营节点、合作通道、资源批次、额度申请、分配和情景分析；
- src/cooperation_assurance/：合作项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/metric_quality/：统计样本批次、指标观测、质量分析、账号权限和审批；
- src/benefit_attribution/：申请主体、最终受益人、关联组织、支持资源、里程碑证据与
  政策版本的冻结登记，重复/共享成果的可解释识别、守恒分摊、版本化归属、
  发布冻结、异议独立复核与来源链管理视图；
- fixtures/：离线验收使用的评估协议与结构化观测；
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

四条命令会在临时 SQLite 数据库中完成合作资源流转、项目证据评估、统计资料质量
和受益关系与成效归属流程，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m trade_flow.api --database trade-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m cooperation_assurance.api --database cooperation-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m metric_quality.api --database metric-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m benefit_attribution.api --database benefit-attribution.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 受益关系与成效归属

src/benefit_attribution/ 面向"同一家小企业通过园区、协会和子公司被重复计入"的问题：

- **六类冻结对象**：申请主体、最终受益人、关联组织（集团母子/园区入驻/协会成员，
  带生效时间窗）、支持资源、里程碑证据、政策版本；全部内容摘要存证，只追加不改写。
- **可解释规则**：政策版本显式声明启用的规则编号（统一法人身份、集团/园区/协会
  关联、同一支持资源、同一里程碑证据、联系方式指纹）；每次合并都记录触发的规则、
  申报对与关联组织，不同类型里程碑（培训完成/平台上线/业务改善）不会跨类合并。
- **守恒分摊**：重复成果只保留一处归属（其余份额记零留痕）；共享成果在共同贡献者
  间等权或按申报权重分摊，末位承担舍入差，份额合计严格等于簇总量。
- **版本化与发布冻结**：证据迟到或关系变化只追加新归属版本；已对外发布的版本由
  SQLite 触发器在数据库层面禁止 UPDATE/DELETE。
- **幂等回调**：机构重复推送凭 Idempotency-Key 识别，单事务内"先占键再入账"，
  重复回调不会再次计入。
- **异议与独立复核**：提出异议保留原结论；发起人不能复核自己的异议；复核驳回恢复
  原状态，成立则另起复核版本并在原版本留链。
- **管理视图**：`coverage`、`dashboard`、`source_chain` 支持按任意政策版本查看
  覆盖地区、有效受益者（份额大于零）、未决争议，以及每项成果"政策→关系→申报→
  资源/证据摘要→份额→异议→版本链"的完整来源链。
