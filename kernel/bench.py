"""uh bench：并发压测 + SLO 判定 + 硬件基线（M3-9 / §10.4 / §7.7）。

设计要点：
- 无硬件基线的性能数字不可比 → 每次运行强制采集 CPU/内存/Python/模型/文档规模
- SLO 默认：错误率 <1%、P95 ≤2000ms（可 --slo-p95-ms 覆盖）
- 场景 chat/search/orchestrate/collaborate 对应内核四种 task_type
- 结果写 benchmarks/<scenario>-<concurrency>-<ts>.json，含完整分布与基线
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

SCENARIOS = ("chat", "search", "orchestrate", "collaborate")
DEFAULT_SLO_ERROR_RATE = 0.01
DEFAULT_SLO_P95_MS = 2000.0


@dataclass
class Sample:
    latency_ms: float
    ok: bool
    status: int = 0
    error: str | None = None


@dataclass
class BenchResult:
    scenario: str
    concurrency: int
    requests: int
    samples: list[Sample] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        if not self.samples:
            return 1.0
        return sum(1 for s in self.samples if not s.ok) / len(self.samples)

    def percentile(self, q: float) -> float:
        """线性插值分位数（与 numpy/statistics.quantiles 的 inclusive 口径一致）。

        不用 round(index)：Python 银行家舍入会让偶数样本的 p50 偏向下中位数。
        """
        values = sorted(s.latency_ms for s in self.samples if s.ok)
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        pos = q * (len(values) - 1)
        lower = int(pos)
        upper = min(lower + 1, len(values) - 1)
        frac = pos - lower
        return values[lower] + (values[upper] - values[lower]) * frac

    def summary(self) -> dict[str, Any]:
        ok_lat = [s.latency_ms for s in self.samples if s.ok]
        return {
            "requests": len(self.samples),
            "success": sum(1 for s in self.samples if s.ok),
            "errors": sum(1 for s in self.samples if not s.ok),
            "error_rate": round(self.error_rate, 6),
            "latency_ms": {
                "min": round(min(ok_lat), 2) if ok_lat else 0,
                "mean": round(statistics.fmean(ok_lat), 2) if ok_lat else 0,
                "p50": round(self.percentile(0.50), 2),
                "p95": round(self.percentile(0.95), 2),
                "p99": round(self.percentile(0.99), 2),
                "max": round(max(ok_lat), 2) if ok_lat else 0,
            },
        }


def hardware_baseline() -> dict[str, Any]:
    """硬件与环境基线（风险项：无基线的数字不可比）。"""
    cpu_count = os.cpu_count() or 0
    mem_total_gb = None
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    mem_total_gb = round(int(line.split()[1]) / 1024 / 1024, 2)
                    break
        elif os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            mem_total_gb = round(stat.ullTotalPhys / 1024 ** 3, 2)
    except Exception:
        pass
    docs_dir = Path(__file__).resolve().parents[1] / "docs"
    doc_count = sum(1 for _ in docs_dir.rglob("*.md")) if docs_dir.exists() else 0
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": cpu_count,
        "memory_total_gb": mem_total_gb,
        "llm_provider": os.environ.get("UH_LLM_PROVIDER", "mock"),
        "llm_model": os.environ.get("UH_LLM_MODEL", "unset"),
        "retrieval_backend": os.environ.get("UH_RETRIEVAL_BACKEND", "local"),
        "doc_corpus_md_files": doc_count,
        "auth_required": os.environ.get("UH_AUTH_REQUIRED", "0"),
    }


async def _one_request(client: httpx.AsyncClient, base: str, token: str, scenario: str) -> Sample:
    started = time.perf_counter()
    try:
        # M3 独立复审 P1：token 不得放查询串——Uvicorn access log 默认记录完整 URL
        # 含 query，等于把 Bearer 凭据写进访问日志。统一改用 Authorization 头。
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        created = await client.post(f"{base}/api/v1/sessions", headers=headers)
        if created.status_code >= 400:
            return Sample((time.perf_counter() - started) * 1000, False, created.status_code, "session_create")
        data = created.json().get("data") or {}
        sid = data.get("session_id")
        cid = data.get("client_id", "")
        if not sid:
            return Sample((time.perf_counter() - started) * 1000, False, created.status_code, "no_session_id")
        # 内核 _request_client_id 优先级：X-UH-Client-ID 头 > cookie > query。
        # 压测复用同一 AsyncClient 会共享 cookie jar，导致后续请求携带上一个会话的
        # uh_client_id cookie 而归属校验失败（403）。显式头覆盖 cookie，且每请求独立。
        if cid:
            headers = {**headers, "X-UH-Client-ID": cid}
        sent = await client.post(
            f"{base}/api/v1/sessions/{sid}/messages",
            headers=headers,
            json={"content": f"bench {scenario}", "mode": scenario},
        )
        elapsed = (time.perf_counter() - started) * 1000
        if sent.status_code >= 400:
            return Sample(elapsed, False, sent.status_code, "message_submit")
        return Sample(elapsed, True, sent.status_code)
    except Exception as exc:
        return Sample((time.perf_counter() - started) * 1000, False, 0, type(exc).__name__)


async def run_bench(*, base_url: str, token: str, scenario: str, concurrency: int,
                    requests: int, timeout_s: float = 30.0) -> BenchResult:
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of {SCENARIOS}")
    if concurrency < 1 or requests < 1:
        raise ValueError("concurrency and requests must be positive")
    result = BenchResult(scenario=scenario, concurrency=concurrency, requests=requests)
    limits = httpx.Limits(max_connections=concurrency + 8, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=timeout_s, limits=limits, trust_env=False) as client:
        sem = asyncio.Semaphore(concurrency)

        async def worker() -> None:
            async with sem:
                result.samples.append(await _one_request(client, base_url.rstrip("/"), token, scenario))

        await asyncio.gather(*(worker() for _ in range(requests)))
    return result


def evaluate_slo(result: BenchResult, *, max_error_rate: float = DEFAULT_SLO_ERROR_RATE,
                 max_p95_ms: float = DEFAULT_SLO_P95_MS) -> dict[str, Any]:
    summary = result.summary()
    error_ok = summary["error_rate"] < max_error_rate
    p95 = summary["latency_ms"]["p95"]
    p95_ok = p95 <= max_p95_ms
    return {
        "passed": error_ok and p95_ok,
        "checks": {
            "error_rate": {"value": summary["error_rate"], "limit": max_error_rate, "passed": error_ok},
            "p95_ms": {"value": p95, "limit": max_p95_ms, "passed": p95_ok},
        },
    }


async def fetch_server_baseline(base_url: str, token: str) -> dict[str, Any]:
    """采集**被测服务端**基线（客户端硬件不代表服务端，风险项要求数字可比）。"""
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            params = {"token": token} if token else {}
            health = await client.get(f"{base_url.rstrip('/')}/health", params=params)
            info: dict[str, Any] = {"health_status": health.status_code}
            if health.status_code == 200:
                info["kernel_version"] = (health.json() or {}).get("version")
            metrics = await client.get(f"{base_url.rstrip('/')}/metrics", params=params)
            if metrics.status_code == 200:
                text = metrics.text
                info["metrics_families"] = len({
                    line.split()[0] for line in text.splitlines()
                    if line and not line.startswith("#")
                })
            # 独立审查 P1-2：采集服务端真实硬件（CPU/内存/平台/模型），
            # 否则「双端硬件基线、数字可比」的承诺不成立。
            try:
                hw = await client.get(f"{base_url.rstrip('/')}/api/v1/bench/baseline", params=params)
                if hw.status_code == 200:
                    data = (hw.json() or {}).get("data") or {}
                    for key in ("platform", "machine", "python", "cpu_count",
                                "memory_total_gb", "llm_provider", "llm_model",
                                "retrieval_backend", "doc_corpus_md_files"):
                        if key in data:
                            info[key] = data[key]
                else:
                    info["hardware_unavailable"] = f"HTTP {hw.status_code}"
            except Exception as exc:
                info["hardware_unavailable"] = type(exc).__name__
            return info
    except Exception as exc:
        return {"error": type(exc).__name__}


def write_report(result: BenchResult, slo: dict[str, Any], *, out_dir: Path,
                 server_baseline: dict[str, Any] | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{result.scenario}-{result.concurrency}-{ts}.json"
    payload = {
        "schema": "uh-bench/v1",
        "generated_at": ts,
        "scenario": result.scenario,
        "concurrency": result.concurrency,
        "summary": result.summary(),
        "slo": slo,
        # client_baseline 是压测发起端；server_baseline 才是被测系统（数字可比性依据）
        "client_baseline": hardware_baseline(),
        "server_baseline": server_baseline or {},
        "samples": [{"latency_ms": round(s.latency_ms, 2), "ok": s.ok, "status": s.status, "error": s.error}
                    for s in result.samples],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
