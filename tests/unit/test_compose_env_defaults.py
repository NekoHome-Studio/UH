"""R2 P1-1：compose 门禁的 ${VAR:?default} 默认值可夹带明文凭据。"""
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _base():
    return yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))


def _verify(doc):
    payload = Path(tempfile.mkdtemp()) / "compose.json"
    payload.write_text(json.dumps(doc), encoding="utf-8")
    script = (
        "import fs from 'node:fs';"
        "const {verifyComposeExposure}=await import('./scripts/verify-compose-exposure.mjs');"
        "const doc=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));"
        "console.log(JSON.stringify(verifyComposeExposure(doc)));"
    )
    r = subprocess.run(["node", "--input-type=module", "-e", script, str(payload)],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    assert r.returncode == 0, r.stderr
    line = [l for l in (r.stdout or "").splitlines() if l.startswith("[")]
    assert line, r.stdout
    return json.loads(line[-1])


def test_real_compose_has_no_errors():
    assert _verify(_base()) == []


@pytest.mark.parametrize("key,value", [
    ("UH_AUTH_TOKEN", "${UH_AUTH_TOKEN:?hunter2-hardcoded-default}"),
    ("DATABASE_URL", "${DB:?postgres://admin:pw@db}"),
    ("UH_MEMORY_MASTER_KEY", "${UH_MEMORY_MASTER_KEY:-placeholder-cred-not-a-real-secret}"),
    ("UH_LLM_API_KEY", "${UH_LLM_API_KEY:-placeholder-cred-not-a-real-secret}"),
])
def test_env_default_credential_is_rejected(key, value):
    doc = _base()
    doc["services"]["uh-kernel"]["environment"][key] = value
    errors = _verify(doc)
    assert errors, key + "=" + value + " 未被拦截"


@pytest.mark.parametrize("key,value", [
    ("UH_LLM_BASE_URL", "${UH_LLM_BASE_URL:-https://api.openai.com/v1}"),
    ("UH_REDIS_URL", "${UH_REDIS_URL:-redis://redis:6379}"),
    ("UH_LLM_MODEL", "${UH_LLM_MODEL:-gpt-4o-mini}"),
    ("DB_PASSWORD", "${DB_PASSWORD:?Set DB_PASSWORD}"),
    ("UH_AUTH_TOKEN_FILE", "${UH_AUTH_TOKEN_FILE:-/run/secrets/uh_auth_token}"),
    ("UH_LLM_API_KEY", "${UH_LLM_API_KEY:-}"),
])
def test_benign_defaults_are_not_flagged(key, value):
    doc = _base()
    doc["services"]["uh-kernel"]["environment"][key] = value
    assert _verify(doc) == [], key + "=" + value + " 被误报"
