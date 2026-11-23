import json
import subprocess
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from kernel.main import create_app

ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "desktop/src-tauri"
LOOPBACK = "http://127.0.0.1:8080"


def test_desktop_config_and_shared_ui():
    subprocess.run(["node", "scripts/verify-desktop.mjs"], cwd=ROOT, check=True)
    config = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    assert config["plugins"]["uh-desktop"]["wakeShortcut"] == "Ctrl+Alt+U"
    assert config["app"]["withGlobalTauri"] is True
    security = config["app"]["security"]
    assert security["capabilities"] == ["desktop"]
    connect = next(rule.split()[1:] for rule in security["csp"].split("; ") if rule.startswith("connect-src"))
    assert set(connect) == {"'self'", LOOPBACK, "ws://127.0.0.1:8080", "ipc:", "http://ipc.localhost"}
    capability = json.loads((TAURI / "capabilities/desktop.json").read_text(encoding="utf-8"))
    assert capability["local"] is True  # release frontendDist is a Tauri-local origin
    assert capability["windows"] == ["main"]
    assert capability["remote"]["urls"] == [LOOPBACK + "/*"]
    assert capability["permissions"] == ["allow-report-task-state"]
    for name in config["bundle"]["icon"]:
        assert (TAURI / name).stat().st_size > 0


def test_desktop_tray_and_lifecycle_wiring():
    native = (TAURI / "src/main.rs").read_text(encoding="utf-8")
    kernel = (TAURI / "src/kernel.rs").read_text(encoding="utf-8")
    for menu_id in ["show", "hide", "open-web", "quit"]:
        assert f'.text("{menu_id}",' in native
        assert f'"{menu_id}" =>' in native
    for text in ["Idle", "Running", "Degraded", "Notice::Finished", "Notice::Failed",
                 "UH_DESKTOP_SHORTCUT", "ShortcutState::Pressed", "RunEvent::Exit"]:
        assert text in native
    assert '"127.0.0.1", "--port", "8080"' in kernel
    assert 'get("x-uh-desktop")' in kernel
    assert "io::ErrorKind::AddrInUse" in kernel
    assert "child.kill()" in kernel and "child.wait()" in kernel


def test_desktop_reuses_exact_page_and_scripts_with_auth_enabled(monkeypatch):
    monkeypatch.setenv("UH_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_LOCAL_MODE", "0")
    config = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    with TestClient(create_app(), base_url=LOOPBACK) as client:
        web = client.get("/")
        desktop = client.get(config["build"]["frontendDist"])
        assert web.status_code == desktop.status_code == 200
        assert web.content == desktop.content == (ROOT / "ui/index.html").read_bytes()
        assert "x-uh-desktop" not in web.headers
        assert desktop.headers["x-uh-desktop"] == "1"
        assert desktop.headers["content-security-policy"] == config["app"]["security"]["csp"]
        for script in ("state-machine.js", "themes.js", "app.js"):
            asset = client.get(f"/static/{script}")
            assert asset.status_code == 200
            assert asset.content == (ROOT / "ui" / script).read_bytes()


def test_web_and_desktop_read_each_others_theme_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    monkeypatch.setenv("UH_PLUGIN_ROOT", str(tmp_path / "plugins"))
    monkeypatch.setenv("UH_PLUGIN_REGISTRY", str(tmp_path / "plugins.json"))
    monkeypatch.setenv("UH_PLUGIN_SIGNATURE_REQUIRED", "0")
    example = ROOT / "examples/plugins/official/theme-light"
    app = create_app()
    catalog, selection = "/api/v1/themes", "/api/v1/themes/active"
    # Two clients, one app lifespan and one registry.
    with TestClient(app, base_url=LOOPBACK) as web, closing(TestClient(app, base_url=LOOPBACK)) as desktop:
        installed = web.post("/api/v1/plugins", json={"source": str(example)})
        assert installed.status_code == 201, installed.text
        name = "theme-light"
        assert web.put(selection, json={"name": name}).status_code == 200
        themes = desktop.get(catalog).json()["data"]
        assert themes["active"] == name
        assert themes["themes"][0]["css_variables"]["--uh-bg-primary"] == "#f8fafc"
        assert desktop.put(selection, json={"name": None}).status_code == 200
        assert web.get(catalog).json()["data"]["active"] is None


def test_desktop_reports_reduced_ws_terminal_events():
    subprocess.run(["node", "--input-type=module", "-"], cwd=ROOT, check=True, text=True, input=r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const nodes = new Map();
const element = () => ({
    textContent: '', addEventListener() {}, scrollIntoView() {}, focus() {},
    appendChild(node) { if (node.id) nodes.set(node.id, node); },
    remove() { nodes.delete(this.id); },
});
const calls = [];
let ws;
const scope = vm.createContext({
    document: { getElementById: id => nodes.get(id), createElement: element },
    console, URLSearchParams,
    location: { search: '', protocol: 'http:', host: '127.0.0.1:8080' },
    localStorage: { getItem: () => '' },
    UHThemes: { mountThemePicker() {} },
    __TAURI__: { core: { invoke(command, payload) {
        assert.equal(command, 'report_task_state');
        calls.push([payload.status, payload.notice]);
    } } },
    WebSocket: class { constructor(url) { assert.match(url, /^ws:\/\/127\.0\.0\.1:8080\/api\/v1\/stream\?/); ws = this; } },
});
scope.window = scope;
for (const id of ['chat', 'prompt', 'send', 'token', 'theme', 'theme-status']) nodes.set(id, element());
for (const file of ['state-machine.js', 'app.js']) vm.runInContext(fs.readFileSync('ui/' + file, 'utf8'), scope);
scope.target = element();
vm.runInContext("connectWs('task', target, 'trace', 0)", scope);
const event = (name, payload = {}) => ws.onmessage({data: JSON.stringify({event: name, payload})});
event('task.created');
event('task.degraded');
event('task.finished');
event('task.finished'); // replay must not notify twice
event('task.created');
event('task.failed', {error: {code: 'UH-CAP-4003', message: 'test'}});
event('task.created');
event('task.cancelled');
await vm.runInContext('desktopReports', scope);
assert.deepEqual(calls, [
    ['running', null], ['degraded', null], ['idle', 'finished'],
    ['running', null], ['idle', 'failed'], ['running', null], ['idle', null],
]);
""")
