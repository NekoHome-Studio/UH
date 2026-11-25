"""M3-9 压测：bench 引擎 / SLO 判定 / 硬件基线 / CLI（#36）。"""
import asyncio
import json
from pathlib import Path

import pytest

from kernel.bench import (
    DEFAULT_SLO_ERROR_RATE,
    DEFAULT_SLO_P95_MS,
    SCENARIOS,
    BenchResult,
    Sample,
    evaluate_slo,
    hardware_baseline,
    run_bench,
    write_report,
)

ROOT = Path(__file__).resolve().parents[2]


def _result(latencies, errors=0):
    r = BenchResult(scenario="chat", concurrency=10, requests=len(latencies) + errors)
    r.samples = [Sample(ms, True, 202) for ms in latencies]
    r.samples += [Sample(9999.0, False, 500, "probe") for _ in range(errors)]
    return r


def test_percentiles_are_computed_from_successful_samples():
    r = _result([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000])
    s = r.summary()
    # 线性插值口径：10 样本 p50=(500+600)/2=550，p95=100+0.95*900=955
    assert s["latency_ms"]["p50"] == 550
    assert s["latency_ms"]["p95"] == 955
    assert s["latency_ms"]["min"] == 100
    assert s["error_rate"] == 0.0


def test_error_rate_counts_failed_samples():
    r = _result([100] * 99, errors=1)
    assert r.summary()["error_rate"] == 0.01
    assert r.summary()["errors"] == 1


def test_empty_result_is_total_failure():
    r = BenchResult(scenario="chat", concurrency=1, requests=1)
    assert r.error_rate == 1.0
    assert r.percentile(0.95) == 0.0


def test_slo_passes_when_both_limits_met():
    slo = evaluate_slo(_result([100] * 100))
    assert slo["passed"] is True
    assert slo["checks"]["error_rate"]["limit"] == DEFAULT_SLO_ERROR_RATE
    assert slo["checks"]["p95_ms"]["limit"] == DEFAULT_SLO_P95_MS


def test_slo_fails_on_error_rate_at_limit():
    # 契约是 <1%，恰好 1% 不达标
    slo = evaluate_slo(_result([100] * 99, errors=1))
    assert slo["checks"]["error_rate"]["passed"] is False
    assert slo["passed"] is False


def test_slo_fails_on_p95_over_limit():
    slo = evaluate_slo(_result([5000] * 100))
    assert slo["checks"]["p95_ms"]["passed"] is False
    assert slo["checks"]["error_rate"]["passed"] is True
    assert slo["passed"] is False


def test_slo_limits_are_overridable():
    slo = evaluate_slo(_result([3000] * 100), max_p95_ms=5000.0, max_error_rate=0.5)
    assert slo["passed"] is True


def test_hardware_baseline_has_required_fields():
    base = hardware_baseline()
    for key in ("platform", "machine", "python", "cpu_count", "llm_provider",
                "retrieval_backend", "doc_corpus_md_files"):
        assert key in base, key
    assert base["cpu_count"] >= 1
    assert isinstance(base["doc_corpus_md_files"], int)


def test_report_contains_client_and_server_baseline(tmp_path):
    r = _result([100] * 10)
    slo = evaluate_slo(r)
    path = write_report(r, slo, out_dir=tmp_path, server_baseline={"kernel_version": "0.1.0"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "uh-bench/v1"
    assert payload["client_baseline"]["cpu_count"] >= 1
    assert payload["server_baseline"]["kernel_version"] == "0.1.0"
    assert len(payload["samples"]) == 10
    assert payload["slo"]["passed"] is True


def test_run_bench_rejects_invalid_arguments():
    with pytest.raises(ValueError):
        asyncio.run(run_bench(base_url="http://x", token="", scenario="nope", concurrency=1, requests=1))
    with pytest.raises(ValueError):
        asyncio.run(run_bench(base_url="http://x", token="", scenario="chat", concurrency=0, requests=1))
    with pytest.raises(ValueError):
        asyncio.run(run_bench(base_url="http://x", token="", scenario="chat", concurrency=1, requests=0))


def test_scenarios_match_kernel_task_types():
    assert set(SCENARIOS) == {"chat", "search", "orchestrate", "collaborate"}


def test_cli_bench_subcommand_is_registered():
    from kernel.cli import main
    # 无 action 时打印帮助并返回 0（不执行压测）
    assert main(["bench"]) == 0


def test_cost_tier_templates_exist_and_differ():
    tiers = ROOT / "deploy" / "cost-tiers"
    names = ["light", "standard", "enterprise"]
    configs = {}
    for name in names:
        path = tiers / f"{name}.env.example"
        assert path.exists(), f"missing tier {name}"
        text = path.read_text(encoding="utf-8")
        assert "UH_LLM_MODEL" in text
        assert "UH_RETRIEVAL_BACKEND" in text
        configs[name] = text
    # 三档必须实质不同（否则分层无意义）
    assert len(set(configs.values())) == 3
    # 不得含明文凭据
    for text in configs.values():
        assert "AKIA" not in text
        assert "-----BEGIN" not in text
