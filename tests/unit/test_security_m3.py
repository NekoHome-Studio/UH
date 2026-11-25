"""M3-8 安全专项：SEC-01~05 + 日志脱敏 + SBOM + 供应链。

SEC-01 日志不含凭据（正则+熵脱敏）
SEC-02 日志不含记忆原文/完整工具返回（正文裁剪）
SEC-03 L2 字段级加密（key_ref: memory-master-key）
SEC-04 WS 跨归属订阅被拒（USR-1002 + 4403）
SEC-05 插件供应链（manifest 存在 + 无内嵌凭据 + SBOM 可生成）
"""
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from kernel.logging import JsonFormatter
from kernel.redaction import MAX_BODY_CHARS, REDACTED, redact_body, redact_fields, redact_text

ROOT = Path(__file__).resolve().parents[2]


# ---------- SEC-01 凭据脱敏 ----------
@pytest.mark.parametrize("secret", [
    "AKIAIOSFODNN7EXAMPLE",
    "Bearer abcdefghijklmnopqrstuvwxyz012345",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "postgresql://user:s3cr3t@db:5432/uh",
])
def test_sec01_secret_patterns_are_redacted(secret):
    out = redact_text(f"prefix {secret} suffix")
    assert REDACTED in out
    assert secret not in out


def test_sec01_sensitive_key_values_are_redacted():
    fields = redact_fields({
        "api_key": "anything-goes-here",
        "authorization": "Bearer xyz",
        "master_key": "k",
        "trace_id": "uh_trc_keepme",
        "span_id": "sp_keep",
        "note": "normal text",
    })
    assert fields["api_key"] == REDACTED
    assert fields["authorization"] == REDACTED
    assert fields["master_key"] == REDACTED
    # 排查链路必须保留
    assert fields["trace_id"] == "uh_trc_keepme"
    assert fields["span_id"] == "sp_keep"
    assert fields["note"] == "normal text"


def test_sec01_high_entropy_unknown_secret_is_redacted():
    token = "Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga"  # 32 位高熵，无已知前缀
    assert REDACTED in redact_text(f"value={token}")
    # 正常长词（低熵）不得误杀
    assert redact_text("configuration-management-system") == "configuration-management-system"


def test_sec01_formatter_applies_redaction_end_to_end():
    record = logging.LogRecord("kernel.test", logging.WARNING, __file__, 1, "probe", None, None)
    record.event = "probe"
    record.fields = {"api_key": "super-secret-value", "msg": "ok", "trace_id": "uh_trc_1"}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["api_key"] == REDACTED
    assert payload["trace_id"] == "uh_trc_1"
    assert payload["level"] == "warning"


# ---------- SEC-02 正文不落原文 ----------
def test_sec02_body_is_truncated_and_redacted():
    body = "记忆原文" * 200
    out = redact_body(body)
    assert len(out) < len(body)
    assert out.startswith("记忆原文"[:4])
    assert "chars]" in out
    assert len(out) <= MAX_BODY_CHARS + 32


def test_sec02_content_fields_use_body_policy():
    fields = redact_fields({"content": "x" * 500, "content_preview": "y" * 500, "other": "z" * 500})
    assert "chars]" in fields["content"]
    assert "chars]" in fields["content_preview"]
    # 非正文键不裁剪（保留诊断信息）
    assert fields["other"] == "z" * 500


# ---------- SEC-03 L2 加密 ----------
def test_sec03_l2_is_encrypted_and_not_semantically_indexed(tmp_path, monkeypatch):
    import base64, os
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "ltm.json"))
    monkeypatch.setenv("UH_LONG_TERM_VECTOR_STORE", str(tmp_path / "vec.json"))
    # L2 加密需主密钥（key_ref: memory-master-key）
    monkeypatch.setenv("UH_MEMORY_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    from kernel.long_term_memory import LongTermMemory

    ltm = LongTermMemory()
    entry = ltm.write("13800138000 家住北京市朝阳区", privacy_level="L2")
    assert entry is not None
    raw = (tmp_path / "ltm.json").read_text(encoding="utf-8")
    # 明文不得落盘
    assert "13800138000" not in raw
    assert "北京市朝阳区" not in raw
    # L2 不进语义索引
    hits = ltm.recall("北京市朝阳区", top_k=5)
    assert all(h.get("privacy_level") != "L2" for h in hits)


# ---------- SEC-05 供应链 ----------
def test_sec05_official_plugins_have_manifests():
    official = ROOT / "examples" / "plugins" / "official"
    dirs = [d for d in official.iterdir() if d.is_dir()]
    assert dirs, "official plugins must exist"
    for d in dirs:
        assert (d / "manifest.yaml").exists(), f"{d.name} missing manifest"


def test_sec05_sbom_generation():
    result = subprocess.run(["node", "scripts/generate-sbom.mjs"], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "SBOM_WRITTEN" in result.stdout
    sbom_path = ROOT / "dist" / "sbom.cdx.json"
    assert sbom_path.exists()
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    assert len(sbom["components"]) >= 5
    # 不得含内嵌凭据
    text = json.dumps(sbom)
    assert "AKIA" not in text


def test_sec05_no_embedded_credentials_in_examples():
    pattern = __import__("re").compile(r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
    for path in (ROOT / "examples").rglob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8", errors="replace")), path


# ---------- R2 审查 P1-5：三类漏出 ----------
def test_sec01_redacted_prefix_does_not_shortcircuit():
    """原文已含 *REDACTED* 时不得跳过熵检测（原第71行短路是逻辑漏洞）。"""
    secret = "Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga"
    out = redact_text(REDACTED + " " + secret)
    assert secret not in out


@pytest.mark.parametrize("payload", [
    '{"dbPassword": "Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga"}',
    '{"authToken": "Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga"}',
    '{"apiKey": "Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga"}',
    'dbPassword: "Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga"',
])
def test_sec01_camelcase_json_keys_are_redacted(payload):
    """camelCase 键（dbPassword/authToken）原不匹配 _SENSITIVE_KEY 边界规则。"""
    out = redact_text(payload)
    assert "Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga" not in out


def test_sec01_sequential_runs_with_spaces_are_redacted():
    """含空白的字母序/数字序串，逐 token 熵检测必然漏过。"""
    out = redact_text("abcdefg hijklmnop qrstuvwxyz 0123456789")
    assert "hijklmnop" not in out
    assert "0123456789" not in out


@pytest.mark.parametrize("text", [
    "the quick brown fox jumps over the lazy dog",
    "configuration-management-system",
    "task manager pipeline",
    "用户请求已完成处理",
])
def test_sec01_natural_language_not_overredacted(text):
    assert redact_text(text) == text


def test_sec01_camelcase_key_normalization():
    from kernel.redaction import redact_value
    for key in ("dbPassword", "authToken", "apiKey", "masterKey", "privateKey"):
        assert redact_value(key, "any-value-here") == REDACTED, key
