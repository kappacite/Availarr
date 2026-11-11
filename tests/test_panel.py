import pytest
from fastapi.testclient import TestClient

from availarr.server.db import JobStore
from availarr.server.jobs import Jobs
from availarr.server.library import Unit
from availarr.server.panel import create_panel, fmt_seed
from tests.fakes import FakeLibrary, FakeQbit, make_cfg
from tests.test_jobs import Clock, GOOD, M, P

D = 86400
AUTH = ("x", "change-me")


def test_fmt_seed():
    assert fmt_seed(2 * D + 4 * 3600, 3) == "2 j 04 h / 3 j ▓▓▓▓▓▓▓░░░"
    assert fmt_seed(9 * D + 2 * 3600, 3) == "9 j 02 h ✅"
    assert fmt_seed(None, 3) == "—"


@pytest.fixture
def env(tmp_path):
    lib = FakeLibrary([Unit("movie:603", False, False, GOOD)])
    jobs = Jobs(JobStore(str(tmp_path / "p.db")), lib, lib, make_cfg(), Clock())
    qbit = FakeQbit({"h": 2 * D + 4 * 3600})

    def mk(title="Dune", **kw):
        j = jobs.store.create(M, P, title, ["movie:603"], [], True, 0)
        j.state, j.hashes = "ready", ["h"]
        for k, v in kw.items():
            setattr(j, k, v)
        jobs.store.update(j)
        jobs.store.set(j.id, **{k: getattr(j, k) for k in ("decision", "closed_at", "outcome", "deleted_at")})
        return jobs.store.get(j.id)
    return jobs, qbit, mk, TestClient(create_panel(jobs, qbit, jobs.cfg, lambda: 0), follow_redirects=False)


def test_requires_password(env):
    r = env[3].get("/")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]


def test_lists_rows_with_seed(env):
    jobs, qbit, mk, c = env
    mk()
    r = c.get("/", auth=AUTH)
    assert "Dune" in r.text and "2 j 04 h / 3 j" in r.text


def test_filters(env):
    jobs, qbit, mk, c = env
    mk("Kept", decision="keep")
    mk("Undecided")
    t = c.get("/?f=kept", auth=AUTH).text
    assert "Kept" in t and "Undecided" not in t


def test_actions(env):
    jobs, qbit, mk, c = env
    j = mk()
    for act, want in [("keep", "keep"), ("delete", "delete"), ("cancel", "none")]:
        assert c.post(f"/jobs/{j.id}/{act}", auth=AUTH).status_code == 303
        assert jobs.store.get(j.id).decision == want
    assert c.post(f"/jobs/{j.id}/retry", auth=AUTH).status_code == 409
    assert c.post("/jobs/99/keep", auth=AUTH).status_code == 404
    f = mk(state="failed", error="x")
    assert c.post(f"/jobs/{f.id}/retry", auth=AUTH).status_code == 303
    assert jobs.store.get(f.id).state == "retried"


def test_unmapped_banner(env):
    env[0].unmapped = {"Ultra"}
    assert "Ultra" in env[3].get("/", auth=AUTH).text


def test_html_escapes_title(env):
    env[2]("<script>x</script>")
    assert "<script>" not in env[3].get("/", auth=AUTH).text


def test_removal_waiting_for_seed(env):
    jobs, qbit, mk, c = env
    mk(decision="delete")
    qbit.seed["h"] = D
    assert "⏳ dès 3 j de seed" in c.get("/", auth=AUTH).text


def test_already_there_and_qbit_error(env):
    jobs, qbit, mk, c = env
    mk(hashes=[], protected_units=["movie:603"])
    assert "Déjà chez toi" in c.get("/", auth=AUTH).text
    qbit.seeding_seconds = lambda h: 1 / 0
    mk("Boom")
    assert c.get("/", auth=AUTH).status_code == 200


def test_pending_row_renders_when_library_down(env):
    jobs, qbit, mk, c = env
    mk(state="pending")

    def down(*a):
        raise RuntimeError("radarr down")
    jobs.status = down
    r = c.get("/", auth=AUTH)
    assert r.status_code == 200 and "Téléchargement …" in r.text
