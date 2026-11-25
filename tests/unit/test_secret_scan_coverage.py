"""独立审查 P1-3：密钥扫描覆盖面（原只认 UH_AUTH_TOKEN 单一变量）。"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REAL = 'Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga'


def _scan(snippet):
    script = (
        "import {hasPlainAuthToken} from './scripts/secret-scan.mjs';"
        "console.log(hasPlainAuthToken(" + json.dumps(snippet) + ") ? 'HIT' : 'MISS');"
    )
    r = subprocess.run(["node", "--input-type=module", "-e", script],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return "HIT" in r.stdout


@pytest.mark.parametrize("name", [
    "UH_AUTH_TOKEN", "UH_MEMORY_MASTER_KEY", "UH_LLM_API_KEY",
    "UH_CLIENT_ID_SECRET", "DB_PASSWORD", "PGPASSWORD", "POSTGRES_PASSWORD",
])
def test_all_secret_variables_are_scanned(name):
    assert _scan(name + "=" + REAL), name + " 明文未被检出"


@pytest.mark.parametrize("form", [
    "${UH_AUTH_TOKEN:?" + REAL + "}",
    "${UH_AUTH_TOKEN:-" + REAL + "}",
    "${DB_PASSWORD:?" + REAL + "}",
])
def test_env_default_inline_credential_is_caught(form):
    assert _scan("UH_AUTH_TOKEN: " + form), form


@pytest.mark.parametrize("snippet", [
    "DB_PASSWORD=",
    "UH_LLM_API_KEY=",
    "UH_AUTH_TOKEN: ${UH_AUTH_TOKEN:-}",
    "PGPASSWORD: ${DB_PASSWORD:?Set DB_PASSWORD}",
    "UH_CLIENT_ID_SECRET: ${UH_CLIENT_ID_SECRET:?Set UH_CLIENT_ID_SECRET}",
    "UH_AUTH_TOKEN_FILE=/run/secrets/uh_auth_token",
    "UH_AUTH_TOKEN=placeholdersamplecredentialabc",
    "UH_MEMORY_MASTER_KEY=supersecretvalue1234567890",
])
def test_placeholders_and_prompts_are_not_flagged(snippet):
    assert not _scan(snippet), snippet


def test_empty_value_does_not_swallow_next_line():
    text = "DB_PASSWORD=\nUH_CLIENT_ID_SECRET=\nUH_MEMORY_MASTER_KEY=\n"
    assert not _scan(text)


def test_repository_scan_passes():
    r = subprocess.run(["node", "scripts/secret-scan.mjs"], cwd=ROOT,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SECRET_SCAN_PASSED" in r.stdout
