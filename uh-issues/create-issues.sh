#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-your-org/uh}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

declare -A MS_TITLE=(
  [MS-0]="MS-0 契约冻结周"
  [MS-1]="MS-1 M0 骨架期：最小闭环"
  [MS-2]="MS-2 M1 能力期：模块拆分"
  [MS-3]="MS-3 M2 插件期：可插拔生态"
  [MS-4]="MS-4 M3 生产期：可运维多形态"
  [MS-5]="MS-5 缓冲与 v0.1.0 发布"
)

declare -A ID2NUM

# 格式：ID|标题|Milestone|标签(空格分隔)|正文文件
TASKS=(
"P0-1|统一错误码事实源|MS-0|type/contract area/kernel P0 size/M|issues/P0-1.md"
"P0-2|四类插件 manifest schema|MS-0|type/contract area/kernel P0 size/M|issues/P0-2.md"
"P0-3|REST→WS 任务流闭环|MS-0|type/contract area/kernel area/ui P0 size/M|issues/P0-3.md"
"P0-4|沙箱分期与平台矩阵|MS-0|type/contract area/kernel P0 size/S|issues/P0-4.md"
"P0-5|部署暴露面与 RBAC 收紧|MS-0|type/contract area/ops P0 size/S|issues/P0-5.md"
"P0-6|机器可读契约与图|MS-0|type/contract type/docs P0 size/M|issues/P0-6.md"
"M0-1|内核骨架：事件接收 + trace_id + 统一日志|MS-1|type/feature area/kernel P1 size/M|issues/M0-1.md"
"M0-2|规则版路由（chat/search/tool）|MS-1|type/feature area/kernel P1 size/M|issues/M0-2.md"
"M0-3|LLM 调用封装：流式、超时重试、Token 统计|MS-1|type/feature area/kernel P1 size/M|issues/M0-3.md"
"M0-4|Redis 短期记忆 + 滑动窗口|MS-1|type/feature area/capability P1 size/M|issues/M0-4.md"
"M0-5|Web UI 最小版：Prompt-bar-first + 流式渲染|MS-1|type/feature area/ui P1 size/M|issues/M0-5.md"
"M0-6|CLI uh dev up/down/logs/reset|MS-1|type/infra area/ops P1 size/M|issues/M0-6.md"
"M1-1|内部 RPC + 事件总线 + 统一信封|MS-2|type/feature area/kernel P1 size/L|issues/M1-1.md"
"M1-2|MCP Host：tools/list / tools/call|MS-2|type/feature area/capability P1 size/M|issues/M1-2.md"
"M1-3|LlamaIndex 独立服务 + 向量库接入|MS-2|type/feature area/capability P1 size/L|issues/M1-3.md"
"M1-4|LangGraph 状态机 + PostgreSQL Checkpoint|MS-2|type/feature area/capability P1 size/L|issues/M1-4.md"
"M1-5|长期记忆写入 + 语义召回|MS-2|type/feature area/capability P1 size/M|issues/M1-5.md"
"M1-6|错误码体系 + 统一响应包裹|MS-2|type/feature area/kernel P1 size/S|issues/M1-6.md"
"M1-7|WebSocket 流式事件|MS-2|type/feature area/kernel area/ui P1 size/M|issues/M1-7.md"
"M1-8|高危工具确认 API 雏形|MS-2|type/security area/kernel P1 size/S|issues/M1-8.md"
"M1-9|事件字典冻结 + 总线映射 + UI 状态机|MS-2|type/contract area/kernel P1 size/M|issues/M1-9.md"
"M2-1|插件驱动：manifest 解析 + 校验 + 六阶段生命周期|MS-3|type/feature area/kernel P1 size/L|issues/M2-1.md"
"M2-2|沙箱最小版：进程隔离 + 权限声明 + 资源上限|MS-3|type/security area/kernel P1 size/L|issues/M2-2.md"
"M2-3|工具插件 Python SDK + 首批 3 个官方工具|MS-3|type/feature area/kernel P1 size/L|issues/M2-3.md"
"M2-4|主题插件 CSS 变量契约 + 一键切换|MS-3|type/feature area/ui P2 size/M|issues/M2-4.md"
"M2-5|钩子插件 + 审计日志示例|MS-3|type/feature area/kernel P2 size/S|issues/M2-5.md"
"M2-6|插件 CLI：install/uninstall/validate/dev|MS-3|type/infra area/ops P1 size/M|issues/M2-6.md"
"M3-1|Docker Compose 编排 + 暴露面收紧|MS-4|type/infra area/ops P1 size/M|issues/M3-1.md"
"M3-2|K8s 清单 + HPA + 最小 RBAC|MS-4|type/infra area/ops P1 size/L|issues/M3-2.md"
"M3-3|Prometheus 指标 + Alertmanager 告警|MS-4|type/infra area/ops P1 size/M|issues/M3-3.md"
"M3-4|OpenTelemetry 链路追踪接入|MS-4|type/infra area/ops P2 size/M|issues/M3-4.md"
"M3-5|CrewAI 按需启停（profile / K8s 0→1→0）|MS-4|type/feature area/capability P1 size/L|issues/M3-5.md"
"M3-6|Desktop 壳（Tauri）+ 托盘 + 全局快捷键|MS-4|type/feature area/ui P2 size/L|issues/M3-6.md"
"M3-7|安全加固：seccomp/cgroups/non-root/只读根|MS-4|type/security area/kernel P1 size/L|issues/M3-7.md"
"M3-8|L2 加密 + 日志脱敏 + 供应链签名扫描|MS-4|type/security area/capability P1 size/M|issues/M3-8.md"
"M3-9|压测 100 并发 + SLO + 成本分层模板|MS-4|type/infra area/ops P1 size/M|issues/M3-9.md"
"R-1|全量回归 + E2E/SBX/SEC 专项|MS-5|type/feature P1 size/L|issues/R-1.md"
"R-2|文档更新 + CHANGELOG + 发布说明|MS-5|type/docs P1 size/M|issues/R-2.md"
"R-3|v0.1.0 发布包（镜像 + .uhpkg + 签名）|MS-5|type/infra P1 size/M|issues/R-3.md"
)

echo "==> 第一轮：创建 issue"
for row in "${TASKS[@]}"; do
  IFS='|' read -r id title ms labels body <<< "$row"
  args=(--repo "$REPO" --title "[$id] $title" --body-file "$body" --milestone "${MS_TITLE[$ms]}")
  for l in $labels; do args+=(--label "$l"); done
  url=$(gh issue create "${args[@]}")
  num="${url##*/}"
  ID2NUM["$id"]="$num"
  echo "  [$id] -> #$num  $title"
done

echo "==> 第二轮：回填依赖编号"
for row in "${TASKS[@]}"; do
  IFS='|' read -r id title ms labels body <<< "$row"
  num="${ID2NUM[$id]}"
  content="$(cat "$body")"
  for k in "${!ID2NUM[@]}"; do
    content="${content//\{\{$k\}\}/#${ID2NUM[$k]}}"
  done
  printf '%s\n' "$content" > /tmp/uh-issue-body.md
  gh issue edit "$num" --repo "$REPO" --body-file /tmp/uh-issue-body.md >/dev/null
  echo "  #$num updated"
done

echo "==> done. 共 ${#ID2NUM[@]} 个 issue。"
