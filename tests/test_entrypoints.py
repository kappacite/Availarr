import dataclasses
from pathlib import Path

from fastapi.testclient import TestClient

from availarr.agent import __main__ as agent_main
from availarr.agent.config import load_agent_config
from availarr.server import __main__ as server_main
from availarr.server.config import load_server_config

EX = Path(__file__).parent.parent / "examples"


def exists(app, method, path):
    return TestClient(app).request(method, path).status_code != 404


def test_server_build(tmp_path, caplog, monkeypatch):
    cfg = dataclasses.replace(load_server_config(str(EX / "server.toml")), db_path=str(tmp_path / "s.db"),
                              qbit_url="http://127.0.0.1:9")
    api, panel, retention_once = server_main.build(cfg)
    assert exists(api, "POST", "/v1/lookup")
    assert exists(panel, "GET", "/")
    retention_once()  # empty store, unreachable qbit: must not raise
    monkeypatch.setattr(server_main, "tick", lambda *a: 1 / 0)
    retention_once()
    assert "retention tick failed" in caplog.text


def test_agent_build(tmp_path, caplog, monkeypatch):
    cfg = dataclasses.replace(load_agent_config(str(EX / "agent.toml")), db_path=str(tmp_path / "a.db"),
                              server_url="http://127.0.0.1:9")
    app, step_once = agent_main.build(cfg)
    assert exists(app, "GET", "/newznab/api") and exists(app, "GET", "/sabnzbd/api")
    assert not exists(app, "GET", "/nope")
    step_once()
    monkeypatch.setattr(agent_main, "step", lambda *a: 1 / 0)
    step_once()
    assert "agent step failed" in caplog.text
