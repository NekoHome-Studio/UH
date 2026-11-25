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
# R2 审查 P1-5：原边界 (?:^|[_-]) 不认 camelCase，dbPassword / authToken 漏检。
# 先把驼峰拆成下划线形态再匹配，覆盖 dbPassword / auth_token / X-Api-Key 等写法。
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"authorization|cookie|session[_-]?id|master[_-]?key|credential)s?(?:$|[_-])"
)


def _normalize_key(key: str) -> str:
    """camelCase / PascalCase → snake_case，使键名规则统一生效。"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)

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
    # R2 审查 P1-5：JSON / YAML 里的 camelCase 键（dbPassword / authToken）。
    # redact_text 无键名上下文，需在值级识别「敏感键 + 引号值」整体。
    re.compile(
        r"(?i)([\x22\x27]?(?:[a-z0-9]*(?:password|passwd|pwd|secret|token|credential|"
        r"api[_-]?key|access[_-]?key|private[_-]?key|master[_-]?key))[a-z0-9]*[\x22\x27]?"
        r"\s*[:=]\s*)[\x22\x27]([^\x22\x27]{8,})[\x22\x27]"
    ),
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


def _is_sequential_run(text: str) -> bool:
    """R2 审查 P1-5：识别字母序/数字序片段（abcdefg / 0123456789）。

    含空白的高熵串按空白切分后每段都短，逐 token 熵检测必然漏过。
    但这类合成凭据有强特征——连续递增字符；自然语言不具备，故不误杀。
    """
    if len(text) < 6:
        return False
    codes = [ord(c) for c in text.lower()]
    asc = sum(1 for a, b in zip(codes, codes[1:]) if b - a == 1)
    desc = sum(1 for a, b in zip(codes, codes[1:]) if a - b == 1)
    return max(asc, desc) >= len(codes) - 2


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
        # 带键名前缀的模式（2 组）保留键名只替换值；其余整体替换
        if pattern.groups >= 2:
            out = pattern.sub(lambda m: m.group(1) + '"' + REDACTED + '"', out)
        else:
            out = pattern.sub(REDACTED, out)
    # 熵兜底：按空白切分，逐 token 判定。
    # R2 审查 P1-5：原条件 `if out is not text or REDACTED not in out` 是逻辑漏洞——
    # 原文本身含 *REDACTED*（如上游已部分脱敏）且正则未替换任何内容时，
    # 整段熵检测被短路跳过，后续真实凭据明文漏出。改为无条件执行。
    parts = re.split(r"(\s+)", out)
    parts = [
        REDACTED if (p and not p.isspace() and (_looks_like_secret(p) or _is_sequential_run(p)))
        else p
        for p in parts
    ]
    return "".join(parts)


def _truncate(value: str, limit: int = MAX_BODY_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"…[+{len(value) - limit} chars]"


def redact_value(key: str, value: Any) -> Any:
    if key and _SENSITIVE_KEY.search(_normalize_key(key)):
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
