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
    r = c.get("/agents/legacy", auth=AUTH)
    assert "Dune" in r.text and "2 j 04 h / 3 j" in r.text


def test_filters(env):
    jobs, qbit, mk, c = env
    mk("Kept", decision="keep")
    mk("Undecided")
    t = c.get("/agents/legacy?f=kept", auth=AUTH).text
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
    t = env[3].get("/agents/legacy", auth=AUTH).text
    assert "<script>x</script>" not in t and "&lt;script&gt;x" in t


def test_removal_waiting_for_seed(env):
    jobs, qbit, mk, c = env
    mk(decision="delete")
    qbit.seed["h"] = D
    assert "⏳ dès 3 j de seed" in c.get("/agents/legacy", auth=AUTH).text


def test_already_there_and_qbit_error(env):
    jobs, qbit, mk, c = env
    j = mk(hashes=[], protected_units=["movie:603"])
    assert "Déjà chez toi" in c.get("/agents/legacy", auth=AUTH).text
    jobs.transfer[j.id] = (50, 200)
    t = c.get("/agents/legacy/fragment", auth=AUTH).text
    assert "25 %" in t and "50 o / 200 o" in t and 'aria-valuenow="25"' in t
    qbit.seeding_seconds = lambda h: 1 / 0
    mk("Boom")
    assert c.get("/agents/legacy", auth=AUTH).status_code == 200


def test_pending_row_renders_when_library_down(env):
    jobs, qbit, mk, c = env
    mk(state="pending")

    def down(*a):
        raise RuntimeError("radarr down")
    jobs.status = down
    r = c.get("/agents/legacy", auth=AUTH)
    assert r.status_code == 200 and "Téléchargement …" in r.text


def test_download_bar_and_filter_kept_on_action(env):
    jobs, qbit, mk, c = env
    j = mk(state="pending")
    jobs.status = lambda job: {"progress": 0.42}
    t = c.get("/agents/legacy/fragment?f=active", auth=AUTH).text
    assert "Téléchargement chez toi" in t and "42 %" in t and "Transfert vers le receveur" in t
    r = c.post(f"/jobs/{j.id}/keep?f=active", auth=AUTH)
    assert r.headers["location"] == "/?f=active"


def test_seed_line_while_downloading(env):
    from availarr.server.qbit import Torrent
    jobs, qbit, mk, c = env
    mk(state="pending", hashes=[])
    jobs.radarr.queue_hashes = ["h"]
    qbit.torrents["h"] = Torrent("h", "n", "downloading", 0.4, None, 0, 11_744_051, 0.0, 41)
    jobs.status = lambda job: {"progress": 0.4}
    assert "↓ 11,2 Mo/s · 41 pairs" in c.get("/agents/legacy", auth=AUTH).text


def test_seed_line_seeding(env):
    from availarr.server.qbit import Torrent
    jobs, qbit, mk, c = env
    mk()
    qbit.torrents["h"] = Torrent("h", "n", "stalledUP", 1.0, 7 * 3600, 1_887_437, 0, 0.42, 3)
    t = c.get("/agents/legacy", auth=AUTH).text
    assert "reste <b>2 j 17 h</b>" in t and "↑ 1,8 Mo/s" in t and "ratio 0,42" in t


def test_removal_waits_while_still_downloading(env):
    from availarr.server.qbit import Torrent
    jobs, qbit, mk, c = env
    mk(decision="delete")
    qbit.torrents["h"] = Torrent("h", "n", "downloading", 0.4, None, 0, 1, 0.0, 1)
    assert "⏳ dès 3 j de seed" in c.get("/agents/legacy", auth=AUTH).text


def test_cancelled_row_is_frozen(env):
    jobs, qbit, mk, c = env
    jobs.lib(M).progress = 0.5
    a = mk("Cancelled", state="pending", outcome="cancelled")
    b = mk("Active", state="pending")
    jobs.lib(M).calls.clear()
    jobs.status = lambda job: {"progress": 0.5}
    seen = []
    orig = qbit.info
    qbit.info = lambda hs: seen.append(list(hs)) or orig(hs)
    t = c.get("/agents/legacy/fragment", auth=AUTH).text
    rows = t.split('<li class="job')
    ca = next(r for r in rows if "Cancelled" in r)
    ac = next(r for r in rows if "Active" in r)
    assert "50 %" not in ca and "50 %" in ac
    assert "Annulé chez le receveur" in ca and "figé, demande annulée" in ca
    assert "Supprimer" in ca and "Retirer" in ca
    assert len(seen) == 1  # only the active row asked qBittorrent


def test_refuse_fails_job_and_removes_entry(env):
    jobs, qbit, mk, c = env
    j = mk(state="pending")
    assert c.post(f"/jobs/{j.id}/refuse", auth=AUTH).status_code == 303
    j = jobs.store.get(j.id)
    assert (j.state, j.error) == ("failed", "Refusée par l'envoyeur")
    assert jobs.lib(M).removed
    t = c.get("/agents/legacy/fragment", auth=AUTH).text
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
    t = c.get("/agents/legacy/fragment", auth=AUTH).text
    assert "Live" in t and "Done" not in t


def test_refused_job_blocks_until_retry(env):
    jobs, qbit, mk, c = env
    j = mk(state="pending")
    c.post(f"/jobs/{j.id}/refuse", auth=AUTH)
    assert jobs.lookup(M, P, agent="legacy") is None
    c.post(f"/jobs/{j.id}/retry", auth=AUTH)
    assert jobs.lookup(M, P, agent="legacy")


def test_refuse_failed_job_409(env):
    jobs, qbit, mk, c = env
    f = mk(state="failed", error="x")
    assert c.post(f"/jobs/{f.id}/refuse", auth=AUTH).status_code == 409


def test_refuse_cleanup_error_is_not_a_500(env):
    jobs, qbit, mk, c = env
    j = mk(state="pending")

    def boom(*a):
        raise RuntimeError("radarr down")
    jobs.lib(M).remove = boom
    assert c.post(f"/jobs/{j.id}/refuse", auth=AUTH).status_code == 303
    assert jobs.store.get(j.id).error == "Refusée par l'envoyeur"


def test_cancelled_not_counted_as_downloading_or_to_decide(env):
    jobs, qbit, mk, c = env
    mk(state="pending", outcome="cancelled")
    assert "Rien en cours" in c.get("/agents/legacy/fragment", auth=AUTH).text


def mkj(jobs, agent, title="T", **cols):
    j = jobs.store.create(M, P, title, ["movie:603"], [], True, 0, agent)
    jobs.store.set(j.id, **{"state": "ready", "hashes": ["h"], **cols})
    return jobs.store.get(j.id)


def test_agents_list(env):
    jobs, _, _, c = env
    jobs.store.seen("a1", "Lyon", -10)
    jobs.store.seen("a2", "Chalet", -500)
    mkj(jobs, "a1", "Dune.Part.Two.2024.MULTi.2160p.WEB-DL-Availarr", state="pending", created_at=5)
    mkj(jobs, "a1", "B")
    mkj(jobs, "a1", "C", outcome="delivered", decision="keep")
    mkj(jobs, "a2", "D", outcome="delivered", decision="keep")
    t = c.get("/", auth=AUTH).text
    assert "Lyon" in t and "Chalet" in t
    assert t.count(">En ligne<") == 1 and t.count("Hors ligne, vu il y a 8 min") == 1
    assert t.index("Lyon") < t.index("Chalet")
    a1 = t.split('href="/agents/a1"')[1].split("</a>")[0]
    assert "<b>2</b><span>En cours</span>" in a1 and "<b>2</b><span>À décider</span>" in a1
    assert "<b>1</b><span>Livrés</span>" in a1 and "Dune Part Two (2024)" in a1
    a2 = t.split('href="/agents/a2"')[1].split("</a>")[0]
    assert "<b>0</b><span>En cours</span>" in a2 and "<b>1</b><span>Livrés</span>" in a2
    assert c.get("/fragment", auth=AUTH).text.count('class="card agent') == 2


def test_blocked_agent_card(env):
    jobs, _, _, c = env
    jobs.store.seen("a1", "Lyon", -10)
    jobs.store.set_agent("a1", blocked=1)
    t = c.get("/", auth=AUTH).text
    assert "Bloqué" in t and ">En ligne<" not in t and "agent blocked" in t


def test_agent_name_escaped(env):
    jobs, _, _, c = env
    jobs.store.seen("a1", "<script>x</script>", -10)
    mkj(jobs, "a1")
    for url in ("/", "/agents/a1", "/jobs/1"):
        t = c.get(url, auth=AUTH).text
        assert "&lt;script&gt;" in t and "<script>x" not in t


def test_agent_id_escaped_in_urls(env):
    jobs, _, _, c = env
    jobs.store.seen('a"<b>', "N", -10)
    t = c.get("/", auth=AUTH).text
    assert 'href="/agents/a%22%3Cb%3E"' in t and 'a"<b>' not in t


def test_agent_page_filters_by_agent(env):
    jobs, _, _, c = env
    jobs.store.seen("a1", "Lyon", -10)
    jobs.store.seen("a2", "Chalet", -10)
    mkj(jobs, "a1", "Alpha.2020.x")
    mkj(jobs, "a2", "Beta.2021.x")
    t = c.get("/agents/a1", auth=AUTH).text
    assert "Alpha" in t and "Beta" not in t and "Lyon" in t
    assert 'href="/jobs/1"' in t and "back=%2Fagents%2Fa1" in t
    assert "Beta" in c.get("/agents/a2/fragment", auth=AUTH).text


def test_rename_and_block(env):
    jobs, _, _, c = env
    jobs.store.seen("a1", "Lyon", -10)
    r = c.post("/agents/a1/rename", data={"name": " Maison "}, auth=AUTH)
    assert r.status_code == 303 and r.headers["location"] == "/agents/a1"
    a = jobs.store.agent("a1")
    assert a.name == "Maison" and a.renamed
    c.post("/agents/a1/rename", data={"name": "  "}, auth=AUTH)
    assert jobs.store.agent("a1").name == "Maison"
    assert c.post("/agents/a1/block", auth=AUTH).status_code == 303
    assert jobs.store.agent("a1").blocked
    assert "Débloquer" in c.get("/agents/a1", auth=AUTH).text
    c.post("/agents/a1/unblock", auth=AUTH)
    assert not jobs.store.agent("a1").blocked
    assert c.post("/agents/zz/block", auth=AUTH).status_code == 404


def test_job_detail(env):
    jobs, qbit, mk, c = env
    jobs.store.seen("a1", "Lyon", -10)
    j = mkj(jobs, "a1", "Dune.Part.Two.2024.MULTi.2160p.WEB-DL-Availarr", created_at=3600, ready_at=5400)
    jobs.store.add_transfer(j.id, 900, 60, 600_000_000, True)
    jobs.store.add_transfer(j.id, 300, 10, 1000, False)
    jobs.store.add_transfer(j.id + 99, 300, 10, 5, True)
    t = c.get(f"/jobs/{j.id}", auth=AUTH).text
    assert "Lyon" in t and P in t and "1080p MULTi" in t and "Dune.mkv" in t
    assert "Dune Part Two (2024)" in t and "Torrent chez toi" in t
    assert t.count("<td>Complet</td>") == 1 and t.count("<td>Interrompu</td>") == 1
    assert "Demandée par le receveur" in t and c.get(f"/jobs/{j.id}/fragment", auth=AUTH).status_code == 200


def test_job_detail_qbit_down_and_cancelled(env):
    jobs, qbit, mk, c = env
    a = mk("Live")
    b = mk("Cancelled", state="pending", outcome="cancelled")
    qbit.info = lambda hs: 1 / 0
    t = c.get(f"/jobs/{a.id}", auth=AUTH).text
    assert "qBittorrent injoignable" in t
    calls = []
    qbit.info = lambda hs: calls.append(hs) or {}
    jobs.status = lambda job: calls.append("status") or {"files": []}
    t = c.get(f"/jobs/{b.id}", auth=AUTH).text
    assert calls == [] and "Annulé chez le receveur" in t


def test_unknown_agent_404(env):
    assert env[3].get("/agents/nope", auth=AUTH).status_code == 404
    assert env[3].get("/agents/nope/fragment", auth=AUTH).status_code == 404


def test_unknown_job_404(env):
    assert env[3].get("/jobs/99", auth=AUTH).status_code == 404
    assert env[3].get("/jobs/99/fragment", auth=AUTH).status_code == 404


def test_back_redirect_is_local(env):
    jobs, _, mk, c = env
    j = mk()
    loc = lambda back: c.post(f"/jobs/{j.id}/keep?f=kept&back={back}", auth=AUTH).headers["location"]
    assert loc("https://evil") == "/?f=kept"
    assert loc("//evil.com") == "/?f=kept"
    assert loc("/agents/a1/x") == "/?f=kept"
    assert loc("/agents/a%2520b") == "/agents/a%20b?f=kept"
    assert loc("/jobs/5") == "/jobs/5?f=kept"
    assert loc("/jobs/x") == "/?f=kept"


def test_legacy_only_agent_card(env):
    jobs, qbit, mk, c = env
    mk()
    t = c.get("/", auth=AUTH).text
    assert "Hors ligne, jamais vu" in t and "Agent" in t and "20000" not in t
    jobs.store.seen("legacy", "Agent", -30)
    t = c.get("/", auth=AUTH).text
    assert ">En ligne<" in t and "jamais vu" not in t


def test_actions_on_jobs_only_agent_persist(env):
    jobs, qbit, mk, c = env
    mk()
    assert jobs.store.agent("legacy") is None
    assert c.post("/agents/legacy/rename", data={"name": "Maison"}, auth=AUTH).status_code == 303
    a = jobs.store.agent("legacy")
    assert a.name == "Maison" and a.renamed and a.last_seen is None
    assert "jamais vu" in c.get("/agents/legacy", auth=AUTH).text
    c.post("/agents/legacy/block", auth=AUTH)
    assert jobs.store.agent("legacy").blocked


def test_detail_removal_waits_for_seed(env):
    jobs, qbit, mk, c = env
    j = mk(decision="delete")
    qbit.seed["h"] = D
    assert "⏳ dès 3 j de seed" in c.get(f"/jobs/{j.id}", auth=AUTH).text


def test_detail_asks_qbit_once(env):
    jobs, qbit, mk, c = env
    j = mk()
    seen = []
    orig = qbit.info
    qbit.info = lambda hs: seen.append(hs) or orig(hs)
    c.get(f"/jobs/{j.id}", auth=AUTH)
    assert len(seen) == 1


def test_back_to_agent_page(env):
    jobs, _, mk, c = env
    j = mk()
    r = c.post(f"/jobs/{j.id}/keep?f=kept&back=%2Fagents%2Flegacy", auth=AUTH)
    assert r.headers["location"] == "/agents/legacy?f=kept"
    assert c.post(f"/jobs/{j.id}/keep?back=%2Fagents%2Fa%20b", auth=AUTH).headers["location"] == "/?f=all"
