#!/usr/bin/env bash
set -euo pipefail
REPO="${REPO:-your-org/uh}"

create() { gh label create "$1" --color "$2" --description "${3:-}" --repo "$REPO" --force >/dev/null && echo "label: $1"; }

create "type/contract" 5319e7 "契约、schema、接口定义"
create "type/feature"  0e8a16 "新功能"
create "type/security" b60205 "安全加固"
create "type/infra"    1d76db "部署、CI、监控"
create "type/docs"     c5def5 "文档"
create "area/kernel"     fbca04 "路由、插件驱动、总线、沙箱"
create "area/capability" d4c5f9 "检索、编排、协作、记忆"
create "area/ui"         f9d0c4 "Web / Desktop / 主题"
create "area/ops"        bfd4d7 "CLI、Compose、K8s、监控"
create "P0" b60205 "阻塞，必须本周"
create "P1" d93f0b "本里程碑必须"
create "P2" fef2c0 "可延后"
create "size/S" ededed "≤1 天"
create "size/M" d4c5f9 "2-3 天"
create "size/L" 5319e7 "4-5 天，需拆"
create "blocked"      000000 "有依赖未关闭"
create "needs-review" 7057ff "待契约评审"
create "flaky"        e99695 "测试不稳定，限期修"

echo "done."
