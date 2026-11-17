# ADR-003：错误映射与记忆召回双模式可配置（用户/插件自由切换）

- 状态：**Accepted**（2026-11-17，用户裁决「两个都做，后面由用户和插件实现自由切换」）
- 关联：DeepSeek 审查 P1-3（CAP-4003 语义挪用）/ P1-10（长期记忆跨会话）
- 原则：契约只增不改——新增选项/新码，不删除旧语义；运行时可配置，插件可覆盖。

## 决策一：能力错误码双模式（UH_CAP_ERROR_MODE）
- 背景：UH-CAP-4003 冻结语义=LLM 上游服务错误；但 MCP/编排/模块异常挪用了它。
- 只增新码 **UH-CAP-4004 = 能力模块执行错误**（MCP/编排/插件；detail: module/tool_name/node_id）。UH-CAP-4003 语义保持不变（仍专指 LLM 上游）。
- 配置 UH_CAP_ERROR_MODE：
  - strict（默认）：MCP/编排/模块异常 → UH-CAP-4004（语义正确）。
  - legacy：上述异常 → UH-CAP-4003（向后兼容旧插件/前端文案）。
- LLM 上游错误恒为 UH-CAP-4003（不受模式影响）。
- 插件可经 manifest/上下文覆盖模式；前端按域文案可据 mode 适配。

## 决策二：长期记忆召回范围双模式（UH_MEMORY_SCOPE）
- 背景：长期记忆默认跨会话召回（session_id=None），A 会话记忆可能注入 B 会话。
- 配置 UH_MEMORY_SCOPE：
  - global（默认）：跨会话语义召回（个人全局知识）。
  - session：仅召回本会话记忆（会话隔离）。
- 每请求可经 context.memory_scope 覆盖全局默认；插件可指定。
- 写入始终标注 session_id（两模式共用同一存储）；scope 仅影响召回过滤。

## 后果
1. errors.md 只增 UH-CAP-4004 行（修订对照见 revisions/）；UH-CAP-4003 行不动。
2. module_error_code() 助手按 UH_CAP_ERROR_MODE 返回码；MCP/编排/protocol 模块异常路径改用助手。
3. _long_term_recall 按 UH_MEMORY_SCOPE/context.memory_scope 决定是否按 session_id 过滤。
4. 两模式均默认保守可用（strict + global），用户/插件按需切换，无需改契约。
