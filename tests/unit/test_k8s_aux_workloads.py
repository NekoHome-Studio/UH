"""独立审查 P1-4：otel-collector 曾在 J/M 白名单外，可 privileged+scaler SA 全绿绕过。"""
import copy
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_docs():
    docs = []
    for f in sorted((ROOT / "deploy" / "k8s").glob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if d:
                docs.append(d)
    return docs


def _verify(docs):
    """调 verifyK8sManifests 返回错误列表。"""
    script = (
        "import {createRequire} from 'node:module';"
        "const req=createRequire(new URL('./.work/verify-deps/noop.js','file://' + process.cwd().replace(/\\\\/g,'/') + '/'));"
        "const {verifyK8sManifests}=await import('./scripts/verify-k8s-manifests.mjs');"
        "const docs=JSON.parse(process.argv[1]);"
        "console.log(JSON.stringify(verifyK8sManifests(docs)));"
    )
    r = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(docs)],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr
    line = [l for l in r.stdout.splitlines() if l.startswith("[")]
    assert line, r.stdout
    return json.loads(line[-1])


def test_baseline_manifests_pass():
    assert _verify(_load_docs()) == []


@pytest.mark.parametrize("mutation", [
    "hostNetwork", "privileged", "hostPort", "scalerSA", "automountToken",
])
def test_otel_collector_hardening_is_enforced(mutation):
    """辅助工作负载必须与业务同口径受约束（原 J/M 均不校验它）。"""
    docs = _load_docs()
    otel = next(d for d in docs
                if d.get("kind") == "Deployment" and d["metadata"]["name"] == "otel-collector")
    ps = otel["spec"]["template"]["spec"]
    if mutation == "hostNetwork":
        ps["hostNetwork"] = True
    elif mutation == "privileged":
        ps["containers"][0].setdefault("securityContext", {})["privileged"] = True
    elif mutation == "hostPort":
        ps["containers"][0]["ports"][0]["hostPort"] = 4317
    elif mutation == "scalerSA":
        ps["serviceAccountName"] = "uh-scaler"
    elif mutation == "automountToken":
        ps["automountServiceAccountToken"] = True
    errors = _verify(docs)
    assert errors, f"{mutation} 未被 J 项拦截"
    assert any("otel-collector" in e for e in errors), errors


def test_otel_collector_has_real_hardening_in_manifest():
    docs = _load_docs()
    otel = next(d for d in docs
                if d.get("kind") == "Deployment" and d["metadata"]["name"] == "otel-collector")
    ps = otel["spec"]["template"]["spec"]
    assert ps["automountServiceAccountToken"] is False
    assert ps["securityContext"]["runAsNonRoot"] is True
    assert ps["securityContext"]["runAsUser"] == 10001
    sec = ps["containers"][0]["securityContext"]
    assert sec["readOnlyRootFilesystem"] is True
    assert sec["allowPrivilegeEscalation"] is False
    assert sec["capabilities"] == {"drop": ["ALL"]}
