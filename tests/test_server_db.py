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
    assert s.active_for(M).id == j.id and s.failed_for(M) is None
    j.state = "failed"; s.update(j)
    assert s.active_for(M) is None and s.failed_for(M).id == j.id
    j.state = "retried"; s.update(j)
    assert s.active_for(M) is None and s.failed_for(M) is None

def test_deleted_not_active_and_all_order(tmp_path):
    s = JobStore(str(tmp_path / "a.db"))
    a = s.create(M, "HD", "A", ["movie:603"], [], True, 1.0)
    b = s.create(Item("movie", 1), "HD", "B", ["movie:1"], [], True, 2.0)
    a.deleted_at = 5.0; s.update(a)
    assert s.active_for(M) is None
    assert [j.id for j in s.all()] == [b.id, a.id]
