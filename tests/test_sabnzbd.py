import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from availarr.agent.config import AgentConfig
from availarr.agent.db import AgentJob, AgentStore
from availarr.agent.sabnzbd import router
from availarr.common import Item, build_nzb

PAYLOAD = {"item": "movie:603", "b_profile": "HD-1080p FR", "title": "The.Matrix.1999.1080p-Availarr", "size": 4 * 2**20}


class FakeServer:
    def __init__(self):
        self.calls, self.closed = [], []

    def request(self, item, prof, title):
        self.calls.append((item, prof, title))
        return 7

    def close(self, sid, outcome):
        self.closed.append((sid, outcome))


def setup(tmp_path):
    cfg = AgentConfig("http://s", "t", "key", "http://a", str(tmp_path / "done"), "", "", "", "")
    store, srv = AgentStore(str(tmp_path / "a.db")), FakeServer()
    app = FastAPI()
    app.include_router(router(cfg, store, srv))
    return TestClient(app), cfg, store, srv


def job(sid, state, **kw):
    d = dict(nzo_id=sid, server_job_id=7, name="T", cat="radarr", state=state, size=2**20, phase=0.0,
             done=0, storage="", fail_message=None, hidden=False, created_at=time.time())
    return AgentJob(**{**d, **kw})


def test_version_and_get_config(tmp_path):
    c, cfg, *_ = setup(tmp_path)
    assert c.get("/sabnzbd/api", params={"mode": "version", "apikey": "key"}).json() == {"version": "4.3.3"}
    conf = c.get("/sabnzbd/api", params={"mode": "get_config", "apikey": "key"}).json()["config"]
    assert conf["misc"]["complete_dir"] == cfg.complete_dir
    assert "radarr" in [x["name"] for x in conf["categories"]]


def test_addfile_creates_job(tmp_path):
    c, cfg, store, srv = setup(tmp_path)
    r = c.post("/sabnzbd/api", params={"mode": "addfile", "apikey": "key", "cat": "radarr"},
               files={"name": ("x.nzb", build_nzb(PAYLOAD))}).json()
    assert r["status"] is True
    j = store.get(r["nzo_ids"][0])
    assert srv.calls == [(Item("movie", 603), "HD-1080p FR", PAYLOAD["title"])]
    assert j.state == "queued" and j.server_job_id == 7 and j.size == PAYLOAD["size"]
    assert j.storage.replace("\\", "/").endswith("radarr/" + PAYLOAD["title"])


def test_addfile_foreign_nzb(tmp_path):
    c, _, store, _ = setup(tmp_path)
    r = c.post("/sabnzbd/api", params={"mode": "addfile", "apikey": "key"}, files={"nzbfile": ("x.nzb", b"<nzb/>")}).json()
    assert r["status"] is False and store.all() == []


def test_queue_percentage(tmp_path):
    c, _, store, _ = setup(tmp_path)
    store.add(job("a", "queued", phase=0.5, size=4 * 2**20))
    s = c.get("/sabnzbd/api", params={"mode": "queue", "apikey": "key"}).json()["queue"]["slots"][0]
    assert s["percentage"] == "25" and s["status"] == "Queued" and s["mb"] == "4.0" and s["mbleft"] == "3.0"


def test_queue_delete_cancels(tmp_path):
    c, cfg, store, srv = setup(tmp_path)
    d = tmp_path / "done" / "radarr" / "T"
    d.mkdir(parents=True)
    (d / "f.mkv.part").write_bytes(b"x")
    store.add(job("a", "transferring", storage=str(d)))
    r = c.get("/sabnzbd/api", params={"mode": "queue", "name": "delete", "value": "a,zzz", "apikey": "key"}).json()
    assert r == {"status": True, "nzo_ids": ["a"]}
    assert store.get("a").state == "cancelled" and srv.closed == [(7, "cancelled")]
    assert not d.exists()


def test_history_completed_failed_and_delete(tmp_path):
    c, _, store, _ = setup(tmp_path)
    store.add(job("a", "completed", storage="/x/T"))
    store.add(job("b", "failed", fail_message="boom"))
    h = c.get("/sabnzbd/api", params={"mode": "history", "apikey": "key"}).json()["history"]["slots"]
    by = {s["nzo_id"]: s for s in h}
    assert by["a"]["status"] == "Completed" and by["a"]["storage"] == "/x/T"
    assert by["b"]["status"] == "Failed" and by["b"]["fail_message"] == "boom"
    c.get("/sabnzbd/api", params={"mode": "history", "name": "delete", "value": "a", "apikey": "key"})
    assert store.get("a").hidden
    h = c.get("/sabnzbd/api", params={"mode": "history", "apikey": "key"}).json()["history"]["slots"]
    assert [s["nzo_id"] for s in h] == ["b"]


def test_sab_bad_apikey(tmp_path):
    c, *_ = setup(tmp_path)
    assert c.get("/sabnzbd/api", params={"mode": "version", "apikey": "no"}).json() == {"status": False, "error": "API Key Incorrect"}
