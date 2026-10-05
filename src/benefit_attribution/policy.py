"""归属政策版本与可解释的去重/共享识别规则。

规则是声明式、可哈希的纯数据：同一政策版本对同一输入永远产出同一结论，
每条判定都能回溯到规则编号与命中证据。政策一经发布即冻结，后续变化
只能发布新版本，已发布结果始终绑定原政策版本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Collection, Mapping, Sequence


_PATTERN_FLAGS = {"i": re.IGNORECASE}

_IDENTITY_TYPES = frozenset({"business_license", "tax_number", "social_credit", "foreign_registry", "other"})
_RELATION_KINDS = frozenset({
    "parent", "subsidiary", "branch", "association_member", "park_tenant", "joint_venture", "same_address", "other"
})


class PolicyValidationError(ValueError):
    """政策定义不能满足契约。"""


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyValidationError(f"{path} 必须是对象")
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise PolicyValidationError(f"{path} 必须是数组")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def _decimal(value: object, path: str, *, low: Decimal | None = None, high: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise PolicyValidationError(f"{path} 必须是十进制数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PolicyValidationError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite() or result.as_tuple().exponent < -6:
        raise PolicyValidationError(f"{path} 必须是有限数值且最多 6 位小数")
    if low is not None and result < low:
        raise PolicyValidationError(f"{path} 不能小于 {low}")
    if high is not None and result > high:
        raise PolicyValidationError(f"{path} 不能大于 {high}")
    return result


def _compile_pattern(pattern: str, flags: str) -> re.Pattern[str]:
    try:
        flag_value = 0
        for letter in flags:
            if letter not in _PATTERN_FLAGS:
                raise PolicyValidationError(f"未知正则标志: {letter}")
            flag_value |= _PATTERN_FLAGS[letter]
        return re.compile(pattern, flag_value)
    except re.error as exc:
        raise PolicyValidationError(f"无法编译规则表达式 {pattern!r}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class IdentityMergeRule:
    """规则 R1：申请主体之间出现同一硬标识即构成重复。"""

    rule_id: str
    identity_types: frozenset[str]
    note: str

    def matches(self, shared: Collection[str]) -> bool:
        return bool(self.identity_types.intersection(shared))

    def explain(self, identity_type: str) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_family": "R1-identity",
            "matched": identity_type,
            "note": self.note,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "identity_types": sorted(self.identity_types),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class AffiliationRule:
    """规则 R2：登记的关联关系（园区、协会、母子公司等）构成共享组。"""

    rule_id: str
    relation_kinds: frozenset[str]
    note: str

    def explains(self, kind: str) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_family": "R2-affiliation",
            "matched": kind,
            "note": self.note,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "relation_kinds": sorted(self.relation_kinds),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class FuzzyRule:
    """规则 R3：名称归一化后命中正则，且弱标识一致时构成疑似重复。"""

    rule_id: str
    name_pattern: str
    pattern_flags: str
    weak_fields: frozenset[str]
    require_weak_match: bool
    note: str
    _compiled: re.Pattern[str] = field(compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name_pattern": self.name_pattern,
            "pattern_flags": self.pattern_flags,
            "weak_fields": sorted(self.weak_fields),
            "require_weak_match": self.require_weak_match,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class OutcomeTypeRule:
    """一类成效的归属与去重参数。

    - dedup_window_days：同键成效超过该间隔才允许算第二次；
    - min_evidence_level：可计入的最低里程碑证据等级（1 最弱）；
    - max_shareholders：单个成果允许的共同贡献方上限；
    - late_evidence_creates_revision：迟到证据是否只能产生新版本；
    - publication_locks_version：对外发布后是否锁定版本不得回写。
    """

    outcome_type: str
    dedup_window_days: int
    min_evidence_level: int
    max_shareholders: int
    late_evidence_creates_revision: bool
    publication_locks_version: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_type": self.outcome_type,
            "dedup_window_days": self.dedup_window_days,
            "min_evidence_level": self.min_evidence_level,
            "max_shareholders": self.max_shareholders,
            "late_evidence_creates_revision": self.late_evidence_creates_revision,
            "publication_locks_version": self.publication_locks_version,
        }


@dataclass(frozen=True, slots=True)
class AttributionPolicy:
    """一个不可变的政策版本。"""

    policy_id: str
    version: int
    title: str
    effective_from: str
    identity_rules: tuple[IdentityMergeRule, ...]
    affiliation_rules: tuple[AffiliationRule, ...]
    fuzzy_rules: tuple[FuzzyRule, ...]
    outcome_rules: tuple[OutcomeTypeRule, ...]
    default_equal_split: bool

    def outcome_rule(self, outcome_type: str) -> OutcomeTypeRule:
        for rule in self.outcome_rules:
            if rule.outcome_type == outcome_type:
                return rule
        raise PolicyValidationError(f"政策未定义成效类型: {outcome_type}")

    def to_catalog_dict(self) -> dict[str, Any]:
        """供持久化与摘要计算的规范化字典（不含编译对象）。"""

        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "title": self.title,
            "effective_from": self.effective_from,
            "rules": {
                "identity": [rule.to_dict() for rule in self.identity_rules],
                "affiliation": [rule.to_dict() for rule in self.affiliation_rules],
                "fuzzy": [rule.to_dict() for rule in self.fuzzy_rules],
                "outcome": [rule.to_dict() for rule in self.outcome_rules],
            },
            "default_equal_split": self.default_equal_split,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AttributionPolicy":
        policy_id = _required_text(raw.get("policy_id"), "policy_id")
        try:
            version = int(raw["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyValidationError("version 必须是正整数") from exc
        if version <= 0:
            raise PolicyValidationError("version 必须是正整数")
        title = _required_text(raw.get("title"), "title")
        effective_from = _required_text(raw.get("effective_from"), "effective_from")

        rules = raw.get("rules", {})
        rules_map = _require_mapping(rules, "rules")

        identity_rules: list[IdentityMergeRule] = []
        seen_rule_ids: set[str] = set()
        for item in _require_sequence(rules_map.get("identity", ()), "rules.identity"):
            data = _require_mapping(item, "rules.identity[]")
            rule_id = _required_text(data.get("rule_id"), "identity rule_id")
            types_raw = _require_sequence(data.get("identity_types", ()), f"{rule_id}.identity_types")
            types = frozenset(_required_text(value, f"{rule_id}.identity_types[]") for value in types_raw)
            unknown = types - _IDENTITY_TYPES
            if unknown:
                raise PolicyValidationError(f"{rule_id} 使用了未知标识类型: {sorted(unknown)}")
            note = _required_text(data.get("note"), f"{rule_id}.note")
            _unique_rule_id(rule_id, seen_rule_ids)
            identity_rules.append(IdentityMergeRule(rule_id, types, note))

        affiliation_rules: list[AffiliationRule] = []
        for item in _require_sequence(rules_map.get("affiliation", ()), "rules.affiliation"):
            data = _require_mapping(item, "rules.affiliation[]")
            rule_id = _required_text(data.get("rule_id"), "affiliation rule_id")
            kinds_raw = _require_sequence(data.get("relation_kinds", ()), f"{rule_id}.relation_kinds")
            kinds = frozenset(_required_text(value, f"{rule_id}.relation_kinds[]") for value in kinds_raw)
            unknown = kinds - _RELATION_KINDS
            if unknown:
                raise PolicyValidationError(f"{rule_id} 使用了未知关联类型: {sorted(unknown)}")
            note = _required_text(data.get("note"), f"{rule_id}.note")
            _unique_rule_id(rule_id, seen_rule_ids)
            affiliation_rules.append(AffiliationRule(rule_id, kinds, note))

        fuzzy_rules: list[FuzzyRule] = []
        for item in _require_sequence(rules_map.get("fuzzy", ()), "rules.fuzzy"):
            data = _require_mapping(item, "rules.fuzzy[]")
            rule_id = _required_text(data.get("rule_id"), "fuzzy rule_id")
            name_pattern = _required_text(data.get("name_pattern"), f"{rule_id}.name_pattern")
            flags = str(data.get("pattern_flags", "") or "").strip()
            weak_raw = _require_sequence(data.get("weak_fields", ()), f"{rule_id}.weak_fields")
            weak_fields = frozenset(_required_text(value, f"{rule_id}.weak_fields[]") for value in weak_raw)
            require_weak = bool(data.get("require_weak_match", True))
            if require_weak and not weak_fields:
                raise PolicyValidationError(f"{rule_id} 要求弱标识一致时必须给出 weak_fields")
            note = _required_text(data.get("note"), f"{rule_id}.note")
            compiled = _compile_pattern(name_pattern, flags)
            _unique_rule_id(rule_id, seen_rule_ids)
            fuzzy_rules.append(
                FuzzyRule(rule_id, name_pattern, flags, weak_fields, require_weak, note, compiled)
            )

        outcome_rules: list[OutcomeTypeRule] = []
        seen_types: set[str] = set()
        for item in _require_sequence(rules_map.get("outcome", ()), "rules.outcome"):
            data = _require_mapping(item, "rules.outcome[]")
            outcome_type = _required_text(data.get("outcome_type"), "outcome_type")
            if outcome_type in seen_types:
                raise PolicyValidationError(f"成效类型重复定义: {outcome_type}")
            seen_types.add(outcome_type)
            try:
                window = int(data["dedup_window_days"])
                level = int(data["min_evidence_level"])
                max_shareholders = int(data["max_shareholders"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PolicyValidationError(f"{outcome_type} 的整数字段缺失或非法") from exc
            if window < 0:
                raise PolicyValidationError(f"{outcome_type}.dedup_window_days 不能为负")
            if not 1 <= level <= 5:
                raise PolicyValidationError(f"{outcome_type}.min_evidence_level 必须在 1..5")
            if max_shareholders < 1:
                raise PolicyValidationError(f"{outcome_type}.max_shareholders 必须为正整数")
            outcome_rules.append(
                OutcomeTypeRule(
                    outcome_type,
                    window,
                    level,
                    max_shareholders,
                    bool(data.get("late_evidence_creates_revision", True)),
                    bool(data.get("publication_locks_version", True)),
                )
            )
        if not outcome_rules:
            raise PolicyValidationError("政策至少要定义一类成效规则")

        default_equal_split = bool(raw.get("default_equal_split", True))
        return cls(
            policy_id=policy_id,
            version=version,
            title=title,
            effective_from=effective_from,
            identity_rules=tuple(identity_rules),
            affiliation_rules=tuple(affiliation_rules),
            fuzzy_rules=tuple(fuzzy_rules),
            outcome_rules=tuple(outcome_rules),
            default_equal_split=default_equal_split,
        )


def _unique_rule_id(rule_id: str, seen: set[str]) -> None:
    if rule_id in seen:
        raise PolicyValidationError(f"规则编号重复: {rule_id}")
    seen.add(rule_id)


# 纯数值校验，供服务层在分摊时复用。
def validate_share(values: Sequence[object]) -> tuple[Decimal, ...]:
    shares: list[Decimal] = []
    for value in values:
        shares.append(_decimal(value, "share", low=Decimal("0"), high=Decimal("1")))
    return tuple(shares)
