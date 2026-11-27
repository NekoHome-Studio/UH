"""R-1 覆盖率补齐：M3 新增模块的未覆盖分支（#37 要求新增代码 ≥80%）。"""
import asyncio
import json
from pathlib import Path

import pytest

from kernel.session_registry import SessionRegistry, create_session_registry


# ---------- SessionRegistry 本地回退全分支 ----------
def test_registry_local_register_owner_exists_forget():
    async def scenario():
        reg = SessionRegistry()
        assert reg.shared is False
        assert await reg.owner("uh_ses_x") is None
        assert await reg.exists("uh_ses_x") is False
        await reg.register("uh_ses_x", "owner-1")
        assert await reg.owner("uh_ses_x") == "owner-1"
        assert await reg.exists("uh_ses_x") is True
        await reg.forget("uh_ses_x")
        assert await reg.exists("uh_ses_x") is False
        # forget 不存在的 key 不应抛
        await reg.forget("uh_ses_absent")

    asyncio.run(scenario())


def test_registry_local_ttl_expiry(monkeypatch):
    import kernel.session_registry as sr

    async def scenario():
        monkeypatch.setattr(sr, "_TTL_S", 0)
        reg = SessionRegistry()
        await reg.register("uh_ses_ttl", "owner")
        await asyncio.sleep(0.01)
        assert await reg.owner("uh_ses_ttl") is None

    asyncio.run(scenario())


class _FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value.encode() if isinstance(value, str) else value

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)


def test_registry_redis_backend_branches():
    async def scenario():
        fake = _FakeRedis()
        reg = SessionRegistry(fake)
        assert reg.shared is True
        assert await reg.owner("uh_ses_r") is None
        await reg.register("uh_ses_r", "owner-r")
        # bytes 解码分支
        assert await reg.owner("uh_ses_r") == "owner-r"
        await reg.forget("uh_ses_r")
        assert await reg.exists("uh_ses_r") is False
        # str 值分支
        fake.data["uh:session:uh_ses_s"] = "plain"
        assert await reg.owner("uh_ses_s") == "plain"

    asyncio.run(scenario())


def test_create_registry_excludes_fakeredis():
    class _Backend:
        class redis:
            pass

    # 无 backend → 本地
    assert create_session_registry(None).shared is False
    # 有 client 属性但非 fakeredis → 共享
    class _Real:
        redis = _FakeRedis()

    assert create_session_registry(_Real()).shared is True
    # fakeredis 必须排除
    try:
        import fakeredis.aioredis as fake

        class _Fake:
            redis = fake.FakeRedis()

        assert create_session_registry(_Fake()).shared is False
    except ImportError:
        pytest.skip("fakeredis not installed")


# ---------- bench 未覆盖分支 ----------
def test_bench_single_sample_and_percentile_edges():
    from kernel.bench import BenchResult, Sample, evaluate_slo

    r = BenchResult(scenario="chat", concurrency=1, requests=1)
    r.samples = [Sample(123.4, True, 202)]
    assert r.percentile(0.5) == 123.4
    assert r.percentile(0.99) == 123.4
    s = r.summary()
    assert s["latency_ms"]["min"] == 123.4
    assert s["latency_ms"]["max"] == 123.4
    # 全失败样本 → 分位数为 0，均值 0
    r2 = BenchResult(scenario="chat", concurrency=1, requests=1)
    r2.samples = [Sample(999.0, False, 500, "x")]
    assert r2.percentile(0.95) == 0.0
    assert r2.summary()["latency_ms"]["mean"] == 0
    assert evaluate_slo(r2)["passed"] is False


def test_bench_server_baseline_unreachable():
    from kernel.bench import fetch_server_baseline

    info = asyncio.run(fetch_server_baseline("http://127.0.0.1:1", ""))
    assert "error" in info


def test_bench_report_without_server_baseline(tmp_path):
    from kernel.bench import BenchResult, Sample, evaluate_slo, write_report

    r = BenchResult(scenario="search", concurrency=2, requests=2)
    r.samples = [Sample(10.0, True, 202), Sample(20.0, True, 202)]
    path = write_report(r, evaluate_slo(r), out_dir=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["server_baseline"] == {}
    assert payload["scenario"] == "search"


# ---------- redaction 边界 ----------
def test_redaction_non_string_and_nested():
    from kernel.redaction import REDACTED, redact_body, redact_fields, redact_text, redact_value

    assert redact_text("") == ""
    assert redact_text(None) is None
    assert redact_body(123) == 123
    # 嵌套 dict / list
    out = redact_value("outer", {"api_key": "k", "items": [{"token": "t"}, "plain"]})
    assert out["api_key"] == REDACTED
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1] == "plain"
    # 非字符串标量原样
    assert redact_value("count", 42) == 42
    # 短 token 不触发熵检测
    assert redact_text("short") == "short"
    # 含空白的高熵串不触发
    assert " " in redact_text("Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga and more words here")


# ---------- execution_gate 统计 ----------
def test_gate_stats_shape():
    from kernel.execution_gate import ExecutionGate

    gate = ExecutionGate(limit=3)
    stats = gate.stats()
    assert set(stats) == {"limit", "running", "queued", "completed"}
    assert stats["limit"] == 3


# ---------- serve 入口 ----------
def test_serve_single_worker_runs_server(monkeypatch):
    """单 worker 走 uvicorn.Server(config).run()。"""
    import kernel.serve as serve

    called = {}

    class FakeServer:
        def __init__(self, config):
            called["config"] = config

        def run(self, sockets=None):
            called["sockets"] = sockets

    monkeypatch.setattr(serve.uvicorn, "Server", FakeServer)
    monkeypatch.setenv("UH_WORKERS", "1")
    monkeypatch.setenv("UH_HOST", "0.0.0.0")
    monkeypatch.setenv("UH_PORT", "9999")
    assert serve.main() == 0
    assert called["config"].port == 9999
    assert called["config"].host == "0.0.0.0"
    assert called["sockets"] is None


def test_serve_multi_worker_forks(monkeypatch):
    """UH_WORKERS>1 且有 fork 时必须真 fork（uvicorn 的 workers 参数实测不生效）。"""
    import kernel.serve as serve

    calls = {}

    def fake_fork(config, count):
        calls["count"] = count
        calls["port"] = config.port
        return 0

    monkeypatch.setattr(serve, "_fork_workers", fake_fork)
    monkeypatch.setattr(serve.os, "fork", lambda: 0, raising=False)
    monkeypatch.setenv("UH_WORKERS", "4")
    monkeypatch.setenv("UH_PORT", "9999")
    assert serve.main() == 0
    assert calls["count"] == 4
    assert calls["port"] == 9999


def test_serve_multi_worker_degrades_without_fork(monkeypatch, capsys):
    """无 fork（Windows）时退化为单进程并明确提示，不静默按 workers 假装多进程。"""
    import kernel.serve as serve

    started = {}

    class FakeServer:
        def __init__(self, config):
            pass

        def run(self, sockets=None):
            started["ran"] = True

    monkeypatch.setattr(serve.uvicorn, "Server", FakeServer)
    monkeypatch.delattr(serve.os, "fork", raising=False)
    monkeypatch.setattr(serve, "_fork_workers", lambda *a: started.setdefault("forged", True))
    monkeypatch.setenv("UH_WORKERS", "4")
    assert serve.main() == 0
    assert started.get("ran") is True
    assert "forged" not in started
    assert "fork" in capsys.readouterr().err


# ---------- bench 网络路径（本地 ASGI 直连，无需真服务器） ----------
def test_bench_run_bench_against_local_app(tmp_path):
    """覆盖 _one_request / run_bench / fetch_server_baseline 的成功与失败分支。"""
    import httpx
    from kernel.bench import fetch_server_baseline, run_bench, write_report, evaluate_slo
    from kernel.main import create_app

    app = create_app()

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        # 直接注入 transport：monkeypatch httpx.AsyncClient 构造
        orig_init = httpx.AsyncClient.__init__

        def patched(self, *a, **kw):
            kw["transport"] = transport
            kw.pop("limits", None)
            orig_init(self, *a, **kw)

        httpx.AsyncClient.__init__ = patched
        try:
            result = await run_bench(base_url="http://testserver", token="", scenario="chat",
                                     concurrency=2, requests=4, timeout_s=10.0)
            baseline = await fetch_server_baseline("http://testserver", "")
        finally:
            httpx.AsyncClient.__init__ = orig_init
        return result, baseline

    result, baseline = asyncio.run(scenario())
    assert len(result.samples) == 4
    assert result.summary()["requests"] == 4
    assert baseline.get("health_status") == 200
    path = write_report(result, evaluate_slo(result), out_dir=tmp_path, server_baseline=baseline)
    assert path.exists()


def test_bench_one_request_error_branches():
    """覆盖 session_create 失败 / no_session_id / message_submit 失败 / 异常。"""
    import httpx
    from kernel.bench import _one_request

    class _Resp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload or {}

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, mode):
            self.mode = mode

        async def post(self, url, **kw):
            if self.mode == "session_fail":
                return _Resp(500)
            if self.mode == "no_sid":
                return _Resp(201, {"data": {}})
            if self.mode == "msg_fail":
                if "messages" in url:
                    return _Resp(403)
                return _Resp(201, {"data": {"session_id": "uh_ses_a", "client_id": "cid"}})
            if self.mode == "raise":
                raise RuntimeError("probe")
            return _Resp(201, {"data": {"session_id": "uh_ses_a", "client_id": ""}})

    async def scenario():
        out = {}
        for mode in ("session_fail", "no_sid", "msg_fail", "raise", "ok"):
            sample = await _one_request(_Client(mode), "http://x", "", "chat")
            out[mode] = sample
        return out

    out = asyncio.run(scenario())
    assert out["session_fail"].error == "session_create"
    assert out["no_sid"].error == "no_session_id"
    assert out["msg_fail"].error == "message_submit"
    assert out["raise"].error == "RuntimeError"
    assert out["ok"].ok is True


def test_bench_server_baseline_includes_hardware():
    """独立审查 P1-2：server_baseline 曾只有 health/version/metrics，无硬件字段，
    使 RELEASE 的「双端硬件基线、数字可比」承诺不成立。"""
    import asyncio
    import httpx
    from kernel.bench import fetch_server_baseline
    from kernel.main import create_app

    app = create_app()

    async def scenario():
        tr = httpx.ASGITransport(app=app)
        orig = httpx.AsyncClient.__init__

        def patched(self, *a, **k):
            k["transport"] = tr
            k.pop("limits", None)
            orig(self, *a, **k)

        httpx.AsyncClient.__init__ = patched
        try:
            return await fetch_server_baseline("http://testserver", "")
        finally:
            httpx.AsyncClient.__init__ = orig

    info = asyncio.run(scenario())
    # 服务端硬件字段必须真实采集，而非仅客户端侧
    for key in ("platform", "cpu_count", "memory_total_gb", "llm_provider", "retrieval_backend"):
        assert key in info, f"server_baseline 缺 {key}"
    assert info["cpu_count"] >= 1
    assert "hardware_unavailable" not in info
