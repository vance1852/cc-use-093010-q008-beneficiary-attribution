"""确定性的规范 JSON 与内容摘要。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"不能序列化 {type(value).__name__}")


def load_json(path: Path) -> Any:
    """读取 UTF-8 JSON，拒绝重复键与非标准常量。"""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{path} 含重复键 {key}")
            result[key] = value
        return result

    text = path.read_text(encoding="utf-8")
    return json.loads(
        text,
        parse_float=Decimal,
        parse_constant=_reject_constant,
        object_pairs_hook=pairs_hook,
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"JSON 不允许非有限数值 {value}")


def canonical_json(value: object) -> str:
    """生成跨进程一致的紧凑 JSON 文本，Decimal 按精确十进制输出。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
