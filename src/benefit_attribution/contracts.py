"""归属政策版本的严格数据契约。

政策是"可解释规则"的载体：每条去重/共享规则都有稳定编号，归属重算时
把触发了哪些规则编号一并记录到归属版本上，管理者可以沿规则编号回溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class PolicyError(ValueError):
    """政策文本不能满足领域契约。"""


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyError(f"{path} 必须是对象")
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise PolicyError(f"{path} 必须是数组")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{path} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


# 政策可以声明的规则编号；编号本身即解释口径，删除/改名会破坏历史可追溯性。
RULE_CODES: Mapping[str, str] = {
    "LEGAL_IDENTITY": "同一统一社会信用代码（法人身份）只计一名有效受益者",
    "GROUP_SUBSIDIARY": "同一集团内母公司、子公司分别申请时合并为共享成果",
    "PARK_TENANT": "园区与其入驻企业就同一支持资源申报时合并为共享成果",
    "ASSOCIATION_MEMBER": "协会与其成员企业就同一支持资源申报时合并为共享成果",
    "SHARED_RESOURCE": "多项申报指向同一支持资源批次时合并为共享成果",
    "SAME_EVIDENCE": "多项申报引用同一份里程碑证据时判定为重复",
    "CONTACT_FINGERPRINT": "联系方式指纹一致且受益地区一致时判定为疑似重复",
}

VALID_SHARE_STRATEGIES = frozenset({"equal", "applicant_weight"})


@dataclass(frozen=True, slots=True)
class Policy:
    """一版不可歧义的成效归属与去重政策。"""

    policy_id: str
    version: int
    title: str
    effective_from: str
    rules: tuple[str, ...]
    share_strategy: str
    applicant_weight_field: str | None
    raw: Mapping[str, Any]

    @classmethod
    def from_dict(cls, raw: object) -> "Policy":
        data = _require_mapping(raw, "policy")
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise PolicyError("policy.version 必须是正整数")
        effective_from = _required_text(data.get("effective_from"), "policy.effective_from")
        rules = tuple(
            _required_text(item, f"policy.rules[{index}]")
            for index, item in enumerate(_require_sequence(data.get("rules"), "policy.rules"))
        )
        if not rules:
            raise PolicyError("policy.rules 至少要声明一条规则")
        if len(set(rules)) != len(rules):
            raise PolicyError("policy.rules 不能重复")
        unknown = sorted(set(rules) - set(RULE_CODES))
        if unknown:
            raise PolicyError(f"policy.rules 含未知规则编号: {unknown}")
        share_strategy = _required_text(data.get("share_strategy"), "policy.share_strategy")
        if share_strategy not in VALID_SHARE_STRATEGIES:
            raise PolicyError(
                f"policy.share_strategy 必须是 {sorted(VALID_SHARE_STRATEGIES)} 之一"
            )
        applicant_weight_field: str | None = None
        if share_strategy == "applicant_weight":
            applicant_weight_field = _required_text(
                data.get("applicant_weight_field"), "policy.applicant_weight_field"
            )
        return cls(
            policy_id=_required_text(data.get("policy_id"), "policy.policy_id"),
            version=version,
            title=_required_text(data.get("title"), "policy.title"),
            effective_from=effective_from,
            rules=rules,
            share_strategy=share_strategy,
            applicant_weight_field=applicant_weight_field,
            raw=dict(data),
        )

    @property
    def rule_set(self) -> frozenset[str]:
        return frozenset(self.rules)

    def explain(self, code: str) -> str:
        return RULE_CODES[code]
