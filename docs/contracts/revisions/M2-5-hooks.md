# M2-5 Hook 实现对照

> 状态：待评审。

`HookManager` 为每个 Hook 建立有界队列和 worker。EventBus 发布只负责入队，Hook 处理采用 `timeout_ms`，异常、超时和队列满均记录审计事件，不影响主链路。官方 `audit-log` 模板订阅 `kernel.route.decided` 与 `capability.module.invoked`，输出脱敏前的内部审计摘要到插件私有目录。

