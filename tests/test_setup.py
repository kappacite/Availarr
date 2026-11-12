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
