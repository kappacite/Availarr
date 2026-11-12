import time

from fastapi.testclient import TestClient

from availarr.common import Item
from availarr.server import stats
from availarr.server.db import JobStore
from availarr.server.jobs import Jobs
from availarr.server.panel import create_panel
from tests.fakes import FakeLibrary, FakeQbit, make_cfg

NOW = time.time()


def store(tmp_path):
    s = JobStore(str(tmp_path / "s.db"))
    a = s.create(Item("movie", 1), "P", "Dune.2024.1080p-Availarr", ["movie:1"], [], True, NOW - 7200)
    s.set(a.id, outcome="delivered", closed_at=NOW - 3600)
    a.state, a.ready_at = "ready", NOW - 5400
    s.update(a)
    b = s.create(Item("movie", 2), "P", "Old.2001.1080p-Availarr", ["movie:2"], ["movie:2"], False, NOW - 600)
    b.state, b.ready_at = "ready", NOW - 600
    s.update(b)
    s.add_transfer(a.id, NOW - 4000, 100, 1000 * 2**20, True)
    s.add_transfer(a.id, NOW - 3900, 10, 50 * 2**20, False)
    return s


def test_compute(tmp_path):
    s = store(tmp_path)
    st = stats.compute(s.all(), s.transfers(), NOW)
    assert st["delivered"] == 1 and st["kinds"]["movie"] == 1
    assert (st["downloaded"], st["from_library"], st["interrupted"]) == (1, 1, 1)
    assert st["dl_mean"] == 1800 and st["lead_median"] == 3600
    assert st["sent"] == 1050 * 2**20 and st["rate_mean"] == 1050 * 2**20 / 110
    assert st["rate_last"] == 5 * 2**20 and st["rate_best"] == 10 * 2**20
    assert st["days"][-1][1] == 1050 * 2**20 and len(st["days"]) == stats.DAYS
    assert s.avg_rate() == 1050 * 2**20 / 110


def test_stats_page(tmp_path):
    s = store(tmp_path)
    jobs = Jobs(s, FakeLibrary([]), FakeLibrary([]), make_cfg())
    c = TestClient(create_panel(jobs, FakeQbit({}), jobs.cfg))
    t = c.get("/stats", auth=("x", "change-me")).text
    assert "Statistiques" in t and "Dune (2024)" in t and "Interrompu" in t and 'aria-current=page' in t
    assert c.get("/stats/fragment", auth=("x", "change-me")).status_code == 200
    empty = Jobs(JobStore(str(tmp_path / "e.db")), FakeLibrary([]), FakeLibrary([]), make_cfg())
    assert "Aucun envoi" in TestClient(create_panel(empty, FakeQbit({}), empty.cfg)).get(
        "/stats", auth=("x", "change-me")).text
