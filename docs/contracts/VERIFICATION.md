# UH! 契约验证指南

> 一键验证 + 分项排查 + CI + 人工评审要点。机器校验全部可复跑；评审决策需人工拍板。

## 1. 一键验证（推荐）

```bash
node scripts/verify-all.mjs            # fast：离线，秒级（首次自动装 ajv/js-yaml 到 .work/verify-deps）
node scripts/verify-all.mjs --full     # 追加 swagger-cli + @asyncapi/parser 官方校验（需网络）
node scripts/verify-all.mjs --full --remote   # 再追加 git 远端同步核对
```

期望输出：`VERIFY_ALL_PASSED (8 checks, full)`，退出码 0。依赖装在 .work/verify-deps（gitignored，可随时删除，下次自动重建）。

| 项 | 检查 | 证明什么 |
|---|---|---|
| A | manifest 四类 schema：4 正例 + 2 负例 + 入口判别 | P0-2 契约可加载、risk_level 缺失/出网无白名单必拒 |
| B | events schema：18 正例 + 7 负例 | P0-3 事件信封与载荷合法，非法事件必拒 |
| C | 交叉一致性预检 8 类 | 错误码/事件名/枚举/内链/数量/信封/占位符/SBX 跨文档无矛盾 |
| D | OpenAPI 端点覆盖 19/19 | §6.3 全端点 + P0-3 confirm 无遗漏 |
| E | v3 基线待修订项（INFO） | 源 docx 的 5 处冲突标记**预期存在**，评审后按修订对照消除；非失败项 |
| F | swagger-cli validate | OpenAPI 3.1 官方校验通过 |
| G | @asyncapi/parser | AsyncAPI 2.6 零错误（与官方 CLI 同引擎） |
| H | git 远端同步 | 本地 HEAD = origin/main |

## 2. 分项排查

```bash
node scripts/review-precheck.mjs       # 仅交叉一致性（YAML_PATH 可指向 js-yaml 模块路径）
node scripts/validate-contracts.mjs    # 仅 schema 正负例（AJV_PATH 可指向 ajv 模块路径）
npx --yes @apidevtools/swagger-cli validate docs/contracts/openapi.yaml
npx --yes @asyncapi/cli validate docs/contracts/asyncapi.yaml   # 官方 CLI；本机 npx 慢时用 --full 的 parser 等效
```

## 3. CI（GitHub 自动）

.github/workflows/contracts.yml：PR/push 触及 docs/contracts/** 或校验脚本时触发，跑 ajv 正负例 + 预检 + swagger-cli + @asyncapi/cli。在仓库 Actions 标签页看结果；本地全绿 ≈ CI 全绿。

## 4. 人工评审要点（机器查不了的）

1. **决议登记**：errors.md D1~D4、events.md C1~C12、task-flow.md F1~F5、sandbox.md S1~S7、deploy-hardening.md E1~E6——逐条确认接受（尤其 C2 task.created 触发点、C5 确认拒绝→cancelled、S4 Windows 降级、E3 内核复用 scaler SA）。
2. **修订对照**：6 份 R 系列替换文本是否符合意图；评审通过后应用两份 docx 并执行各自的应用核对清单。
3. **基线差异 A1~A5**：A1 检索接口形态（MCP vs 内部 RPC）、A2 无条件重试 vs 幂等约束——M1-1 冻结前裁决，不阻塞 MS-0。
4. **Windows M2 风险披露文案**（sandbox.md §3）：确认对外表述可接受。
5. **INFO 项 E 的 5 处标记**（CAP-50210 五位数×1、LLM-*×3、KRN-1001×1）：是源 docx 待修订项，不是契约缺陷。

## 5. GitHub 侧验证

- Issues #1~#39：依赖段为可点击 #编号 链接（两轮回填，双井号 bug 已修）
- Milestones MS-0~MS-5 / 18 labels 齐全
- Actions：contracts 工作流绿
- 渲染：docs/contracts/diagrams.md 4 图在 GitHub 正常显示（viewscreen iframe）

## 6. 验证记录

- 2026-11-14：ajv 7/7（manifest）+ 25/25（events）；swagger-cli valid；@asyncapi/cli exit 0；Mermaid 4/4 渲染实测
- 2026-11-15：verify-all fast 5/5、full+remote 8/8 复跑全绿
