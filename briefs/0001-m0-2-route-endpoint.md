# Brief 0001：修复 M0-2 dev/route 端点测试

## 角色
你是实现者（Codex）。只改代码与测试，**不要 git commit**（编排者 DSH 负责提交）。

## 目标
让 tests/unit/test_router.py 中 3 个失败用例通过，全套件绿。

## 现状
- 仓库：UH!（Python 3.12 + FastAPI），工作目录即仓库根。
- kernel/router.py 规则路由已完成，100 样本分类准确率 100%（test_accuracy_at_least_90 通过）。
- kernel/main.py 新增了 POST /api/v1/dev/route 端点（开发用，不在公开 OpenAPI 契约内），调用 classify 并记 kernel.route.decided 事件。
- 失败用例（症状）：
  1. test_dev_route_endpoint_and_event —— 断言失败（响应/事件不符预期）
  2. test_dev_route_invalid_mode_400 —— 期望 400，实际 422
  3. test_dev_route_fallback_warn —— KeyError: 'task_type'（响应体缺字段）
- 疑似方向：端点内联定义的 RouteRequest 模型 / 错误响应构造 / FastAPI 校验交互。请自行定位根因，最小修复。

## 改什么
- kernel/main.py 的 dev_route 端点（及必要的模型定义位置）。
- 如确属测试断言写错，可修正测试，但不得削弱验收（invalid mode 必须 400 + UH-USR-1003；正常路由返回 task_type/mode/rule/confidence/fallback；fallback 记 route.fallback WARN）。

## 不动什么
- kernel/router.py 的分类规则与准确率（已 100%）。
- kernel/tracing.py、kernel/logging.py、/health、/ready、trace 中间件。
- 公开契约（docs/contracts/）与 OpenAPI/AsyncAPI。
- 不要新增公开端点；dev/route 保持开发用途。

## 门禁（必须跑并报告结果）
- python -m pytest -q  （期望全部通过）

## 报告契约
完成后输出：根因一句话 + 改动文件清单 + pytest 结果摘要（通过数/失败数）。不要提交。
