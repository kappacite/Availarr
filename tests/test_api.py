import pytest
from fastapi.testclient import TestClient

from availarr.server.api import create_api, iter_file
from availarr.server.db import JobStore
from availarr.server.jobs import Jobs
from availarr.server.library import Unit
from availarr.server.quality import FileInfo
from tests.fakes import FakeLibrary, make_cfg

DATA = bytes(range(250)) * 4
P = "HD-1080p FR"
H = {"Authorization": "Bearer change-me"}
BODY = {"item": "movie:603", "b_profile": P}


@pytest.fixture
def env(tmp_path):
    f = tmp_path / "Dune.mkv"
    f.write_bytes(DATA)
    info = FileInfo(1080, "Bluray-1080p", ["French", "English"], str(f), 1000)
    lib = FakeLibrary([Unit("movie:603", True, True, info)])
    cfg = make_cfg(bandwidth_mbps=0)
    jobs = Jobs(JobStore(str(tmp_path / "j.db")), lib, lib, cfg, lambda: 42.0)
    return TestClient(create_api(jobs, cfg)), jobs


@pytest.fixture
def jid(env):
    return env[0].post("/v1/requests", json={**BODY, "release_title": "x"}, headers=H).json()["job_id"]


def test_requires_token(env):
    c = env[0]
    assert c.post("/v1/lookup", json=BODY).status_code == 401
    assert c.post("/v1/lookup", json=BODY, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_lookup_and_request_flow(env, jid):
    c = env[0]
    assert c.post("/v1/lookup", json=BODY, headers=H).json()["release"]["size"] == 1000
    s = c.get(f"/v1/requests/{jid}", headers=H).json()
    assert s["state"] == "ready" and s["files"][0]["size"] == 1000


def test_file_full_and_range(env, jid):
    c, jobs = env
    r = c.get(f"/v1/requests/{jid}/files/0", headers=H)
    assert r.status_code == 200 and r.content == DATA
    r = c.get(f"/v1/requests/{jid}/files/0", headers={**H, "Range": "bytes=600-"})
    assert r.status_code == 206 and r.headers["content-range"] == "bytes 600-999/1000" and r.content == DATA[600:]
    assert jobs.transfer[jid] == (1000, 1000)
    assert c.get(f"/v1/requests/{jid}/files/5", headers=H).status_code == 404


def test_range_past_end_416(env, jid):
    r = env[0].get(f"/v1/requests/{jid}/files/0", headers={**H, "Range": "bytes=1000-"})
    assert r.status_code == 416


def test_close_sets_closed_at_and_outcome(env, jid):
    c, jobs = env
    assert c.post(f"/v1/requests/{jid}/close", json={"outcome": "delivered"}, headers=H).status_code == 204
    job = jobs.store.get(jid)
    assert job.outcome == "delivered" and job.closed_at == 42.0


def test_unknown_job_404(env):
    c = env[0]
    assert c.get("/v1/requests/99", headers=H).status_code == 404
    assert c.get("/v1/requests/99/files/0", headers=H).status_code == 404
    assert c.post("/v1/requests/99/close", json={"outcome": "cancelled"}, headers=H).status_code == 404


@pytest.mark.parametrize("key", ["bogus:1", "movie", "movie:x", "season:1", "movie:1:2"])
def test_bad_item_key_400(env, key):
    assert env[0].post("/v1/lookup", json={**BODY, "item": key}, headers=H).status_code == 400
    assert env[0].post("/v1/requests", json={**BODY, "item": key, "release_title": "x"}, headers=H).status_code == 400


def test_unmapped_profile_400(env):
    env[1].cfg.profiles.pop("default", None)
    r = env[0].post("/v1/requests", json={**BODY, "b_profile": "Nope", "release_title": "x"}, headers=H)
    assert r.status_code == 400 and r.json()["detail"] == "unmapped profile: Nope"


def test_iter_file_throttles(tmp_path):
    p = tmp_path / "big"
    content = b"x" * (2 << 20)
    p.write_bytes(content)
    now, slept = [0.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s

    out = b"".join(iter_file(str(p), 0, 8, clock=lambda: now[0], sleep=sleep))
    assert out == content and sum(slept) >= 2.0


def test_size_comes_from_disk(env, jid):
    c, jobs = env
    jobs.lib(jobs.store.get(jid).item).units(None)[0].file.size = 5000  # recorded size lies
    r = c.get(f"/v1/requests/{jid}/files/0", headers={**H, "Range": "bytes=600-"})
    assert r.headers["content-range"] == "bytes 600-999/1000" and r.headers["content-length"] == "400"
    assert c.get(f"/v1/requests/{jid}/files/0", headers={**H, "Range": "bytes=1000-"}).status_code == 416
    assert jobs.transfer[jid] == (1000, 1000)


def test_early_close_reports_only_yielded_and_closes_file(env, tmp_path):
    from availarr.server.api import _stream
    jobs = env[1]
    p = tmp_path / "Dune.mkv"
    g = _stream(jobs, 7, str(p), 0, 1000, 0, chunk=100)
    next(g)
    assert 7 not in jobs.transfer
    next(g)
    assert jobs.transfer[7] == (100, 1000)
    g.close()
    p.unlink()  # fails on Windows if the handle is still open
    assert jobs.transfer[7] == (100, 1000)


def test_transfer_keeps_max(env, jid):
    from availarr.server.api import _stream
    c, jobs = env
    c.get(f"/v1/requests/{jid}/files/0", headers=H)
    c.get(f"/v1/requests/{jid}/files/0", headers={**H, "Range": "bytes=900-"})
    assert jobs.transfer[jid] == (1000, 1000)
    path = jobs.file_path(jobs.store.get(jid), 0)[0]
    g = _stream(jobs, jid, path, 0, 1000, 0, chunk=100)
    next(g); next(g); g.close()
    assert jobs.transfer[jid] == (1000, 1000)


def test_no_sleep_after_last_chunk(tmp_path):
    from availarr.server.api import iter_file
    p = tmp_path / "f"
    p.write_bytes(b"x" * 100)
    slept = []
    g = iter_file(str(p), 0, 8, chunk=100, clock=lambda: 0.0, sleep=slept.append)
    next(g)
    n = len(slept)
    assert list(g) == [] and len(slept) == n  # paced before the chunk, nothing after it


def test_deleted_job_file_410_and_status_failed(env, jid):
    c, jobs = env
    jobs.store.set(jid, deleted_at=1.0)
    assert c.get(f"/v1/requests/{jid}/files/0", headers=H).status_code == 410
    s = c.get(f"/v1/requests/{jid}", headers=H).json()
    assert s["state"] == "failed" and s["error"] == "Supprimé chez A" and s["files"] == []
