"""2026-11-28 审计第 2 轮：#5 Windows 沙箱静态扫描漏文件 IO、#6 HPA 与缩容器目标冲突。"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


# ---------- #5 静态扫描必须拦沙箱外敏感路径 ----------
@pytest.mark.parametrize('code', [
    'f = open("/etc/passwd", "r")',
    'import io; io.open("/etc/shadow")',
    'import os; os.open("/etc/passwd", os.O_RDONLY)',
    'from pathlib import Path; Path("/etc/passwd").read_text()',
    'open("/root/.ssh/id_rsa").read()',
    'Path("/proc/self/environ").read_bytes()',
])
def test_static_scan_catches_sensitive_path_access(code):
    from kernel.plugins import _static_scan
    findings = _static_scan({'probe.py': code.encode()})
    assert findings, '敏感路径未被检出: ' + code


@pytest.mark.parametrize('code', [
    'def add(a, b): return a + b',
    's = "a".replace("a", "b")',
    'd = {}; d.get("k")',
    'items = [1, 2]',
    'from pathlib import Path; Path(__file__).parent.joinpath("data.json").read_text()',
    'open("/tmp/scratch.txt", "w")',
])
def test_static_scan_does_not_flag_legitimate_plugin_io(code):
    from kernel.plugins import _static_scan
    assert _static_scan({'probe.py': code.encode()}) == [], '误杀: ' + code


# ---------- #6 HPA 下限必须允许缩到 0 ----------
def _docs(name):
    return [d for d in yaml.safe_load_all(
        (ROOT / "deploy" / "k8s" / name).read_text(encoding="utf-8")) if d]


def test_crewai_hpa_allows_scale_to_zero():
    """缩容器会 scale 到 0；HPA 下限若为 1 会与之永久拉锯。"""
    hpa = next(d for d in _docs("hpa.yaml")
               if d.get("kind") == "HorizontalPodAutoscaler"
               and d["metadata"]["name"] == "uh-crewai")
    assert hpa["spec"]["minReplicas"] == 0, \
        "HPA 下限必须为 0，否则与内核缩容器目标冲突"
    assert hpa["spec"]["maxReplicas"] >= 1


def test_crewai_scaler_targets_zero():
    """代码侧确实会缩到 0（与上一条断言互为约束）。"""
    import inspect

    from kernel import collaboration

    src = inspect.getsource(collaboration)
    assert "scale(_TARGET, 0" in src or "scale(_TARGET, 0," in src, \
        "缩容目标应为 0，若改为 >0 需同步调整 HPA"


def test_other_capabilities_keep_positive_floor():
    """检索/编排的 HPA 应保持 >=2 下限（它们承接正常业务流量）。"""
    for dep in _docs("hpa.yaml"):
        if dep.get("kind") != "HorizontalPodAutoscaler":
            continue
        name = dep["metadata"]["name"]
        if name == "uh-crewai":
            continue
        assert dep["spec"]["minReplicas"] >= 2, f"{name} 下限过低"
