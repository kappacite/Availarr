import base64
import time

import pytest
from fastapi.testclient import TestClient

from availarr.agent import transfer
from availarr.agent.__main__ import build
from availarr.agent.config import AgentConfig
from availarr.agent.db import AgentJob, AgentStore

AUTH = {"Authorization": "Basic " + base64.b64encode(b"u:pw").decode()}


@pytest.fixture
def env(tmp_path):
    transfer.SERVER.update(ok_at=None, error=None)
    transfer.ETA.clear()
    cfg = AgentConfig("http://s", "t", "key", "http://a", str(tmp_path / "done"), "", "", "", "",
                      db_path=str(tmp_path / "a.db"), agent_id="3f9c2b10-6a7e", agent_name="Lyon", admin_password="pw")
    path = tmp_path / "agent.toml"
    path.write_text('server_url="http://s"\n', encoding="utf-8")
    app, _ = build(cfg, config_path=str(path), restart=lambda: None)
    return TestClient(app), AgentStore(cfg.db_path), cfg


def add(store, i, state, **kw):
    d = dict(nzo_id=f"n{i}", server_job_id=i, name=f"Release.{i}", cat="radarr", state=state, size=2**20, phase=0.5,
             done=0, storage="", fail_message=None, hidden=False, created_at=time.time() + i)
    store.add(AgentJob(**{**d, **kw}))


def test_ui_requires_password(env):
    c, _, _ = env
    assert c.get("/").status_code == 401
    assert c.get("/fragment").status_code == 401
    assert c.get("/config").status_code == 401
    assert c.get("/", headers={"Authorization": "Basic " + base64.b64encode(b"u:bad").decode()}).status_code == 401
    assert c.get("/", headers=AUTH).status_code == 200


def test_empty_password_refuses_all(env):
    c, _, cfg = env
    cfg.admin_password = ""
    assert c.get("/", headers={"Authorization": "Basic " + base64.b64encode(b"u:").decode()}).status_code == 401


def test_sabnzbd_unaffected(env):
    c, _, _ = env
    assert c.get("/sabnzbd/api?mode=version&apikey=key&output=json").status_code == 200


def test_lists_jobs_with_states(env):
    c, store, _ = env
    add(store, 1, "queued")
    add(store, 2, "transferring", done=2**19)
    add(store, 3, "completed")
    add(store, 4, "failed", fail_message="Refusée par l'envoyeur")
    add(store, 5, "cancelled")
    add(store, 6, "queued", hidden=True, name="Cache.moi")
    transfer.ETA["n2"] = 120
    h = c.get("/", headers=AUTH).text
    for s in ("L&#x27;envoyeur télécharge", "Transfert", "Terminé", "Refusée par l&#x27;envoyeur", "Annulé", "2 min"):
        assert s in h
    assert "Cache.moi" not in h
    assert h.index("Release.5") < h.index("Release.1")  # newest first


def test_escapes(env):
    c, store, _ = env
    add(store, 1, "failed", name="<b>x</b>", fail_message="<i>boom</i>")
    h = c.get("/fragment", headers=AUTH).text
    assert "<b>x</b>" not in h and "<i>boom</i>" not in h


def test_server_status(env):
    c, _, _ = env
    transfer.SERVER["ok_at"] = time.time() - 10
    assert "Connecté" in c.get("/", headers=AUTH).text
    transfer.SERVER.update(ok_at=None, error="boom")
    h = c.get("/", headers=AUTH).text
    assert "Injoignable" in h and "boom" in h
    assert "Lyon" in h and "3f9c2b10-6a7e" in h


def test_config_behind_auth(env):
    c, _, _ = env
    assert c.get("/config", headers=AUTH).status_code == 200
