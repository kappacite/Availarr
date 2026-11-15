from availarr.common import Item
from availarr.server.db import JobStore
M = Item("movie", 603)

def test_create_get_update(tmp_path):
    s = JobStore(str(tmp_path / "a.db"))
    j = s.create(M, "HD", "T", units=["movie:603"], protected_units=[], added_by_tool=True, now=100.0)
    g = s.get(j.id)
    assert (g.state, g.decision, g.item, g.created_at) == ("pending", "none", M, 100.0)
    j.state = "ready"; j.hashes = ["abc"]; s.update(j)
    assert s.get(j.id).hashes == ["abc"]

def test_active_and_failed_for(tmp_path):
    s = JobStore(str(tmp_path / "a.db"))
    j = s.create(M, "HD", "T", ["movie:603"], [], True, 1.0)
    assert s.active_for(M, "legacy").id == j.id and s.failed_for(M, "legacy") is None
    j.state = "failed"; s.update(j)
    assert s.active_for(M, "legacy") is None and s.failed_for(M, "legacy").id == j.id
    j.state = "retried"; s.update(j)
    assert s.active_for(M, "legacy") is None and s.failed_for(M, "legacy") is None

def test_deleted_not_active_and_all_order(tmp_path):
    s = JobStore(str(tmp_path / "a.db"))
    a = s.create(M, "HD", "A", ["movie:603"], [], True, 1.0)
    b = s.create(Item("movie", 1), "HD", "B", ["movie:1"], [], True, 2.0)
    s.set(a.id, deleted_at=5.0)
    assert s.active_for(M, "legacy") is None
    assert [j.id for j in s.all()] == [b.id, a.id]


def test_update_never_rewrites_owned_columns(tmp_path):
    s = JobStore(str(tmp_path / "a.db"))
    stale = s.create(M, "HD", "T", ["movie:603"], [], True, 1.0)
    s.set(stale.id, decision="keep", closed_at=9.0, outcome="delivered", deleted_at=None)
    stale.state = "ready"
    s.update(stale)  # a refresh holding a snapshot taken before the panel/API wrote
    g = s.get(stale.id)
    assert (g.state, g.decision, g.closed_at, g.outcome) == ("ready", "keep", 9.0, "delivered")
    s.set(stale.id, hashes=["x"])
    assert s.get(stale.id).hashes == ["x"]


def test_agents_seen_and_rename(tmp_path):
    s = JobStore(str(tmp_path / "a.db"))
    s.seen("a1", "Lyon", 10)
    a = s.seen("a1", "Lyon2", 20)
    assert (a.name, a.first_seen, a.last_seen) == ("Lyon2", 10, 20)
    s.set_agent("a1", name="Maison", renamed=1)
    assert s.seen("a1", "X", 30).name == "Maison" and s.agent("a1").renamed
    assert [x.id for x in s.agents()] == ["a1"] and s.agent("zz") is None


def test_migration_adds_agent_column(tmp_path):
    import sqlite3
    old = """CREATE TABLE jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, b_profile TEXT, release_title TEXT,
 state TEXT, units TEXT, protected_units TEXT, added_by_tool INTEGER, hashes TEXT,
 created_at REAL, ready_at REAL, closed_at REAL, outcome TEXT, decision TEXT,
 deleted_at REAL, error TEXT)"""
    path = str(tmp_path / "a.db")
    c = sqlite3.connect(path)
    c.execute(old)
    c.execute("INSERT INTO jobs (item,b_profile,release_title,state,units,protected_units,added_by_tool,"
              "hashes,created_at,decision) VALUES ('movie:603','HD','T','ready','[]','[]',1,'[]',1,'none')")
    c.commit(); c.close()
    j = JobStore(path).get(1)
    assert j.agent == "legacy" and j.hidden_at is None


def test_active_for_is_per_agent(tmp_path):
    s = JobStore(str(tmp_path / "a.db"))
    j = s.create(M, "HD", "T", ["movie:603"], [], True, 1.0, agent="a1")
    assert s.active_for(M, "a1").id == j.id and s.active_for(M, "a2") is None
