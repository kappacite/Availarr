import tomllib

from fastapi.testclient import TestClient

from availarr.agent.config import load_agent_config
from availarr.server.config import load_server_config
from availarr.setup import create_setup, toml_dump


def test_toml_dump_roundtrip():
    d = {"a": "x\"y", "n": 3, "f": 1.5, "b": True, "l": ["French", "English"],
         "radarr": {"url": "http://r"}, "profiles": {"HD-1080p FR": {"a_profiles": ["x"], "min_resolution": 1080}}}
    assert tomllib.loads(toml_dump(d)) == d


SERVER_FORM = {
    "setup_code": "abc123", "token": "tok", "admin_password": "pw", "bandwidth_mbps": "0",
    "radarr_url": "http://r:7878/radarr/", "radarr_key": "k", "radarr_root": "/movies",
    "sonarr_url": "http://s:8989", "sonarr_key": "k2", "sonarr_root": "/tv",
    "qbit_url": "http://q:8080", "qbit_user": "admin", "qbit_pass": "p",
    "keep_days": "7", "seed_days": "3", "fail_days": "7",
    "p_name": ["default", "HD-1080p"], "p_a": ["HD-1080p", "TV Bas Français"], "p_res": ["720", "1080"],
    "p_lang": ["", "French"], "p_announce": ["1080p.WEB-DL", "MULTi.1080p.WEB-DL"],
}


def test_server_setup_writes_valid_config(tmp_path):
    path, saved = tmp_path / "cfg" / "server.toml", []
    c = TestClient(create_setup("server", str(path), load_server_config, lambda: saved.append(1), code="ABC123"))
    page = c.get("/").text
    assert "Configuration du server" in page and 'name="token"' in page
    assert c.post("/save", data={**SERVER_FORM, "setup_code": "nope"}).status_code == 403 and not path.exists()
    assert c.post("/save", data={**SERVER_FORM, "admin_password": ""}).status_code == 400 and not path.exists()
    r = c.post("/save", data=SERVER_FORM)
    assert r.status_code == 200 and saved == [1]
    cfg = load_server_config(str(path))
    assert cfg.radarr_url == "http://r:7878/radarr" and cfg.profiles["HD-1080p"].languages == ["French"]
    assert cfg.profiles["default"].min_resolution == 720 and cfg.admin_password == "pw"


def test_agent_setup_and_test_endpoint_needs_code(tmp_path):
    path = tmp_path / "agent.toml"
    c = TestClient(create_setup("agent", str(path), load_agent_config, code="ABC123"))
    assert c.post("/test", json={"kind": "server", "code": "x", "url": "http://a"}).json()["msg"].startswith("Code")
    assert "http://" in c.post("/test", json={"kind": "server", "code": "abc123", "url": "ftp://a"}).json()["msg"]
    r = c.post("/save", data={"setup_code": "ABC123", "server_url": "http://a:8787", "token": "t", "api_key": "k",
                              "public_url": "http://agent:8789", "complete_dir": "/dl",
                              "radarr_url": "http://r", "radarr_key": "1", "sonarr_url": "http://s", "sonarr_key": "2"})
    assert r.status_code == 200 and load_agent_config(str(path)).complete_dir == "/dl"


def test_tailscale_ip(monkeypatch):
    import socket
    from availarr import setup

    class S:
        ip = ""

        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def connect(self, addr):
            pass

        def getsockname(self):
            return (S.ip, 1)
    monkeypatch.setattr(socket, "socket", S)
    S.ip = "100.101.102.103"
    assert setup.tailscale_ip() == "100.101.102.103"
    S.ip = "192.168.1.2"
    assert setup.tailscale_ip() is None


def test_agent_setup_writes_identity(tmp_path):
    path = tmp_path / "agent.toml"
    c = TestClient(create_setup("agent", str(path), load_agent_config, code="ABC123"))
    assert 'name="agent_id"' in c.get("/").text
    r = c.post("/save", data={"setup_code": "ABC123", "agent_name": "Lyon", "agent_id": "u-1", "admin_password": "pw",
                              "server_url": "http://a:8787", "token": "t", "api_key": "k",
                              "public_url": "http://agent:8789", "complete_dir": "/dl",
                              "radarr_url": "http://r", "radarr_key": "1", "sonarr_url": "http://s", "sonarr_key": "2"})
    assert r.status_code == 200
    cfg = load_agent_config(str(path))
    assert (cfg.agent_name, cfg.agent_id, cfg.admin_password) == ("Lyon", "u-1", "pw")
    assert 'agent_name = "Lyon"' in path.read_text(encoding="utf-8")


def _cfg_env(tmp_path, kind="server"):
    from starlette.datastructures import FormData
    from availarr.setup import config_router, form_values, server_config
    from fastapi import FastAPI
    path = tmp_path / "server.toml"
    form = FormData([(k, x) for k, v in SERVER_FORM.items() for x in (v if isinstance(v, list) else [v])])
    path.write_text(toml_dump({**server_config(form), "api_port": 9000}), encoding="utf-8")
    calls = []
    app = FastAPI()
    app.include_router(config_router("server", str(path), load_server_config, lambda: calls.append(1)))
    return path, calls, TestClient(app), form, form_values


def test_form_values_roundtrip(tmp_path):
    from availarr.setup import server_config
    path, _, _, form, form_values = _cfg_env(tmp_path)
    v = form_values("server", tomllib.loads(path.read_text(encoding="utf-8")))
    assert v["admin_password"] == "" and v["qbit_pass"] == ""
    again = server_config(__import__("starlette.datastructures", fromlist=["FormData"]).FormData(
        [(k, x) for k, val in v.items() if k != "profiles" for x in [val]]
        + [(f"p_{k}", p[k]) for p in v["profiles"] for k in ("a", "res", "lang", "announce")]
        + [("p_name", p["name"]) for p in v["profiles"]]))
    old = server_config(form)
    assert {**again, "admin_password": "pw", "qbittorrent": {**again["qbittorrent"], "password": "p"}} == old


def test_agent_form_values():
    from availarr.setup import agent_config, form_values
    d = {"agent_name": "Lyon", "agent_id": "u-1", "admin_password": "pw", "server_url": "http://a", "token": "t",
         "api_key": "k", "public_url": "http://p", "complete_dir": "/d",
         "radarr": {"url": "http://r", "key": "rk"}, "sonarr": {"url": "http://s", "key": "sk"}}
    v = form_values("agent", d)
    assert v["agent_id"] == "u-1" and v["agent_name"] == "Lyon" and v["admin_password"] == ""
    assert agent_config(type("F", (), {"get": lambda s, k, dflt=None: v.get(k, dflt)})())["radarr"] == d["radarr"]


def test_config_page_has_no_setup_code_nor_tailscale(tmp_path):
    _, _, c, _, _ = _cfg_env(tmp_path)
    t = c.get("/config").text
    assert "Enregistrer et redémarrer" in t and 'name="setup_code"' not in t and "<legend>Tailscale" not in t
    assert 'data-test="/config/test"' in t and "laisser vide pour garder l&#x27;actuel" in t
    assert 'value="tok"' in t


def test_config_keeps_blank_passwords(tmp_path):
    path, calls, c, _, _ = _cfg_env(tmp_path)
    r = c.post("/config", data={**SERVER_FORM, "admin_password": "", "qbit_pass": "", "keep_days": "9"})
    d = tomllib.loads(path.read_text(encoding="utf-8"))
    assert r.status_code == 200 and calls == [1] and "redémarrage" in r.text
    assert d["admin_password"] == "pw" and d["qbittorrent"]["password"] == "p" and d["retention"]["keep_days"] == 9
    assert d["api_port"] == 9000  # keys the form does not know are kept


def test_config_invalid_not_written(tmp_path):
    path, calls, c, _, _ = _cfg_env(tmp_path)
    before = path.read_text(encoding="utf-8")
    r = c.post("/config", data={**SERVER_FORM, "token": ""})
    assert r.status_code == 400 and path.read_text(encoding="utf-8") == before and calls == []
