#!/usr/bin/env bash
set -euo pipefail
REPO="${REPO:-your-org/uh}"

ms() {
  gh api "repos/$REPO/milestones" \
    -f title="$1" -f description="$2" --jq '.number' >/dev/null
  echo "milestone: $1"
}

ms "MS-0 契约冻结周"              "P0 全部关闭，docs/contracts/ 评审通过"
ms "MS-1 M0 骨架期：最小闭环"      "chat/search 流式对话，trace_id 可查，路由准确率 ≥90%"
ms "MS-2 M1 能力期：模块拆分"      "RAG + MCP + LangGraph resume + WS 事件，内部协议冻结"
ms "MS-3 M2 插件期：可插拔生态"    "装/卸插件不重启，沙箱拦截演示，SDK 可用"
ms "MS-4 M3 生产期：可运维多形态"  "K8s 单模块故障不中断，100 并发错误率 <1%"
ms "MS-5 缓冲与 v0.1.0 发布"       "回归通过，发布包 + CHANGELOG"

echo "done."
