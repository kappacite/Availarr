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
    assert fmt_seed(2 * D + 4 * 3600, 3) == "2 j 04 h / 3 j"
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
    t = env[3].get("/", auth=AUTH).text
    assert "<script>x</script>" not in t and "&lt;script&gt;x" in t


def test_removal_waiting_for_seed(env):
    jobs, qbit, mk, c = env
    mk(decision="delete")
    qbit.seed["h"] = D
    assert "⏳ dès 3 j de seed" in c.get("/", auth=AUTH).text


def test_already_there_and_qbit_error(env):
    jobs, qbit, mk, c = env
    j = mk(hashes=[], protected_units=["movie:603"])
    assert "Déjà chez toi" in c.get("/", auth=AUTH).text
    jobs.transfer[j.id] = (50, 200)
    t = c.get("/fragment", auth=AUTH).text
    assert "25 %" in t and "50 o / 200 o" in t and 'aria-valuenow="25"' in t
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


def test_download_bar_and_filter_kept_on_action(env):
    jobs, qbit, mk, c = env
    j = mk(state="pending")
    jobs.status = lambda job: {"progress": 0.42}
    t = c.get("/fragment?f=active", auth=AUTH).text
    assert "Téléchargement chez toi" in t and "42 %" in t and "Transfert vers le receveur" in t
    r = c.post(f"/jobs/{j.id}/keep?f=active", auth=AUTH)
    assert r.headers["location"] == "/?f=active"


def test_agent_status(env):
    jobs, _, _, c = env
    assert "Agent hors ligne, jamais vu" in c.get("/fragment", auth=AUTH).text
    jobs.store.seen("legacy", "Agent", -30)
    assert "Agent en ligne" in c.get("/fragment", auth=AUTH).text
    jobs.store.seen("legacy", "Agent", -600)
    assert "Agent hors ligne, vu il y a 10 min" in c.get("/fragment", auth=AUTH).text


def test_seed_line_while_downloading(env):
    from availarr.server.qbit import Torrent
    jobs, qbit, mk, c = env
    mk(state="pending", hashes=[])
    jobs.radarr.queue_hashes = ["h"]
    qbit.torrents["h"] = Torrent("h", "n", "downloading", 0.4, None, 0, 11_744_051, 0.0, 41)
    jobs.status = lambda job: {"progress": 0.4}
    assert "↓ 11,2 Mo/s · 41 pairs" in c.get("/", auth=AUTH).text


def test_seed_line_seeding(env):
    from availarr.server.qbit import Torrent
    jobs, qbit, mk, c = env
    mk()
    qbit.torrents["h"] = Torrent("h", "n", "stalledUP", 1.0, 7 * 3600, 1_887_437, 0, 0.42, 3)
    t = c.get("/", auth=AUTH).text
    assert "reste <b>2 j 17 h</b>" in t and "↑ 1,8 Mo/s" in t and "ratio 0,42" in t


def test_removal_waits_while_still_downloading(env):
    from availarr.server.qbit import Torrent
    jobs, qbit, mk, c = env
    mk(decision="delete")
    qbit.torrents["h"] = Torrent("h", "n", "downloading", 0.4, None, 0, 1, 0.0, 1)
    assert "⏳ dès 3 j de seed" in c.get("/", auth=AUTH).text


def test_cancelled_row_is_frozen(env):
    jobs, qbit, mk, c = env
    jobs.lib(M).progress = 0.5
    a = mk("Cancelled", state="pending", outcome="cancelled")
    b = mk("Active", state="pending")
    jobs.lib(M).calls.clear()
    jobs.status = lambda job: {"progress": 0.5}
    t = c.get("/fragment", auth=AUTH).text
    rows = t.split('<li class="job')
    ca = next(r for r in rows if "Cancelled" in r)
    ac = next(r for r in rows if "Active" in r)
    assert "50 %" not in ca and "50 %" in ac
    assert "Annulé chez le receveur" in ca and "figé, demande annulée" in ca
    assert "Supprimer" in ca and "Retirer" in ca


def test_refuse_fails_job_and_removes_entry(env):
    jobs, qbit, mk, c = env
    j = mk(state="pending")
    assert c.post(f"/jobs/{j.id}/refuse", auth=AUTH).status_code == 303
    j = jobs.store.get(j.id)
    assert (j.state, j.error) == ("failed", "Refusée par l'envoyeur")
    assert jobs.lib(M).removed
    t = c.get("/fragment", auth=AUTH).text
    assert "Refusée" in t and "Réessayer" in t and "Retirer" in t


def test_refuse_shared_entry_keeps_files(env):
    jobs, qbit, mk, c = env
    j = mk(state="pending")
    o = jobs.store.create(M, P, "o", ["movie:603"], [], True, 0, "a2")
    jobs.store.set(o.id, state="pending")
    assert c.post(f"/jobs/{j.id}/refuse", auth=AUTH).status_code == 303
    assert jobs.lib(M).removed == []


def test_refuse_delivered_409(env):
    jobs, qbit, mk, c = env
    j = mk(outcome="delivered")
    assert c.post(f"/jobs/{j.id}/refuse", auth=AUTH).status_code == 409


def test_hide_only_when_finished(env):
    jobs, qbit, mk, c = env
    a = mk("Live", state="pending")
    d = mk("Done", outcome="delivered")
    assert c.post(f"/jobs/{a.id}/hide", auth=AUTH).status_code == 409
    assert c.post(f"/jobs/{d.id}/hide", auth=AUTH).status_code == 303
    t = c.get("/fragment", auth=AUTH).text
    assert "Live" in t and "Done" not in t


def test_refused_job_blocks_until_retry(env):
    jobs, qbit, mk, c = env
    j = mk(state="pending")
    c.post(f"/jobs/{j.id}/refuse", auth=AUTH)
    assert jobs.lookup(M, P, agent="legacy") is None
    c.post(f"/jobs/{j.id}/retry", auth=AUTH)
    assert jobs.lookup(M, P, agent="legacy")
