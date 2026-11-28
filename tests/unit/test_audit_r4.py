"""2026-11-28 审计第 2 轮剩余 5 项：回收 / rate_limit / 指标抓取 / UI routing / worker 监督。"""
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


# ---------- #1 任务与映射状态必须有回收 ----------
def test_task_manager_reaps_terminal_tasks(monkeypatch):
    """终态任务超过上限时淘汰最旧的（原实现只增不减）。"""
    import os as _os

    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    monkeypatch.setenv("UH_AUTH_REQUIRED", "0")
    monkeypatch.setenv("UH_MAX_TERMINAL_TASKS", "3")
    from kernel.main import create_app

    app = create_app()
    tm = app.state.tasks
    assert tm.max_terminal_tasks == 3
    for i in range(6):
        tid = f"uh_task_probe{i:019d}"
        tm.tasks[tid] = {"task_id": tid, "status": "finished", "event_buffer": [], "seq": 0,
                         "session_id": "uh_ses_x", "trace_id": "uh_trc_x", "queue": None,
                         "subscribers": set()}
        tm._terminal_order.append(tid)
        tm._reap_terminal_tasks()
    assert len([t for t in tm.tasks.values() if t.get("status") == "finished"]) <= 3


def test_event_mapper_forget_task_frees_state():
    from kernel.event_mapping import EventMapper
    from kernel.protocol import Envelope

    mapper = EventMapper()
    env = Envelope(action="mcp.tool.call", payload={
        "call_id": "c1", "tool_name": "x", "args_summary": "{}",
        "risk_level": "read-only", "requires_confirmation": False,
    }, trace_id="uh_trc_x", span_id="sp_1")
    mapper.map(env, task_id="uh_task_t1")
    assert mapper._external and mapper._links
    mapper.forget_task("uh_task_t1")
    assert not mapper._external and not mapper._links and not mapper._next_ids


# ---------- #2 Hook rate_limit 必须真正生效 ----------
def test_hook_rate_limit_is_enforced(monkeypatch):
    """契约声明了 rate_limit，运行时必须用它限流（原实现完全忽略）。"""
    import asyncio

    from kernel.hooks import HookManager
    from kernel.protocol import Envelope, EventBus

    async def scenario():
        bus = EventBus()
        rt = HookManager(bus, queue_size=100)
        seen = []

        async def handler(env):
            seen.append(env)

        rt.register("h1", ["memory.record.write"], handler, rate_limit=2)
        for i in range(6):
            await bus.publish("memory.record.write", {"session_id": "uh_ses_x", "role": "user"})
        await asyncio.sleep(0.1)
        return len(seen)

    delivered = asyncio.run(scenario())
    assert delivered <= 2, f"rate_limit=2 却投递了 {delivered} 次"


def test_hook_rejects_invalid_rate_limit():
    import asyncio

    from kernel.hooks import HookManager
    from kernel.protocol import EventBus

    async def handler(env):
        pass

    bus = EventBus()
    rt = HookManager(bus)
    with pytest.raises(ValueError):
        rt.register("bad", ["memory.record.write"], handler, rate_limit=0)


# ---------- #3 K8s 指标抓取必须接通 ----------
def test_metrics_service_has_scrape_annotations():
    docs = [d for d in yaml.safe_load_all(
        (ROOT / "deploy" / "k8s" / "observability-k8s.yaml").read_text(encoding="utf-8")) if d]
    svc = next(d for d in docs if d.get("kind") == "Service"
               and d["metadata"]["name"] == "uh-kernel-metrics")
    ann = svc["metadata"].get("annotations") or {}
    assert ann.get("prometheus.io/scrape") == "true"
    assert ann.get("prometheus.io/port") == "9090"
    assert ann.get("prometheus.io/path") == "/metrics"


# ---------- #4 工具任务不得停在 routing ----------
def _sm():
    import subprocess
    script = (
        "const sm=require('./ui/state-machine.js');"
        "let s=sm.transition(sm.initialState(),'task.created');"
        "const out={routing:s.state};"
        "out.tool=sm.transition(s,{event:'tool.call',payload:{}}).state;"
        "out.delta=sm.transition(s,{event:'delta',payload:{}}).state;"
        "console.log(JSON.stringify(out));"
    )
    r = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_tool_task_leaves_routing_state():
    """工具任务不产生 router 的 step 事件，原逻辑会永远停在 routing。"""
    out = _sm()
    assert out["routing"] == "routing"
    assert out["tool"] == "acting", f"tool.call 未推进状态: {out}"
    assert out["delta"] == "streaming", f"delta 未推进状态: {out}"


# ---------- #5 worker 异常退出必须被替换 ----------
def test_fork_supervisor_respawns_workers():
    """原实现 waitpid 一次就返回，worker 崩溃后不替换。"""
    import inspect

    from kernel import serve

    src = inspect.getsource(serve._fork_workers)
    assert "os.wait()" in src, "应为循环 wait 而非一次性 waitpid"
    assert "spawn()" in src
    assert "while not stopping" in src, "缺监督循环"
