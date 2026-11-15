import logging

import pytest
from fastapi.testclient import TestClient

from kernel.router import classify, TASK_TYPES
from kernel.main import create_app

# 100 条标注样本（M0-2 验收：准确率 ≥90%）
SAMPLES = [
    # ---- chat ×40 ----
    ("你好", "chat"), ("讲个笑话", "chat"), ("你是谁", "chat"), ("陪我聊天", "chat"),
    ("我喜欢猫", "chat"), ("人生意义是什么", "chat"), ("哈哈哈", "chat"), ("谢谢", "chat"),
    ("再见", "chat"), ("晚安", "chat"), ("我好累", "chat"), ("推荐部电影", "chat"),
    ("唱首歌", "chat"), ("你好吗", "chat"), ("嗯嗯", "chat"), ("好的", "chat"),
    ("明白了", "chat"), ("真不错", "chat"), ("我饿了", "chat"), ("无聊", "chat"),
    ("陪我说说话", "chat"), ("你叫什么名字", "chat"), ("今年多大了", "chat"), ("喜欢什么颜色", "chat"),
    ("给我讲个故事", "chat"), ("安慰我一下", "chat"), ("我失恋了", "chat"), ("开心", "chat"),
    ("难过", "chat"), ("早上好", "chat"), ("中午好", "chat"), ("晚上好", "chat"),
    ("在吗", "chat"), ("忙吗", "chat"), ("睡了吗", "chat"), ("吃了吗", "chat"),
    ("干嘛呢", "chat"), ("想你了", "chat"), ("加油", "chat"), ("好困", "chat"),
    # ---- search ×30 ----
    ("帮我搜索一下 MCP 协议", "search"), ("搜一下今天的新闻", "search"), ("检索相关文档", "search"),
    ("查一下北京天气", "search"), ("查询订单状态", "search"), ("查找附近的餐厅", "search"),
    ("找一下那份资料", "search"), ("最新的 iPhone 价格", "search"), ("多少钱一斤", "search"),
    ("search for python tutorials", "search"), ("look up the definition", "search"), ("google 一下这个错误", "search"),
    ("百度一下", "search"), ("维基百科怎么说", "search"), ("wiki 上查查", "search"),
    ("帮我查一下高铁时刻", "search"), ("搜索最新论文", "search"), ("检索知识库", "search"),
    ("查询汇率", "search"), ("找一下说明书", "search"), ("新闻头条", "search"),
    ("资料汇总", "search"), ("文档在哪", "search"), ("今天天气如何", "search"),
    ("价格对比", "search"), ("search the web", "search"), ("look up flights", "search"),
    ("查找联系人", "search"), ("查询余额", "search"), ("搜一下食谱", "search"),
    # ---- tool ×20 ----
    ("调用 web_search 工具", "tool"), ("执行这段代码", "tool"), ("运行脚本", "tool"),
    ("打开浏览器", "tool"), ("发送邮件给张三", "tool"), ("删除临时文件", "tool"),
    ("创建文件 report.txt", "tool"), ("写入数据库", "tool"), ("计算 123*456", "tool"),
    ("翻译这段话", "tool"), ("跑一下测试", "tool"), ("run the script", "tool"),
    ("execute command", "tool"), ("call the api", "tool"), ("send an email", "tool"),
    ("delete the file", "tool"), ("open the door", "tool"), ("compute the sum", "tool"),
    ("translate to english", "tool"), ("帮我运行一下", "tool"),
    # ---- workflow ×5 ----
    ("制定一个多步计划", "workflow"), ("先检索再总结", "workflow"), ("按步骤执行", "workflow"),
    ("编排一个流程", "workflow"), ("workflow for data pipeline", "workflow"),
    # ---- collaborate ×5 ----
    ("让团队协作完成", "collaborate"), ("多角色讨论一下", "collaborate"), ("分工处理", "collaborate"),
    ("collaborate on this", "collaborate"), ("brainstorm ideas", "collaborate"),
]


def test_sample_count():
    assert len(SAMPLES) == 100


def test_accuracy_at_least_90():
    correct = sum(1 for text, want in SAMPLES if classify(text).task_type == want)
    acc = correct / len(SAMPLES)
    assert acc >= 0.90, f"accuracy {acc:.2%} < 90%"


def test_explicit_mode():
    for t in TASK_TYPES:
        d = classify("随便什么内容", mode=t)
        assert d.task_type == t and d.rule == "explicit" and d.confidence == 1.0


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        classify("x", mode="bogus")


def test_fallback_chat():
    d = classify("嗯嗯好的呀")
    assert d.task_type == "chat" and d.fallback is True


def test_priority_tie_tool_over_search():
    # 含 search 词但 tool 优先级更高
    assert classify("调用 web_search 工具").task_type == "tool"


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_dev_route_endpoint_and_event():
    cap = _Cap()
    logging.getLogger().addHandler(cap)
    client = TestClient(create_app())
    r = client.post("/api/v1/dev/route", json={"content": "帮我搜索一下 MCP 协议", "mode": "auto"})
    assert r.status_code == 200
    assert r.json()["task_type"] == "search"
    events = [getattr(x, "event", "") for x in cap.records]
    assert "kernel.route.decided" in events
    logging.getLogger().removeHandler(cap)


def test_dev_route_invalid_mode_400():
    client = TestClient(create_app())
    r = client.post("/api/v1/dev/route", json={"content": "x", "mode": "bogus"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UH-USR-1003"


def test_dev_route_fallback_warn():
    cap = _Cap()
    logging.getLogger().addHandler(cap)
    client = TestClient(create_app())
    r = client.post("/api/v1/dev/route", json={"content": "嗯嗯", "mode": "auto"})
    assert r.json()["task_type"] == "chat" and r.json()["fallback"] is True
    events = [getattr(x, "event", "") for x in cap.records]
    assert "route.fallback" in events
    logging.getLogger().removeHandler(cap)
