"""M3-1 Compose 契约，验证过程无需 Docker 或外部存储。"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
import yaml

from kernel.orchestration import Checkpoint, OrchestrationService, PostgresCheckpointStore
from kernel.retrieval import ChromaVectorStore


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yaml"
VERIFY = ROOT / "scripts/verify-compose-exposure.mjs"


def test_compose_exposure_gate():
    result = subprocess.run(["node", str(VERIFY)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "7 services, 1 published port" in result.stdout


@pytest.mark.parametrize(("service", "field", "value"), [
    ("uh-kernel", "ports", ["8081:8080"]),
    ("redis", "ports", []),
    ("chroma", "ports", ["8000:8000"]),
    ("postgres", "ports", ["5432:5432"]),
    ("uh-llama-index", "expose", ["8081"]),
    ("uh-langgraph", "network_mode", "host"),
    ("uh-crewai", "profiles", []),
    (None, "version", "3.9"),
    ("postgres", "environment", {"POSTGRES_PASSWORD": "inline-fixture"}),
    ("uh-kernel", "environment", ["UH_LLM_API_KEY=inline-fixture"]),
    ("uh-kernel", "environment", {"UH_CLIENT_ID_SECRET": "${CLIENT_SECRET:-inline-fixture}"}),
    ("uh-kernel", "environment", {"UH_CHECKPOINT_POSTGRES_URL": "postgresql://user:inline-fixture@postgres/db"}),
])
def test_compose_exposure_gate_rejects_drift(tmp_path, service, field, value):
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    target = document["services"][service] if service else document
    target[field] = value
    candidate = tmp_path / "compose.yaml"
    candidate.write_text(yaml.safe_dump(document), encoding="utf-8")
    result = subprocess.run(["node", str(VERIFY), str(candidate)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 1
    assert "FAIL" in result.stderr
    assert "inline-fixture" not in result.stderr


def test_compose_environment_and_volumes():
    compose = COMPOSE.read_text(encoding="utf-8")
    document = yaml.safe_load(compose)
    services = document["services"]
    kernel = services["uh-kernel"]
    env = kernel["environment"]
    template = dict(line.split("=", 1) for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
                    if line and not line.startswith("#"))
    assert set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose)) == template.keys()
    for name in ("DB_PASSWORD", "UH_LLM_API_KEY", "UH_CLIENT_ID_SECRET", "UH_MEMORY_MASTER_KEY"):
        assert template[name] == ""
    assert env["UH_LOCAL_MODE"] == "0" and env["UH_AUTH_REQUIRED"] == "1"
    assert "UH_AUTH_TOKEN" not in env
    assert env["PGPASSWORD"] == services["postgres"]["environment"]["POSTGRES_PASSWORD"]
    assert template["UH_REDIS_URL"] == "redis://redis:6379"
    assert template["UH_CHECKPOINT_POSTGRES_URL"] == "postgresql://uh@postgres:5432/uh_checkpoints"
    assert template["UH_RETRIEVAL_CHROMA"] == "http://chroma:8000"
    assert set(kernel["depends_on"]) == {"redis", "chroma", "postgres"}
    assert set(services["uh-langgraph"]["depends_on"]) == {"redis", "postgres"}
    assert services["uh-llama-index"]["depends_on"] == ["chroma"]
    assert services["chroma"]["volumes"] == ["chroma-data:/chroma/db"]
    assert services["chroma"]["environment"]["CHROMA_PERSIST_PATH"] == "/chroma/db"
    assert services["postgres"]["volumes"] == ["pg-data:/var/lib/postgresql/data"]
    assert kernel["volumes"] == ["uh-state:/app/.uh"]
    assert env["UH_PLUGIN_ROOT"] == "/app/.uh/plugins"
    assert kernel["secrets"] == [
        {"source": "uh_auth_token", "target": env["UH_AUTH_TOKEN_FILE"]},
        {"source": "uh_plugin_trusted_keys", "target": env["UH_PLUGIN_TRUSTED_KEYS_FILE"]},
    ]
    assert set(document["secrets"]) == {item["source"] for item in kernel["secrets"]}
    assert all("file" in secret for secret in document["secrets"].values())


def test_kernel_image_runtime_and_cli():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert len(re.findall(r"^FROM\s", dockerfile, re.MULTILINE)) >= 2
    runtime = dockerfile.split(" AS runtime\n", 1)[1]
    assert re.findall(r"^USER\s+(\S+)", runtime, re.MULTILINE) == ["10001:10001"]
    assert re.search(r"^HEALTHCHECK\s+.*127\.0\.0\.1:8080/health", runtime, re.MULTILINE)
    assert "/app/.uh/plugins" in runtime
    assert "UH_PLUGIN_INSTALL_VIA_KERNEL=1" in runtime
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"]["uh"] == "kernel.cli:main"


def test_chroma_url_selects_http_client(monkeypatch):
    client = Mock()
    chromadb = SimpleNamespace(HttpClient=Mock(return_value=client), PersistentClient=Mock())
    monkeypatch.setitem(sys.modules, "chromadb", chromadb)
    monkeypatch.setenv("UH_RETRIEVAL_CHROMA", "http://chroma:8000")
    assert ChromaVectorStore()._chroma is client
    chromadb.HttpClient.assert_called_once_with(host="chroma", port=8000, ssl=False)
    chromadb.PersistentClient.assert_not_called()
    client.get_or_create_collection.assert_called_once_with("uh_retrieval", embedding_function=None)


def test_postgres_checkpoint_backend(monkeypatch):
    connection = MagicMock()
    db = connection.__enter__.return_value
    connect = Mock(return_value=connection)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("UH_CHECKPOINT_BACKEND", "postgres")
    monkeypatch.setenv("UH_CHECKPOINT_POSTGRES_URL", "postgresql://uh@postgres:5432/uh_checkpoints")
    store = OrchestrationService._store_from_env()
    assert isinstance(store, PostgresCheckpointStore)
    checkpoint = Checkpoint("step-1", "task-1", {"content": "中文"})
    store.save(checkpoint)
    sql, parameters = db.execute.call_args.args
    assert "ON CONFLICT" in sql and parameters[:3] == ("step-1", "task-1", checkpoint.updated_at)
    db.execute.return_value.fetchone.return_value = (parameters[3],)
    assert store.load("task-1", "step-1") == checkpoint
    assert db.execute.call_args.args[1] == ("step-1", "task-1")
    assert store.load("task-1") == checkpoint
    assert db.execute.call_args.args[1] == ("task-1",)
    connect.assert_called_with(os.environ["UH_CHECKPOINT_POSTGRES_URL"], connect_timeout=5)
