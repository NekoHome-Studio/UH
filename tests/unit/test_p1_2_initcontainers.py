"""R2 P1-2 回归：K8s initContainers 与 host 命名空间 / hostPath 必须被 J 与 M 双门禁拦截。

攻击样本（来自独立审查 R2 P1-2）：向 uh-kernel 注入 privileged initContainer
（privileged:true / runAsUser:0 / seccompProfile Unconfined / capabilities SYS_ADMIN /
hostPath 挂 "/"），并给 Pod 加 hostPID/hostIPC。修复前 J 与 M 均返回 0 errors；
修复后必须全部拦截。
"""
import copy
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "k8s"
DEPLOY = K8S / "deployments.yaml"


def _load_docs():
    docs = []
    for f in sorted(K8S.glob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if d:
                docs.append(d)
    return docs


_NODE_VERIFY_J = (
    "import {createRequire} from 'node:module';"
    "const req=createRequire(new URL('./.work/verify-deps/noop.js','file://' + process.cwd().replace(/\\\\/g,'/') + '/'));"
    "const {verifyK8sManifests}=await import('./scripts/verify-k8s-manifests.mjs');"
    "const docs=JSON.parse(process.argv[1]);"
    "console.log(JSON.stringify(verifyK8sManifests(docs)));"
)


def _verify_j(docs):
    r = subprocess.run(
        ["node", "--input-type=module", "-e", _NODE_VERIFY_J, json.dumps(docs)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert r.returncode == 0, r.stderr
    line = [l for l in r.stdout.splitlines() if l.startswith("[")]
    assert line, r.stdout
    return json.loads(line[-1])


def _verify_m_cli():
    return subprocess.run(
        ["node", "scripts/verify-security-hardening.mjs"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def _write_deployments_bytes(data: bytes):
    DEPLOY.write_bytes(data)


def _attack(docs):
    """向 uh-kernel 注入 privileged initContainer + hostPID/hostIPC + hostPath /"""
    clone = copy.deepcopy(docs)
    kernel = next(
        (d for d in clone
         if d.get("kind") == "Deployment" and d["metadata"]["name"] == "uh-kernel"),
        None,
    )
    ps = kernel["spec"]["template"]["spec"]
    ps["initContainers"] = [{
        "name": "privileged-init",
        "image": "ultra-harness/kernel:0.1.0",
        "securityContext": {
            "privileged": True,
            "runAsUser": 0,
            "runAsGroup": 0,
            "seccompProfile": {"type": "Unconfined"},
            "capabilities": {"add": ["SYS_ADMIN"]},
        },
        "volumeMounts": [{"name": "rootfs", "mountPath": "/"}],
    }]
    ps["volumes"] = (ps.get("volumes") or []) + [{"name": "rootfs", "hostPath": {"path": "/"}}]
    ps["hostPID"] = True
    ps["hostIPC"] = True
    return clone


def test_baseline_j_and_m_pass():
    """基线（无攻击）两门禁必须全绿，防止本回归用例自身误报。"""
    assert _verify_j(_load_docs()) == []
    r = _verify_m_cli()
    assert r.returncode == 0, r.stderr or r.stdout
    assert "SECURITY_HARDENING_PASSED" in r.stdout


@pytest.mark.parametrize("target", ["j", "m"])
def test_initcontainer_attack_blocked(target):
    """同一攻击样本必须被 J 与 M 拦截（非零 errors / 非零退出码）。"""
    docs = _load_docs()
    mutated = _attack(docs)
    if target == "j":
        errors = _verify_j(mutated)
        assert errors, "J 门禁未拦截 privileged initContainer + hostPID/hostIPC 攻击"
        assert any("privileged-init" in e or "host" in e.lower() for e in errors), errors
    else:
        original_bytes = DEPLOY.read_bytes()
        original = original_bytes.decode("utf-8")
        try:
            mutated_text = "---\n".join(
                yaml.safe_dump(d, sort_keys=False) for d in mutated
            )
            DEPLOY.write_text(mutated_text, encoding="utf-8")
            r = _verify_m_cli()
            assert r.returncode != 0, "M 门禁未拦截 privileged initContainer 攻击"
            assert r.stderr, r.stdout
            text = (r.stdout + r.stderr)
            assert any(k in text for k in ("privileged-init", "host PID/IPC", 'hostPath volume at "/"')), text
        finally:
            DEPLOY.write_bytes(original_bytes)


def test_baseline_restored_after_m_mutation():
    """M 门禁对磁盘文件的临时修改必须可还原，不污染后续门禁。"""
    r = _verify_m_cli()
    assert r.returncode == 0, r.stderr or r.stdout
    assert "SECURITY_HARDENING_PASSED" in r.stdout
