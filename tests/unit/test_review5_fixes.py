"""五审回归：身份绑定与密钥扫描门禁。"""
import subprocess

import pytest
from fastapi.testclient import TestClient

from kernel.main import create_app


def test_production_client_identity_cannot_be_spoofed(monkeypatch):
    monkeypatch.setenv("UH_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_LOCAL_MODE", "0")
    monkeypatch.setenv("UH_AUTH_TOKEN", "owner-token")
    app = create_app()
    auth = {"Authorization": "Bearer owner-token"}
    with TestClient(app) as victim:
        session = victim.post("/api/v1/sessions", headers=auth).json()["data"]
        task = victim.post(
            f"/api/v1/sessions/{session['session_id']}/messages",
            headers=auth,
            json={"content": "hello"},
        ).json()["data"]
        # A stale/raw session ID is not a valid principal in production mode.
        with TestClient(app) as attacker:
            status = attacker.get(
                f"/api/v1/tasks/{task['task_id']}",
                headers={**auth, "X-UH-Client-ID": session["session_id"]},
            )
            assert status.status_code == 403
            with attacker.websocket_connect(
                f"/api/v1/stream?task_id={task['task_id']}&token=owner-token&client_id={session['session_id']}"
            ) as websocket:
                error = websocket.receive_json()
                assert error["error"]["code"] == "UH-USR-1002"


def test_valid_cookie_wins_over_invalid_client_header(monkeypatch):
    monkeypatch.setenv("UH_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_LOCAL_MODE", "0")
    monkeypatch.setenv("UH_AUTH_TOKEN", "owner-token")
    app = create_app()
    auth = {"Authorization": "Bearer owner-token"}
    with TestClient(app) as client:
        session = client.post("/api/v1/sessions", headers=auth).json()["data"]
        response = client.post(
            f"/api/v1/sessions/{session['session_id']}/messages",
            headers={**auth, "X-UH-Client-ID": "stale-raw-id"},
            json={"content": "hello"},
        )
        assert response.status_code == 202


def test_secret_scan_catches_spaced_assignments_and_ignores_file_reference():
    code = (
        "import {hasPlainAuthToken as h} from './scripts/secret-scan.mjs';"
        "console.log(h('UH_AUTH_TOKEN = ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'));"
        "console.log(h('UH_AUTH_TOKEN: \"ABCDEFGHIJKLMNOPQRSTUVWX012345\"'));"
        "console.log(h('UH_AUTH_TOKEN_FILE=D:/uh/.auth-token'));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code],
        capture_output=True,
        text=True,
        cwd=".",
        check=True,
    )
    assert result.stdout.splitlines() == ["true", "true", "false"]


def test_unknown_sse_task_returns_404_envelope():
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/tasks/uh_task_missing/stream")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UH-USR-1003"

