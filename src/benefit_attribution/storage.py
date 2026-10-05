"""受益关系与成效归属服务的 SQLite 模式与事务辅助。

所有业务事实表只追加、不回写：申请主体、最终受益人、关联组织、支持资源、
里程碑证据、政策版本冻结后不可修改；关系变化与证据迟到只能写入新行并形成
新的归属版本。对外发布通过 publications 留痕，已发布版本永久锁定。
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
    role TEXT NOT NULL CHECK (role IN ('steward', 'analyst', 'reviewer', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS policy_versions (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    published_by TEXT NOT NULL REFERENCES users(user_id),
    published_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS applicants (
    applicant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    applicant_kind TEXT NOT NULL CHECK (applicant_kind IN ('enterprise', 'cooperative', 'institution', 'other')),
    region_code TEXT NOT NULL,
    weak_json TEXT NOT NULL DEFAULT '{}',
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    frozen_by TEXT NOT NULL REFERENCES users(user_id),
    frozen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applicant_identities (
    applicant_id TEXT NOT NULL REFERENCES applicants(applicant_id),
    identity_type TEXT NOT NULL CHECK (identity_type IN
        ('business_license', 'tax_number', 'social_credit', 'foreign_registry', 'other')),
    identity_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    PRIMARY KEY (applicant_id, identity_type, identity_value)
);

CREATE INDEX IF NOT EXISTS applicant_identities_lookup
ON applicant_identities(identity_type, normalized_value);

CREATE TABLE IF NOT EXISTS beneficiaries (
    beneficiary_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    beneficiary_kind TEXT NOT NULL CHECK (beneficiary_kind IN
        ('enterprise', 'micro_business', 'cooperative', 'household', 'person', 'other')),
    region_code TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    frozen_by TEXT NOT NULL REFERENCES users(user_id),
    frozen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    organization_kind TEXT NOT NULL CHECK (organization_kind IN
        ('park', 'association', 'subsidiary', 'group', 'platform', 'other')),
    region_code TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    frozen_by TEXT NOT NULL REFERENCES users(user_id),
    frozen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relations (
    relation_id TEXT PRIMARY KEY,
    left_applicant_id TEXT NOT NULL REFERENCES applicants(applicant_id),
    right_applicant_id TEXT NOT NULL REFERENCES applicants(applicant_id),
    organization_id TEXT REFERENCES organizations(organization_id),
    kind TEXT NOT NULL CHECK (kind IN
        ('parent', 'subsidiary', 'branch', 'association_member', 'park_tenant',
         'joint_venture', 'same_address', 'other')),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    supersedes_relation_id TEXT REFERENCES relations(relation_id),
    frozen_by TEXT NOT NULL REFERENCES users(user_id),
    frozen_at TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK (left_applicant_id <> right_applicant_id)
);

CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK (resource_type IN
        ('training', 'platform', 'grant', 'voucher', 'mentoring', 'credit_line', 'other')),
    region_code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    frozen_by TEXT NOT NULL REFERENCES users(user_id),
    frozen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    applicant_id TEXT NOT NULL REFERENCES applicants(applicant_id),
    beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
    program_id TEXT NOT NULL,
    resource_id TEXT REFERENCES resources(resource_id),
    region_code TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    frozen_by TEXT NOT NULL REFERENCES users(user_id),
    frozen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_chain (
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    PRIMARY KEY (claim_id, position)
);

CREATE TABLE IF NOT EXISTS milestones (
    milestone_id TEXT PRIMARY KEY,
    claim_id TEXT REFERENCES claims(claim_id),
    evidence_type TEXT NOT NULL CHECK (evidence_type IN
        ('training_completed', 'platform_online', 'business_improvement', 'certificate', 'filing', 'other')),
    evidence_level INTEGER NOT NULL CHECK (evidence_level BETWEEN 1 AND 5),
    occurred_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    late INTEGER NOT NULL DEFAULT 0 CHECK (late IN (0, 1)),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    outcome_type TEXT NOT NULL,
    outcome_key TEXT NOT NULL,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    applicant_id TEXT NOT NULL REFERENCES applicants(applicant_id),
    beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
    value TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    evidence_level INTEGER NOT NULL CHECK (evidence_level BETWEEN 1 AND 5),
    milestone_ids_json TEXT NOT NULL,
    late_evidence INTEGER NOT NULL DEFAULT 0 CHECK (late_evidence IN (0, 1)),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    registered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS outcomes_by_key ON outcomes(outcome_key, occurred_at, outcome_id);

CREATE TABLE IF NOT EXISTS callback_receipts (
    callback_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    outcome_id TEXT REFERENCES outcomes(outcome_id),
    status TEXT NOT NULL CHECK (status IN ('processed', 'duplicate')),
    duplicate_of_callback_id TEXT REFERENCES callback_receipts(callback_id),
    received_by TEXT NOT NULL REFERENCES users(user_id),
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attributions (
    attribution_id TEXT PRIMARY KEY,
    outcome_key TEXT NOT NULL,
    outcome_type TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    seq_no INTEGER NOT NULL CHECK (seq_no > 0),
    window_anchor_outcome_id TEXT NOT NULL REFERENCES outcomes(outcome_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'published', 'superseded')),
    result_json TEXT NOT NULL,
    explanations_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    total_share TEXT NOT NULL,
    total_value TEXT NOT NULL,
    supersedes_attribution_id TEXT REFERENCES attributions(attribution_id),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    published_at TEXT,
    superseded_at TEXT,
    FOREIGN KEY (policy_id, policy_version) REFERENCES policy_versions(policy_id, version),
    UNIQUE (outcome_key, seq_no)
);

CREATE INDEX IF NOT EXISTS attributions_by_policy ON attributions(policy_id, policy_version, status);

CREATE TABLE IF NOT EXISTS attribution_outcomes (
    attribution_id TEXT NOT NULL REFERENCES attributions(attribution_id),
    outcome_id TEXT NOT NULL REFERENCES outcomes(outcome_id),
    role TEXT NOT NULL CHECK (role IN ('anchor', 'member', 'duplicate')),
    PRIMARY KEY (attribution_id, outcome_id)
);

CREATE TABLE IF NOT EXISTS publications (
    publication_id TEXT PRIMARY KEY,
    attribution_id TEXT NOT NULL UNIQUE REFERENCES attributions(attribution_id),
    channel TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL CHECK (length(receipt_sha256) = 64),
    published_by TEXT NOT NULL REFERENCES users(user_id),
    published_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribution_id TEXT NOT NULL REFERENCES attributions(attribution_id),
    outcome_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'in_review', 'upheld', 'rejected')),
    raised_by TEXT NOT NULL REFERENCES users(user_id),
    raised_at TEXT NOT NULL,
    closed_by TEXT REFERENCES users(user_id),
    closed_at TEXT,
    close_note TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id INTEGER NOT NULL UNIQUE REFERENCES disputes(dispute_id),
    reviewer_id TEXT NOT NULL REFERENCES users(user_id),
    decision TEXT CHECK (decision IN ('uphold', 'dismiss')),
    original_attribution_id TEXT NOT NULL REFERENCES attributions(attribution_id),
    new_attribution_id TEXT REFERENCES attributions(attribution_id),
    finding TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

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
    "schema_meta", "users", "policy_versions", "applicants", "applicant_identities",
    "beneficiaries", "organizations", "relations", "resources", "claims", "claim_chain",
    "milestones", "outcomes", "callback_receipts", "attributions", "attribution_outcomes",
    "publications", "disputes", "reviews", "audit_events",
})


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    HTTP 服务在工作线程间共享连接时传 check_same_thread=False；
    写入均经由 BEGIN IMMEDIATE 串行化，配合 busy_timeout 保证安全。
    """

    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=check_same_thread
    )
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
