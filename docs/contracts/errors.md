# UH! 错误码契约 · 唯一事实源

> 版本：v0.1.0（契约已冻结）
> 状态：frozen —— MS-0 评审通过（2026-11-15），tag contract-v0.1.0；只增不改，语义变更需新编号 + 评审
> 对应任务：P0-1 · 上游文档：《UH!架构设计文档_v0.1.0》§4.1 / §6.2 / §6.7 / §11.2
> 上游基线：2026-11-14 纳入《Ultra-Harness技术文档_整合版_v3（润色校对）》为主基线（契约章节号一致，决议不变）；v3 新笔误 UH-CAP-50210 与三段式缺连字符见修订对照 R8/R9
> 效力：自评审通过起，本文件是错误码的唯一事实源。架构文档、前端文案、插件 SDK、告警规则与本文件冲突时，以本文件为准。

## 1. 域定义（固定四类，不增设第五域）

| 域 | 含义 | 编号段 | 前端文案策略 |
|---|---|---|---|
| USR | 请求参数 / 鉴权 / 限流 | 1xxx | 用户可自助修正：重新登录、检查输入、稍后重试 |
| KRN | 内核路由 / 插件加载 / 权限 | 2xxx、5xxx | 系统侧故障：展示 trace_id 供反馈，不暴露内部路径 |
| CAP | 能力层执行（编排 / 检索 / 协作 / 工具 / 上游 LLM） | 3xxx 检索、4xxx 上游 LLM、5xxx 执行 | 能力降级或可重试提示，按 detail.module 细分 |
| MEM | 记忆层读写 | 7xxx | 会话过期类提示，引导新开会话 |

决议 D1：上游 LLM 错误不设独立域，统一归入 CAP-4xxx（见 §8）。前端按域（USR/KRN/CAP/MEM）做差异化文案即可覆盖 LLM 场景。

## 2. 有效错误码全表（13 个）

| 错误码 | 含义 | HTTP | retryable | detail 要求 |
|---|---|---|---|---|
| UH-USR-1001 | 未认证 | 401 | false | — |
| UH-USR-1002 | 权限不足 | 403 | false | 高危确认拒绝：confirmation_required、tool_name、risk_level；越权订阅：resource、task_id |
| UH-USR-1003 | 参数 / ID 校验失败 | 400 | false | field、reason |
| UH-USR-1004 | 请求限流 | 429 | true | retry_after_ms 必填（顶层），与 Retry-After 头一致 |
| UH-KRN-2001 | 路由 / 意图识别失败 | 500 | false | — |
| UH-KRN-2010 | 插件越权访问（沙箱拦截） | 403 | false | plugin、resource、violation；必关联审计事件 |
| UH-KRN-5001 | 内核未捕获异常 | 500 | false | — |
| UH-CAP-3001 | 检索无结果（降级信号） | 200 | false | query（可选） |
| UH-CAP-4001 | LLM Token 超限 | 400 | false | model、max_tokens、requested_tokens |
| UH-CAP-4002 | LLM 上游限流 | 429 | true | model、provider；retry_after_ms 必填 |
| UH-CAP-4003 | LLM 上游服务错误 | 502 | true | model、provider、upstream_status |
| UH-CAP-5021 | 能力模块执行超时 | 504 | true（受幂等约束） | module 必填 + timeout_ms，见 §3 |
| UH-MEM-7001 | 短期记忆过期 | 410 | false | session_id |

注：
- HTTP 状态映射与 retryable 默认值为本契约新增约定（原文档未定义）；retryable 最终由服务端判定，客户端仅在 true 且 retry_after_ms 通过后重试。
- UH-KRN-2001 语义边界：auto 模式未识别时兜底 chat 并记 WARN（M0-2 约定），不产出错误码；2001 仅用于路由内部故障或无法兜底的情形。
- UH-CAP-3001 为降级信号：chat/search 链路 HTTP 200，data 正常返回，meta.degraded=true，error 携带 CAP-3001 说明降级原因，不阻断对话（§4.3）。
- UH-CAP-5021 的 retryable=true 仅表示"可重试"；是否自动重试受幂等约束：幂等工具才可自动重试，irreversible 禁止（§9.6）。

## 3. UH-CAP-5021 detail.module 规范

保留统一超时码（不拆新码），来源以 detail.module 必填区分：

| module | 来源 | 附加 detail 字段 |
|---|---|---|
| tool | MCP 工具调用（§4.5 / §9.6） | tool_name、timeout_ms |
| orchestration | 编排状态机（§4.2） | task_id、node_id、timeout_ms |
| collaboration | 协作模块（§4.4） | task_id、crew、timeout_ms |
| retrieval | 检索模块（预留，§4.3） | query、timeout_ms |

示例（tool 超时）：

```json
{
  "data": null,
  "meta": { "trace_id": "uh_trc_7f3a9c..." },
  "error": {
    "code": "UH-CAP-5021",
    "message": "工具 web_search 调用超时 (5000ms)",
    "retryable": true,
    "retry_after_ms": 2000,
    "detail": { "module": "tool", "tool_name": "web_search", "timeout_ms": 5000 }
  }
}
```

## 4. 错误响应结构（与 §6.2 / §6.7 一致）

```json
{
  "data": null,
  "meta": { "trace_id": "uh_trc_..." },
  "error": {
    "code": "UH-<域>-<编号>",
    "message": "人类可读描述，不含内部路径与策略细节",
    "retryable": false,
    "retry_after_ms": 0,
    "detail": {}
  }
}
```

- 所有 REST 响应统一 {data, meta, error} 包裹；meta.trace_id 必带。
- 成功：error 为 null；降级：HTTP 200 + data 可用 + meta.degraded=true + error 携带降级原因。
- 对消息不泄露策略与内部路径，排查信息通过 trace_id 在日志关联（§6.7）。

## 5. Deprecated 对照表（4 个）

| 旧码 | 替代码 | 原出处 | 废弃原因 |
|---|---|---|---|
| UH-KRN-1001 | UH-KRN-2001 | §11.2 速查表 | 与 §6.7 的 KRN-2001 同义二义，统一为 2001（决议 D2） |
| UH-LLM-4001 | UH-CAP-4001 | §11.2 速查表 | LLM 域并入 CAP（决议 D1） |
| UH-LLM-4002 | UH-CAP-4002 | §11.2 速查表 | 同上 |
| UH-LLM-5001 | UH-CAP-4003 | §11.2 速查表 | 同上 |

切换策略：v0.1.0 尚未发布、无存量客户端，自契约冻结起服务端一律产出新码，旧码停止产出；本表仅作迁移对照与历史检索。旧码最早移除版本：v0.2.0（对齐 §6.8 弃用流程：提前一个版本 Deprecation 提示 + 变更日志标注）。

## 6. 治理规则

1. 错误码只增不改；语义变更需新编号，旧编号标记 deprecated。
2. 新增 / 变更流程：PR 修改本文件 → 内核负责人评审 → 同步修订引用章节（§4.1 / §6.7 / §11.2 等）→ 合并即生效。
3. 编号按段内递增分配，不复用已废弃编号。
4. 维护人：内核负责人（单人维护，杜绝第二事实源）。
5. 本文件冻结后，CI 应对代码与文档中的错误码做一致性校验（配合 P0-6 机器可读契约）。

## 7. 原文档引用点盘点（28 处，全部处置）

| 位置 | 涉及码 | 处置 |
|---|---|---|
| §4.1 路由表（3 处 CAP-5021） | CAP-5021 | 码值保留，表下追加 detail.module 约定（修订 R1） |
| §4.3 检索降级 | CAP-3001 | 不变 |
| §6.7 域表与示例 | USR-1003、KRN-2001、CAP-5021、MEM-7001 | KRN-2001 确认为规范码；CAP 域含义扩至上游 LLM；示例 detail 补 module（修订 R3） |
| §5.5 / §9.2 凭据与归属 | USR-1001、USR-1002、KRN-2010 | 不变 |
| §9.3 沙箱（KRN-2010×2、CAP-5021×1） | KRN-2010、CAP-5021 | 不变；超时行可选加注 detail.module=tool |
| §9.6 工具安全 | USR-1002、CAP-5021 | 不变 |
| §10.2 测试清单 | KRN-2010、CAP-5021、USR-1002 | 不变 |
| §11.2 速查表 | KRN-1001、LLM-4001/4002/5001 及其余 | 冲突行整表替换（修订 R4） |
| §6.2 限流行为 | （无码） | 补 UH-USR-1004（修订 R2，决议 D4） |

逐字替换文本见 revisions/P0-1-修订对照.md。

## 8. 决议记录

| 编号 | 决议 | 理由 |
|---|---|---|
| D1 | LLM 归入 CAP，CAP-4xxx 段给上游 LLM；LLM-4001→CAP-4001、LLM-4002→CAP-4002、LLM-5001→CAP-4003 | §6.7 域固定四类是规范表述；LLM 是能力层公共依赖而非独立链路，归 CAP 后前端文案策略无需第五分支；4xxx 段内按登记顺序递增，不保留旧域编号尾部 |
| D2 | 路由失败统一 UH-KRN-2001，废弃 UH-KRN-1001 | §6.7 是规范章节（§11.2 自述"完整体系见 6.7"），其示例 2001 胜出；2001 与 2010 同处 KRN-2xxx 段，编号语义一致 |
| D3 | CAP-5021 保留单码，detail.module 必填（tool / orchestration / collaboration，retrieval 预留） | 拆新码会破坏 §4.1 / §9.3 / §9.6 / §10.2 共 8 处引用；单码 + 结构化 detail 既消除语义过粗，又保持"只增不改" |
| D4 | 新增 UH-USR-1004 请求限流 | §6.2 已定义 429 + Retry-After 行为但无对应码，前端按域做文案需要落码；属"只增"允许的新增 |
