# Brief 0006：M1-5 长期记忆（语义召回 + L2/L3 隐私分级 + 无痕 + purge/export）

## 角色与约定
你是实现者（Codex），在 UH! 仓库（Python 3.12 + FastAPI）工作，先读 AGENTS.md。只改代码与测试，**不要 git commit**（DSH 审查后提交）。离线测试，加密密钥经 env 注入测试值，严禁硬编码真实密钥。

## 目标
长期记忆能力：向量写入 + 语义召回，隐私分级（L3 拒入/L2 加密/L1 普通），无痕会话不写长期，purge/export 可验证。与 M1-3 检索构成 RAG + 长期记忆闭环。

## 必读上下文（仓库内）
- AGENTS.md；kernel/retrieval.py（DeterministicEmbedding/LocalVectorStore/ChromaVectorStore，复用向量能力）；kernel/memory.py（M0-4 短期记忆）。
- kernel/tasks.py / kernel/main.py：会话流程，context.memory_recall 控制召回；长期记忆写入点。
- kernel/cli.py：uh CLI（增 memory purge/export 子命令）。
- docs/contracts/errors.md；docs/contracts/openapi.yaml：DELETE /memory/long-term?before=、GET /memory/short-term（purge/export REST 形态参考）。
- v3 §4.6 / §9.4 / §9.7：记忆分级 L0 会话/L1 普通/L2 敏感加密/L3 高敏拒入；本地优先、数据最小化、可删除；E2E-08 无痕会话无长期写入。

## 隐私分级策略（必须遵循，解决 L2 语义泄露风险）
- L3（身份证号/银行卡/密钥/密码等高敏）：**拒绝入库 + 记 WARN**，正则/熵检测识别。
- L2（敏感个人信息）：**字段级加密后入库**（AES-GCM，cryptography 库，密钥经 UH_MEMORY_MASTER_KEY env/钥匙串引用）。**关键策略：L2 加密内容不入语义向量索引（防向量语义泄露），仅加密存储 + 元数据过滤召回；L1 普通内容才做语义向量索引。**（此策略解决任务卡风险注记，DSH 审查时重点核）
- L1（普通）：明文入向量库，语义召回。

## 需求
1. 长期记忆模块（kernel/long_term_memory.py）：向量写入 + 语义召回（复用 M1-3 embedding/store）；写入前经隐私分级。
2. 分级器：L3 拒入+WARN；L2 字段加密（AES-GCM）+ 不入语义索引；L1 语义索引。分级可配/可扩展。
3. 无痕会话：context.memory_recall=false → 不召回长期 + 会话结束不写长期（E2E-08）。
4. purge/export：uh memory purge [--before DATE] / uh memory export（CLI 子命令）+ REST（DELETE /memory/long-term、GET /memory/export）；purge 可验证清空，export 可验证导出（L2 导出需授权/脱敏）。
5. 集成：tasks.py 会话后按分级写长期（跳过无痕）；召回经 context.memory_recall；指标 uh_memory_long_term_entries。
6. 加密密钥经 env/钥匙串引用，严禁硬编码；适配层隔离。

## 测试（必须，离线，测试密钥经 env）
- L3 样本（18 位身份证/银行卡/API key 模式）拒入 + WARN，store 无残留。
- L2 样本加密入库（存储密文 != 明文，可用密钥解密）；**断言 L2 不入语义向量索引**（语义查询不命中 L2 原文）。
- L1 普通写入 + 语义召回命中。
- 无痕（memory_recall=false）会话结束无长期写入（E2E-08）。
- purge 清空可验证；export 导出可验证。
- 不得削弱现有 80 个测试。

## 不动什么
- docs/contracts/ 冻结契约 + ADR；kernel/retrieval.py / memory.py / protocol.py / mcp.py / orchestration.py 既有行为；/health /ready router 既有端点。
- 不新增公开 REST 端点（purge/export 用契约已定义的 /memory 路径形态）。

## 门禁（必须跑并报告）
- python -m pytest -q  （期望全绿）
- node scripts/verify-all.mjs  （期望 VERIFY_ALL_PASSED）

## 报告契约
输出：新增/改动文件清单 + 设计要点（分级器/L2 加密与不入语义索引策略/purge-export）+ pytest 结果 + verify-all 结果 + L3 拒入/L2 加密且不语义索引/无痕不写/purge-export 如何验证。不要提交。
