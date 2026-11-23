"""规则版意图路由（M0-2，§4.1）。

M0 用关键词 + 正则 + 启发式把请求归入五类 task_type；
后续里程碑以小模型分类兜底 + 大模型攻坚替换（保留 classify 接口）。
路由判定写入 kernel.route.decided 事件，是 UI Agent Status 与链路追踪起点。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import otel

TASK_TYPES = ("chat", "search", "workflow", "collaborate", "tool")

# 规则按特异性排序；命中多类时取优先级最高者
_RULES: list[tuple[str, str, re.Pattern]] = [
    ("workflow", "workflow.keywords", re.compile(r"(多步|步骤|流程|计划|编排|先.{0,10}再|然后|workflow|plan|pipeline|step[ -]by[ -]step)", re.I)),
    ("collaborate", "collaborate.keywords", re.compile(r"(协作|协同|团队|讨论|分工|多角色|多 ?agent|collaborate|team|debate|brainstorm)", re.I)),
    ("tool", "tool.keywords", re.compile(r"(调用|执行|运行|打开|发送|发邮件|删除|创建文件|写入|计算|翻译|跑一下|run |execute|call |send |delete |open |compute|translate)", re.I)),
    ("search", "search.keywords", re.compile(r"(搜索|搜一下|检索|查一下|查询|查找|找一下|最新|新闻|资料|文档|价格|多少钱|天气|search|look up|find info|google|百度|维基|wiki)", re.I)),
]


@dataclass
class RouteDecision:
    task_type: str
    mode: str
    rule: str
    confidence: float
    fallback: bool = False


def classify(content: str, mode: str = "auto") -> RouteDecision:
    if otel.active():
        with otel.span("router.decide"):
            return _classify(content, mode)
    return _classify(content, mode)


def _classify(content: str, mode: str) -> RouteDecision:
    if mode != "auto":
        if mode not in TASK_TYPES:
            raise ValueError(f"invalid mode: {mode}")
        return RouteDecision(task_type=mode, mode=mode, rule="explicit", confidence=1.0)
    text = content or ""
    for task_type, rule_name, pattern in _RULES:
        if pattern.search(text):
            return RouteDecision(task_type=task_type, mode="auto", rule=rule_name, confidence=0.9)
    # 未识别兜底为 chat，记 WARN（由调用方落日志）
    return RouteDecision(task_type="chat", mode="auto", rule="fallback.chat", confidence=0.5, fallback=True)
