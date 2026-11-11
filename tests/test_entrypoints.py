import dataclasses

from fastapi.testclient import TestClient

from availarr.agent import __main__ as agent_main
from availarr.agent.config import load_agent_config
from availarr.server import __main__ as server_main
from availarr.server.config import load_server_config


def exists(app, method, path):
    return TestClient(app).request(method, path).status_code != 404


def test_server_build(tmp_path):
    cfg = dataclasses.replace(load_server_config("examples/server.toml"), db_path=str(tmp_path / "s.db"),
                              qbit_url="http://127.0.0.1:9")
    api, panel, retention_once = server_main.build(cfg)
    assert exists(api, "POST", "/v1/lookup")
    assert exists(panel, "GET", "/")
    retention_once()


def test_agent_build(tmp_path):
    cfg = dataclasses.replace(load_agent_config("examples/agent.toml"), db_path=str(tmp_path / "a.db"),
                              server_url="http://127.0.0.1:9")
    app, step_once = agent_main.build(cfg)
    assert exists(app, "GET", "/newznab/api") and exists(app, "GET", "/sabnzbd/api")
    assert not exists(app, "GET", "/nope")
    step_once()
