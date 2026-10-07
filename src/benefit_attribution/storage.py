"""受益关系与成效归属服务的 SQLite 模式与事务辅助。

设计要点：
- 申请主体、最终受益人、关联组织、支持资源、里程碑证据、政策版本六类记录
  只追加、不更新、不删除，全部以内容摘要自证；
- 归属判定结果以 attribution_versions 版本链保存，新版本只追加；
- 已经对外发布（published_at 非空）的归属版本被触发器禁止回写；
- 重复回调登记在 processed_callbacks，重复键直接被数据库拒绝。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('registry_editor', 'policy_admin', 'analyst', 'reviewer', 'manager', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- 政策版本：只追加，(policy_id, version) 与内容摘要均唯一。
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    published_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    PRIMARY KEY (policy_id, version),
    UNIQUE (content_sha256)
);

-- 申请主体：通过园区/协会/子公司多渠道申报的同一法律实体在此冻结。
CREATE TABLE IF NOT EXISTS applicants (
    applicant_id TEXT PRIMARY KEY,
    legal_identity_code TEXT,
    name TEXT NOT NULL,
    applicant_kind TEXT NOT NULL CHECK (applicant_kind IN ('enterprise', 'park', 'association', 'subsidiary', 'public_body')),
    region_code TEXT NOT NULL,
    contact_fingerprint TEXT,
    channel TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (content_sha256)
);

-- 最终受益人（企业本体/自然人等），冻结快照。
CREATE TABLE IF NOT EXISTS beneficiaries (
    beneficiary_id TEXT PRIMARY KEY,
    beneficiary_kind TEXT NOT NULL CHECK (beneficiary_kind IN ('enterprise', 'natural_person', 'cooperative')),
    legal_identity_code TEXT,
    name TEXT NOT NULL,
    region_code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (content_sha256)
);

-- 关联组织关系（集团母子、园区入驻、协会成员），关系变化只能新增版本。
CREATE TABLE IF NOT EXISTS org_relations (
    relation_id TEXT PRIMARY KEY,
    relation_kind TEXT NOT NULL CHECK (relation_kind IN ('group_parent', 'group_subsidiary', 'park_tenant', 'association_member', 'trade_name')),
    left_org_id TEXT NOT NULL,
    right_org_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    supersedes_relation_id TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    detail_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (content_sha256)
);

-- 支持资源（培训名额、平台账户、补贴批次等），冻结快照。
CREATE TABLE IF NOT EXISTS support_resources (
    resource_id TEXT PRIMARY KEY,
    resource_kind TEXT NOT NULL CHECK (resource_kind IN ('training', 'platform', 'subsidy', 'advisory', 'data_access')),
    program_id TEXT NOT NULL,
    region_code TEXT NOT NULL,
    batch_ref TEXT,
    detail_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (content_sha256)
);

-- 里程碑证据（培训完成、平台上线、业务改善），带证据发生时间。
CREATE TABLE IF NOT EXISTS milestone_evidence (
    evidence_id TEXT PRIMARY KEY,
    milestone_kind TEXT NOT NULL CHECK (milestone_kind IN ('training_completed', 'platform_online', 'business_outcome', 'certification', 'other')),
    occurred_at TEXT NOT NULL,
    reporting_org TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    detail_json TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (content_sha256)
);

-- 受益申报：某申请主体就某资源为某最终受益人提交的成果主张，并附证据。
-- outcome_value 是要被归属守恒分摊的总量（如结业人数、上线平台数、改善金额）。
CREATE TABLE IF NOT EXISTS benefit_claims (
    claim_id TEXT PRIMARY KEY,
    applicant_id TEXT NOT NULL REFERENCES applicants(applicant_id),
    beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
    resource_id TEXT NOT NULL REFERENCES support_resources(resource_id),
    milestone_kind TEXT NOT NULL,
    outcome_value TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES milestone_evidence(evidence_id),
    reported_by_org TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (content_sha256)
);

-- 幂等的外部回调（各机构重复推送培训完成/平台上线/业务改善时不再计入）。
CREATE TABLE IF NOT EXISTS processed_callbacks (
    callback_key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    claim_id TEXT,
    created_at TEXT NOT NULL
);

-- 归属版本：同一组申报（scope_fingerprint 标识）的版本链。
CREATE TABLE IF NOT EXISTS attribution_versions (
    attribution_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    scope_fingerprint TEXT NOT NULL CHECK (length(scope_fingerprint) = 64),
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    basis_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'effective', 'disputed', 'superseded', 'revised')),
    change_reason TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY (attribution_id, version_no),
    UNIQUE (attribution_id, scope_fingerprint, version_no),
    FOREIGN KEY (policy_id, policy_version) REFERENCES policies(policy_id, version)
);

CREATE INDEX IF NOT EXISTS attribution_versions_scope ON attribution_versions(scope_fingerprint);

-- 归属簇：一次判定中的一个重复/共享组（单点申报自成一簇）。
CREATE TABLE IF NOT EXISTS attribution_clusters (
    cluster_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    attribution_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    cluster_key TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('unique', 'duplicate', 'shared')),
    matched_rule TEXT,
    total_value TEXT NOT NULL,
    UNIQUE (attribution_id, version_no, cluster_key),
    FOREIGN KEY (attribution_id, version_no) REFERENCES attribution_versions(attribution_id, version_no)
);

-- 归属份额：每条申报在其簇内分到的份额；同簇份额之和必须等于总量（守恒）。
CREATE TABLE IF NOT EXISTS attribution_shares (
    attribution_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    cluster_key TEXT NOT NULL,
    claim_id TEXT NOT NULL REFERENCES benefit_claims(claim_id),
    share_value TEXT NOT NULL,
    share_weight TEXT NOT NULL,
    PRIMARY KEY (attribution_id, version_no, claim_id),
    FOREIGN KEY (attribution_id, version_no, cluster_key)
        REFERENCES attribution_clusters(attribution_id, version_no, cluster_key)
);

-- 已对外发布的结果冻结：禁止对发布版本做任何 UPDATE/DELETE。
CREATE TRIGGER IF NOT EXISTS attribution_versions_no_update_after_publish
BEFORE UPDATE ON attribution_versions
WHEN OLD.published_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, '已发布的归属版本不得回写');
END;

CREATE TRIGGER IF NOT EXISTS attribution_clusters_no_change_after_publish
BEFORE UPDATE ON attribution_clusters
WHEN EXISTS (
    SELECT 1 FROM attribution_versions v
    WHERE v.attribution_id = OLD.attribution_id
      AND v.version_no = OLD.version_no
      AND v.published_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, '已发布归属版本的簇不得改写');
END;

CREATE TRIGGER IF NOT EXISTS attribution_clusters_no_delete_after_publish
BEFORE DELETE ON attribution_clusters
WHEN EXISTS (
    SELECT 1 FROM attribution_versions v
    WHERE v.attribution_id = OLD.attribution_id
      AND v.version_no = OLD.version_no
      AND v.published_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, '已发布归属版本的簇不得删除');
END;

CREATE TRIGGER IF NOT EXISTS attribution_shares_no_change_after_publish
BEFORE UPDATE ON attribution_shares
WHEN EXISTS (
    SELECT 1 FROM attribution_versions v
    WHERE v.attribution_id = OLD.attribution_id
      AND v.version_no = OLD.version_no
      AND v.published_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, '已发布归属版本的份额不得改写');
END;

CREATE TRIGGER IF NOT EXISTS attribution_shares_no_delete_after_publish
BEFORE DELETE ON attribution_shares
WHEN EXISTS (
    SELECT 1 FROM attribution_versions v
    WHERE v.attribution_id = OLD.attribution_id
      AND v.version_no = OLD.version_no
      AND v.published_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, '已发布归属版本的份额不得删除');
END;

-- 异议：提出异议保留原结论（原版本状态不被覆盖），另起独立复核。
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribution_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    cluster_key TEXT,
    reason TEXT NOT NULL,
    raised_by TEXT NOT NULL REFERENCES users(user_id),
    raised_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'review', 'rejected', 'upheld')),
    reviewer_id TEXT REFERENCES users(user_id),
    review_started_at TEXT,
    closed_at TEXT,
    determination_note TEXT,
    new_attribution_id TEXT,
    new_version_no INTEGER,
    FOREIGN KEY (attribution_id, version_no) REFERENCES attribution_versions(attribution_id, version_no)
);

-- 同一版本同一簇至多一个未决异议；整版异议 cluster_key 为 NULL（NULL 互不相等，可并存）。
CREATE UNIQUE INDEX IF NOT EXISTS one_open_dispute_per_cluster
ON disputes(attribution_id, version_no, cluster_key)
WHERE status IN ('open', 'review');

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "policies", "applicants", "beneficiaries", "org_relations",
    "support_resources", "milestone_evidence", "benefit_claims", "processed_callbacks",
    "attribution_versions", "attribution_clusters", "attribution_shares", "disputes",
    "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    # ThreadingHTTPServer 会在工作线程中复用同一连接；写入均经
    # BEGIN IMMEDIATE 串行化，busy_timeout 负责并发等待。
    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
