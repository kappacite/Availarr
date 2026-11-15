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


def test_ensure_identity_writes_uuid(tmp_path):
    import uuid

    from availarr.agent.config import ensure_identity, load_agent_config
    p = tmp_path / "agent.toml"
    p.write_text('server_url="http://s"\ntoken="t"\napi_key="k"\npublic_url="http://a"\ncomplete_dir="/d"\n'
                 '[radarr]\nurl="r"\nkey="1"\n[sonarr]\nurl="s"\nkey="2"\n', encoding="utf-8")
    cfg = ensure_identity(str(p), load_agent_config(str(p)))
    uuid.UUID(cfg.agent_id)
    again = load_agent_config(str(p))
    assert again.agent_id == cfg.agent_id and again.admin_password == "k" and again.agent_name
    assert ensure_identity(str(p), again).agent_id == cfg.agent_id
    assert (again.token, again.radarr_key, again.sonarr_url) == ("t", "1", "s")
    p.write_text(p.read_text(encoding="utf-8").replace('agent_name = "' + again.agent_name + '"', 'agent_name = ""'),
                 encoding="utf-8")
    fixed = ensure_identity(str(p), load_agent_config(str(p)))
    assert fixed.agent_name and fixed.agent_id == cfg.agent_id and load_agent_config(str(p)).agent_name


def test_agent_blocked_shows_clear_error(tmp_path, monkeypatch):
    import httpx

    from availarr.agent.transfer import SERVER, ServerClient
    cfg = dataclasses.replace(load_agent_config(str(EX / "agent.toml")), db_path=str(tmp_path / "a.db"),
                              server_url="http://127.0.0.1:9")
    _, step_once = agent_main.build(cfg)

    def ping(self):
        r = httpx.Response(403, request=httpx.Request("GET", "http://s/v1/ping"))
        raise httpx.HTTPStatusError("403 Forbidden", request=r.request, response=r)
    monkeypatch.setattr(ServerClient, "ping", ping)
    step_once()
    assert SERVER["error"] == "Bloqué par l'envoyeur"
