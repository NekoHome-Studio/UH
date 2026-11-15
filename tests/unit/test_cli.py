import socket
import time

import pytest

from kernel import cli


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_config_defaults():
    cfg = cli.load_config()
    assert cfg["ui"]["port"] == 8080
    assert cfg["ui"]["mode"] == "web"
    assert cfg["mode"] == "local"


def test_config_from_repo_file():
    cfg = cli.load_config()
    assert cfg["llm"]["provider"] == "openai"
    assert "short_term" in cfg["memory"]


def test_resolve_ui_web():
    ui = cli.resolve_ui("web")
    assert ui["kernel"] and ui["web"] and not ui["desktop"] and ui["open_browser"]


def test_resolve_ui_desktop():
    ui = cli.resolve_ui("desktop")
    assert ui["kernel"] and not ui["web"] and ui["desktop"] and not ui["open_browser"]


def test_resolve_ui_both():
    ui = cli.resolve_ui("both")
    assert ui["kernel"] and ui["web"] and ui["desktop"] and ui["open_browser"]


def test_resolve_ui_invalid():
    with pytest.raises(ValueError):
        cli.resolve_ui("bogus")


def test_up_down_integration():
    port = _free_port()
    rc = cli.main(["dev", "up", "--port", str(port), "--no-browser"])
    assert rc == 0
    assert cli._wait_health(port, timeout=10)
    pid = cli._read_pid()
    assert pid and cli._alive(pid)
    rc = cli.main(["dev", "down"])
    assert rc == 0
    time.sleep(0.5)
    assert cli._read_pid() is None


def test_reset_cleans_logs():
    cli.STATE.mkdir(exist_ok=True)
    cli.LOG_FILE.write_text("dummy log")
    rc = cli.main(["dev", "reset", "--keep-vectors"])
    assert rc == 0
    assert not cli.LOG_FILE.exists()
