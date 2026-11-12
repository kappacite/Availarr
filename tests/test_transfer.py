import httpx
import pytest
from fastapi.testclient import TestClient

from availarr.agent.db import AgentJob, AgentStore
from availarr.agent.transfer import ETA, RATE, ServerClient, step
from availarr.server.api import create_api
from availarr.server.db import JobStore
from availarr.server.jobs import Jobs
from availarr.server.library import Unit
from availarr.server.quality import FileInfo
from availarr.common import Item
from tests.fakes import FakeLibrary, make_cfg

DATA = bytes(range(250)) * 4
P = "HD-1080p FR"
ITEM = Item.from_key("movie:603")


class Env:
    def __init__(self, tmp_path, ready=True):
        f = tmp_path / "src" / "Dune.mkv"
        f.parent.mkdir()
        f.write_bytes(DATA)
        info = FileInfo(1080, "Bluray-1080p", ["French", "English"], str(f), 1000)
        self.lib = FakeLibrary([Unit("movie:603", True, True, info) if ready else Unit("movie:603", True, False, None)])
        cfg = make_cfg(bandwidth_mbps=0)
        self.now = 42.0
        self.jobs = Jobs(JobStore(str(tmp_path / "j.db")), self.lib, self.lib, cfg, lambda: self.now)
        self.server = ServerClient("http://x", "change-me", TestClient(create_api(self.jobs, cfg)))
        self.db = str(tmp_path / "a.db")
        self.store = AgentStore(self.db)
        self.dir = tmp_path / "out"
        self.jid = self.server.request("movie:603", P, "t")

    def add(self, state="queued"):
        self.store.add(AgentJob("n1", self.jid, "t", "radarr", state, 1000, 0.0, 0, str(self.dir), None, False, 0.0))

    def job(self):
        return self.store.get("n1")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_download_resumes_from_part(env):
    env.dir.mkdir()
    dest = env.dir / "Dune.mkv"
    (env.dir / "Dune.mkv.part").write_bytes(DATA[:400])
    seen = []
    env.server.download(env.jid, 0, dest, 1000, seen.append)
    assert dest.read_bytes() == DATA and not (env.dir / "Dune.mkv.part").exists()
    assert env.jobs.transfer[env.jid] == (1000, 1000) and seen[-1] == 1000
    (t,) = env.jobs.store.transfers()
    assert (t["job_id"], t["bytes"], t["complete"]) == (env.jid, 600, 1)  # only the resumed part went over


def test_download_complete_part_416(env):
    env.dir.mkdir()
    (env.dir / "Dune.mkv.part").write_bytes(DATA)
    env.server.download(env.jid, 0, env.dir / "Dune.mkv", 1000, lambda n: None)
    assert (env.dir / "Dune.mkv").read_bytes() == DATA


def test_download_short_stream_keeps_part(env, monkeypatch):
    env.dir.mkdir()
    real = httpx.Response.iter_bytes

    def short(self, n=None):
        yield next(real(self, n))[:300]
    monkeypatch.setattr(httpx.Response, "iter_bytes", short)
    with pytest.raises(OSError):
        env.server.download(env.jid, 0, env.dir / "Dune.mkv", 1000, lambda n: None)
    assert (env.dir / "Dune.mkv.part").stat().st_size == 300 and not (env.dir / "Dune.mkv").exists()


def test_lookup_none(env):
    assert env.server.lookup("movie:603", P)["size"] == 1000
    env.lib.units = []
    assert env.server.lookup("movie:603", P) is None


def test_step_pending_updates_phase(tmp_path):
    e = Env(tmp_path, ready=False)
    e.lib.progress = 0.5
    e.add()
    step(e.store, e.server)
    assert (e.job().phase, e.job().state) == (0.5, "queued")
    # time left = torrent ETA (unknown here: no qBittorrent) + 1000 bytes at the learned rate
    RATE[0] = 100.0
    e.jobs.qbit = type("Q", (), {"live": lambda self, hs: {}})()
    e.lib.progress = 0.5
    step(e.store, e.server)
    assert ETA.pop("n1") == 10.0
    RATE[0] = 100e6 / 8
    e.jobs.store.add_transfer(99, 0, 10, 5000, True)  # server history: 500 B/s wins over the fallback
    step(e.store, e.server)
    assert ETA.pop("n1") == 2.0


def test_step_ready_transfers_and_completes(env):
    env.add()
    step(env.store, env.server)
    j = env.job()
    assert j.state == "completed" and (j.size, j.done) == (1000, 1000)
    assert (env.dir / "Dune.mkv").read_bytes() == DATA
    assert env.jobs.store.get(env.jid).outcome == "delivered"


def test_step_failed_marks_failed(tmp_path):
    e = Env(tmp_path, ready=False)
    e.now = 10**9
    e.add()
    step(e.store, e.server)
    assert e.job().state == "failed" and e.job().fail_message == e.jobs.store.get(e.jid).error


def test_step_resumes_after_restart(env):
    env.dir.mkdir()
    (env.dir / "Dune.mkv.part").write_bytes(DATA[:400])
    env.add("transferring")
    step(AgentStore(env.db), env.server)
    assert env.job().state == "completed" and (env.dir / "Dune.mkv").read_bytes() == DATA


def test_step_bad_name_fails(env, monkeypatch):
    env.add()
    monkeypatch.setattr(env.server, "status", lambda i: {"state": "ready", "progress": 1.0, "error": None,
                                                         "files": [{"index": 0, "name": "../x", "size": 1}]})
    step(env.store, env.server)
    assert env.job().state == "failed" and env.job().fail_message == "nom de fichier invalide"


def test_step_survives_network_error(env, monkeypatch):
    env.add()

    def boom(i):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(env.server, "status", boom)
    step(env.store, env.server)
    assert env.job().state == "queued"


def _cancel(env):
    j = env.job()
    j.state = "cancelled"
    env.store.update(j)


def test_step_skips_cancelled(env):
    env.add()
    _cancel(env)
    step(env.store, env.server)
    assert env.job().state == "cancelled" and not (env.dir / "Dune.mkv").exists()
    assert env.jobs.store.get(env.jid).outcome == "cancelled" and env.job().reported  # unreported cancel is closed


def test_step_cancel_during_download(env, monkeypatch):
    env.add()
    real = env.server.download

    def dl(jid, idx, dest, size, cb):
        def hook(n):
            _cancel(env)
            cb(n)
        real(jid, idx, dest, size, hook)
    monkeypatch.setattr(env.server, "download", dl)
    step(env.store, env.server)
    assert env.job().state == "cancelled"
    assert env.jobs.store.get(env.jid).outcome is None


def test_download_oversized_part_restarts(env):
    env.dir.mkdir()
    (env.dir / "Dune.mkv.part").write_bytes(DATA + b"junk")
    env.server.download(env.jid, 0, env.dir / "Dune.mkv", 1000, lambda n: None)
    assert (env.dir / "Dune.mkv").read_bytes() == DATA


def test_download_200_to_range_truncates(env, monkeypatch):
    env.dir.mkdir()
    (env.dir / "Dune.mkv.part").write_bytes(b"x" * 400)
    orig = env.server.client.stream

    def stream(method, url, headers=None, **kw):
        return orig(method, url, headers={k: v for k, v in headers.items() if k != "Range"}, **kw)
    monkeypatch.setattr(env.server.client, "stream", stream)
    env.server.download(env.jid, 0, env.dir / "Dune.mkv", 1000, lambda n: None)
    assert (env.dir / "Dune.mkv").read_bytes() == DATA


def test_step_malformed_status_survives(env, monkeypatch):
    env.add()
    monkeypatch.setattr(env.server, "status", lambda i: {})
    step(env.store, env.server)
    assert env.job().state == "queued"


def test_failed_close_is_retried_next_step(env, monkeypatch):
    env.add()
    real, calls = env.server.close, []

    def flaky(jid, outcome):
        calls.append(outcome)
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        real(jid, outcome)
    monkeypatch.setattr(env.server, "close", flaky)
    step(env.store, env.server)
    assert env.job().state == "completed" and not env.job().reported
    assert env.jobs.store.get(env.jid).closed_at is None
    step(env.store, env.server)
    assert env.job().reported and env.jobs.store.get(env.jid).outcome == "delivered"
    step(env.store, env.server)
    assert calls == ["delivered", "delivered"]


def test_server_retried_state_fails_agent_job(env, monkeypatch):
    env.add()
    monkeypatch.setattr(env.server, "status", lambda i: {"state": "retried", "progress": 0.0, "error": None, "files": []})
    step(env.store, env.server)
    assert (env.job().state, env.job().fail_message) == ("failed", "Relancé chez A")


def test_progress_write_never_resurrects_cancelled(env):
    env.add("transferring")
    assert env.store.set_done("n1", 10) and env.job().done == 10
    _cancel(env)
    assert not env.store.set_done("n1", 20)
    assert (env.job().state, env.job().done) == ("cancelled", 10)


def test_server_client_has_read_timeout():
    t = ServerClient("http://x", "t").client.timeout
    assert (t.read, t.connect) == (60, 30)


def test_close_retry_error_does_not_abort_step(env, monkeypatch):
    env.add("completed")
    env.store.add(AgentJob("n2", env.jid, "t", "radarr", "cancelled", 1, 0.0, 0, str(env.dir), None, False, 1.0))
    seen = []

    def close(jid, outcome):
        seen.append(outcome)
        if outcome == "delivered":
            raise RuntimeError("weird")
    monkeypatch.setattr(env.server, "close", close)
    step(env.store, env.server)
    assert seen == ["delivered", "cancelled"] and not env.job().reported and env.store.get("n2").reported
