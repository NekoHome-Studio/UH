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


def _cjk_ratio(value: str) -> float:
    """CJK（中日韩）字符占比。凭据里 CJK 至多是少量装饰（前缀/标签），
    自然语言里 CJK 是主体。实测：凭据 ≤0.07，中文技术文本 ≥0.19。"""
    if not value:
        return 0.0
    cjk = sum(
        1 for ch in value
        if "\u4e00" <= ch <= "\u9fff"       # CJK 统一汉字
        or "\u3040" <= ch <= "\u30ff"       # 日文假名
        or "\uac00" <= ch <= "\ud7af"       # 韩文
    )
    return cjk / len(value)


# 结构化白名单：契约 ID / URL / 哈希前缀 / 文件路径。必须是**整 token 完整匹配**
# （或 key=value 中 key 为契约字段的精确形式），不能用松散子串搜索——
# 自审发现 `note=trc_<32hex>` 这类真凭据会伪装成 ID 绕过，故收紧为全匹配。
_STRUCTURED_SAFE = re.compile(
    r"^(?:"
    # 契约字段=ID：trace_id=uh_trc_、span_id=sp_（span 无 uh_ 前缀）、
    # session/task/entry 带 uh_ 前缀。key 必须是契约字段，不接受 note=trc_ 伪装。
    r"(?:trace_id=uh_trc|span_id=sp|session_id=uh_ses|task_id=uh_task|entry_id=uh_mem)_[0-9a-f]{16,}"
    r"|uh_(?:trc|sp|ses|task|mem)_[0-9a-f]{16,}"   # 裸契约 ID（如 `field: uh_trc_<hex>` 切分后）
    r"|sp_[0-9a-f]{16,}"                            # 裸 span id
    r"|(?:sha256|sha1|sha512|md5):[0-9a-f]+"                                                        # 哈希
    r"|[a-z][a-z0-9+.-]*://\S+"                                                                     # URL
    r"|[A-Za-z]:[\\/][^\s]+|(?:/[^\s/]+){2,}"                                                      # 绝对路径
    r")$",
    re.I,
)


def _looks_like_secret(value: str) -> bool:
    """高熵 token → 未知格式凭据；自然语言与结构化 ID 零误杀。

    独立审查 P1-1 修复三处漏出：长度下限 24→15（短凭据）、放宽 ASCII 限制
    （非 ASCII/emoji）、取消 isspace 整串否决（含空白串按 token 切分后判定）。

    自审修正：首版用「字符类别 ≥3」区分，但中文技术文本天然是 CJK+数字+标点
    三类，导致大面积误杀；改用 **CJK 占比** 判别（凭据 ≤0.07 / 自然语言 ≥0.19），
    并把 ID 豁免从松散子串搜索收紧为整 token 全匹配，堵住 `note=trc_<hex>` 伪装的
    新绕过。
    """
    if len(value) < 15 or len(value) > 512:
        return False
    if _STRUCTURED_SAFE.fullmatch(value):
        return False
    # 非 ASCII 凭据里 CJK 只是少量装饰；占比高则是自然语言正文，放行。
    if _cjk_ratio(value) > 0.12:
        return False
    return _shannon_entropy(value) >= 3.8


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
