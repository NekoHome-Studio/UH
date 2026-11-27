"""M3 独立复审 P1：K8s 默认追踪链路在发送端与 Collector 两处断开。"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _docs():
    path = ROOT / "deploy" / "k8s" / "observability-k8s.yaml"
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _collector():
    return next(d for d in _docs() if d.get("kind") == "Deployment"
                and d["metadata"]["name"] == "otel-collector")


def _config():
    cm = next(d for d in _docs() if d.get("kind") == "ConfigMap"
              and d["metadata"]["name"] == "otel-collector-config")
    return yaml.safe_load(cm["data"]["config.yaml"])


def test_collector_image_is_pinned():
    """latest 浮动不可复现，且不同版本移除过 exporter。"""
    image = _collector()["spec"]["template"]["spec"]["containers"][0]["image"]
    assert not image.endswith(":latest"), image
    assert re.search(r":\d+\.\d+\.\d+$", image), f"镜像版本未固定: {image}"


def test_collector_does_not_use_removed_logging_exporter():
    """logging exporter 自 v0.111.0 移除（upstream #11337），沿用会让 pipeline 加载失败。"""
    config = _config()
    assert "logging" not in config["exporters"], "仍在使用已移除的 logging exporter"
    assert "debug" in config["exporters"], "缺替代 exporter"


def test_traces_pipeline_references_existing_components():
    """pipeline 引用的 receiver/processor/exporter 都必须已定义。"""
    config = _config()
    traces = config["service"]["pipelines"]["traces"]
    for kind in ("receivers", "processors", "exporters"):
        defined = set(config.get(kind) or {})
        for name in traces[kind]:
            assert name in defined, f"pipeline 引用了未定义的 {kind}: {name}"


def test_kernel_image_includes_otel_extra():
    """K8s 清单默认开 UH_OTEL_ENABLED；镜像只装 compose extra 会导入失败并静默降级。"""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"ARG UH_INSTALL_EXTRAS=(\S+)", dockerfile)
    assert m, "缺 UH_INSTALL_EXTRAS 声明"
    extras = [e.strip() for e in m.group(1).split(",")]
    assert "otel" in extras, f"默认镜像不含 otel extra: {extras}"


def test_k8s_enables_otel_and_points_at_collector():
    """发送端配置必须齐备：启用开关 + endpoint + 采样率。"""
    deployments = [d for d in yaml.safe_load_all(
        (ROOT / "deploy" / "k8s" / "deployments.yaml").read_text(encoding="utf-8")) if d]
    kernel = next(d for d in deployments
                  if d.get("kind") == "Deployment" and d["metadata"]["name"] == "uh-kernel")
    env = {e["name"]: e.get("value") for e in kernel["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env.get("UH_OTEL_ENABLED") == "1"
    assert "otel-collector" in (env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "")
    assert env.get("UH_OTEL_SAMPLE_RATIO")
