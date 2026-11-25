"""日志脱敏（M3-8 / §9.9）：密钥模式正则 + 高熵检测 + 正文裁剪。

设计要点：
- 只脱敏值，不脱敏键名与 trace_id/span_id（保留排查链路，风险项要求）
- 正则覆盖常见凭据形态；熵检测兜底未知格式的长随机串
- 记忆原文与完整工具返回按白名单裁剪（正文不落原文）
"""
from __future__ import annotations

import math
import re
from typing import Any

REDACTED = "*REDACTED*"

# 键名命中即整体脱敏（不落值）
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"authorization|cookie|session[_-]?id|master[_-]?key|credential)s?(?:$|[_-])"
)

# 值模式（任意位置命中即替换该片段）
_VALUE_PATTERNS = (
    # Authorization: Bearer xxx / Basic xxx
    re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{12,}"),
    # URL 内嵌凭据 scheme://user:pass@host
    re.compile(r"://[^\s/@:]*:[^\s/@]+@"),
    # 常见私钥块
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    # AWS 风格 AK
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # JWT
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # 通用 key=value 形式（query / cli）
    re.compile(r"(?i)\b(?:password|passwd|token|api[_-]?key|secret|access[_-]?key)=\S+"),
    # 独立审查补充：已知厂商前缀的短凭据。熵检测要求 ≥24 字符，
    # sk-xxx / ghp_xxx 这类短 token 会漏出（实测 sk-abc123 不脱敏）。
    # 前缀本身即强信号，不依赖长度与熵。
    re.compile(r"\b(?:sk|pk|rk)-(?:live-|test-|proj-)?[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{6,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{6,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{10,}\b"),
)

# 记忆正文 / 工具返回裁剪阈值
MAX_BODY_CHARS = 160


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _looks_like_secret(value: str) -> bool:
    """高熵且够长 → 视为未知格式凭据（仅供熵兜底，不误杀短词）。"""
    if len(value) < 24 or len(value) > 512:
        return False
    if any(c.isspace() for c in value):
        return False
    # base64/hex/urlsafe 字符集
    if not re.fullmatch(r"[A-Za-z0-9._~+/=:-]+", value):
        return False
    return _shannon_entropy(value) >= 4.2


def redact_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pattern in _VALUE_PATTERNS:
        out = pattern.sub(REDACTED, out)
    # 熵兜底：按空白切分，逐 token 判定
    if out is not text or REDACTED not in out:
        parts = re.split(r"(\s+)", out)
        parts = [REDACTED if (p and not p.isspace() and _looks_like_secret(p)) else p for p in parts]
        out = "".join(parts)
    return out


def _truncate(value: str, limit: int = MAX_BODY_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"…[+{len(value) - limit} chars]"


def redact_value(key: str, value: Any) -> Any:
    if key and _SENSITIVE_KEY.search(str(key)):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(key, item) for item in value]
    return value


def redact_body(text: str) -> str:
    """正文脱敏 + 裁剪：用于记忆原文与工具返回（不落全文）。"""
    if not isinstance(text, str):
        return text
    return _truncate(redact_text(text))


def redact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """日志字段整体脱敏（保留键名与 trace_id/span_id）。"""
    keep = {"trace_id", "span_id", "traceId", "spanId"}
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key in keep:
            out[key] = value
        elif key in {"content", "body", "text", "result", "preview", "content_preview"}:
            out[key] = redact_body(value) if isinstance(value, str) else redact_value(key, value)
        else:
            out[key] = redact_value(key, value)
    return out
