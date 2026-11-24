# 安全专项 SEC-01~05（M3-8）

> 契约章节：§9.4（敏感分级）/ §9.9（日志脱敏）/ §10.2（供应链）
> 实现：`kernel/redaction.py`（脱敏）、`kernel/long_term_memory.py`（L2 加密）、`scripts/generate-sbom.mjs`（SBOM）
> 测试：`tests/unit/test_security_m3.py`（14 例）；门禁：verify-all **N 项**

## SEC-01 日志不含凭据
两层防护（`kernel/redaction.py`）：
1. **正则模式**：Bearer/Basic token、URL 内嵌凭据 `://user:pass@`、私钥块、AWS AK、JWT、`key=value` 形式
2. **熵检测兜底**：长度 24~512、无空白、字符集受限且 **Shannon 熵 ≥ 4.2** 的 token → `*REDACTED*`

键名命中即整体脱敏：`password|passwd|pwd|secret|token|api_key|access_key|private_key|authorization|cookie|session_id|master_key|credential`。
**`trace_id`/`span_id` 永不脱敏**（风险项要求保留排查链路）。

接入点：`JsonFormatter.format` → `redact_fields`，所有 `log_event` 自动生效。

## SEC-02 正文不落原文
`redact_body()` = 脱敏 + 裁剪到 **160 字符**（超出标记 `…[+N chars]`）。
作用于 `content|body|text|result|preview|content_preview` 键；其他键只脱敏不裁剪（保留诊断信息）。

## SEC-03 L2 字段级加密（key_ref: memory-master-key）
- `UH_MEMORY_MASTER_KEY`（base64 32 字节）→ AES-GCM 加密 L2 内容
- **明文不落盘**：`long_term_memory.json` 中 L2 条目只存密文 + 可过滤元数据
- **L2 不进语义索引**：`recall()` 结果不含 L2（避免向量库泄露语义）
- 无主密钥时写 L2 抛 `MemoryKeyError`（fail-closed，不明文降级）

## SEC-04 WS 跨归属订阅被拒
`owner_matches` 双因子（token + client_id）；跨归属订阅 → `UH-USR-1002` + close **4403**。
测试见 `test_websocket.py::test_websocket_ownership_requires_token_and_client_id`。

## SEC-05 插件供应链
`scripts/generate-sbom.mjs`：
- 生成 **CycloneDX 1.5** SBOM（`dist/sbom.cdx.json`），涵盖 pyproject 主依赖 + optional extras + npm
- 校验官方插件样例：manifest.yaml 存在、源码无内嵌凭据（AKIA / 私钥块）
- 镜像通过 OCI `LABEL org.opencontainers.image.sbom` 声明 SBOM 位置

## 运维命令
```bash
node scripts/generate-sbom.mjs          # 生成 SBOM + 供应链校验
python -m pytest tests/unit/test_security_m3.py -q   # SEC-01~05
node scripts/verify-all.mjs --full      # 含 N 项
```

## 已知边界
- 熵阈值 4.2 对短凭据（<24 字符）不生效，依赖正则覆盖；如需更强可下调长度阈值（有误杀风险）。
- SBOM 为静态解析 pyproject 声明版本，非实际安装版本；生产建议用 `pip freeze` 或 `syft` 生成精确清单。
- L2 加密依赖主密钥可轮换性未实现（M3+ 可加 key rotation）。
