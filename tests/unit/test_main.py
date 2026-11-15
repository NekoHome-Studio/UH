import json
import logging
import re

from fastapi.testclient import TestClient

from kernel.main import create_app


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _client_with_capture():
    cap = _Capture()
    logging.getLogger().addHandler(cap)
    return TestClient(create_app()), cap


def test_health():
    client, cap = _client_with_capture()
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"
    logging.getLogger().removeHandler(cap)


def test_ready():
    client, cap = _client_with_capture()
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    logging.getLogger().removeHandler(cap)


def test_trace_id_header_and_log_correlation():
    client, cap = _client_with_capture()
    r = client.get("/health")
    tid = r.headers.get("X-Trace-Id")
    assert tid and re.fullmatch(r"uh_trc_[0-9a-f]{32}", tid)
    events = [getattr(rec, "event", "") for rec in cap.records]
    assert "request.received" in events and "request.finished" in events
    tids = {getattr(rec, "trace_id", None) for rec in cap.records if getattr(rec, "event", "") in ("request.received", "request.finished")}
    assert tids == {tid}
    logging.getLogger().removeHandler(cap)


def test_trace_ids_differ_per_request():
    client, cap = _client_with_capture()
    t1 = client.get("/health").headers["X-Trace-Id"]
    t2 = client.get("/health").headers["X-Trace-Id"]
    assert t1 != t2
    logging.getLogger().removeHandler(cap)


def test_external_trace_id_accepted():
    client, cap = _client_with_capture()
    external = "uh_trc_" + "12" * 16
    r = client.get("/health", headers={"X-Trace-Id": external})
    assert r.headers["X-Trace-Id"] == external
    logging.getLogger().removeHandler(cap)
