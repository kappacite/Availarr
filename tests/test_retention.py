import pytest

from availarr.common import Item
from availarr.server.db import JobStore
from availarr.server.jobs import Jobs
from availarr.server.library import Unit
from availarr.server.retention import tick
from tests.fakes import FakeLibrary, FakeQbit, make_cfg
from tests.test_jobs import Clock, GOOD, M, P

D = 86400


@pytest.fixture
def env(tmp_path):
    lib = FakeLibrary([Unit("movie:603", False, False, GOOD)])
    jobs = Jobs(JobStore(str(tmp_path / "r.db")), lib, lib, make_cfg(), Clock())
    qbit = FakeQbit({"h": 5 * D})

    def mk(item=M, units=("movie:603",), protected=(), added=True, **kw):
        j = jobs.store.create(item, P, "t", list(units), list(protected), added, 0)
        j.state, j.closed_at, j.hashes = "ready", 0, ["h"]
        for k, v in kw.items():
            setattr(j, k, v)
        jobs.store.update(j)
        return j
    return jobs, lib, qbit, mk


def test_not_deleted_before_keep_days(env):
    jobs, lib, qbit, mk = env
    mk()
    assert tick(jobs, qbit, 6.9 * D) == [] and lib.removed == []


def test_deleted_after_keep_days_and_seed(env):
    jobs, lib, qbit, mk = env
    j = mk()
    qbit.seed["h"] = 3 * D
    assert tick(jobs, qbit, 7 * D) == [j.id]
    assert lib.removed == [(M, ["movie:603"], True)] and qbit.deleted == ["h"]
    assert jobs.store.get(j.id).deleted_at == 7 * D
    assert tick(jobs, qbit, 8 * D) == []


def test_waits_for_seed(env):
    jobs, lib, qbit, mk = env
    mk()
    qbit.seed["h"] = int(2.9 * D)
    assert tick(jobs, qbit, 10 * D) == [] and qbit.deleted == []


def test_torrent_absent_counts_as_seeded(env):
    jobs, lib, qbit, mk = env
    j = mk()
    qbit.seed["h"] = None
    assert tick(jobs, qbit, 7 * D) == [j.id]


def test_keep_never_deleted(env):
    jobs, lib, qbit, mk = env
    mk(decision="keep")
    qbit.seed["h"] = 9 * D
    assert tick(jobs, qbit, 100 * D) == [] and lib.removed == [] and qbit.deleted == []


def test_protected_never_deleted(env):
    jobs, lib, qbit, mk = env
    mk(protected=["movie:603"])
    qbit.seed["h"] = 9 * D
    assert tick(jobs, qbit, 100 * D) == [] and lib.removed == [] and qbit.deleted == []


def test_manual_delete_waits_for_seed_then_deletes(env):
    jobs, lib, qbit, mk = env
    j = mk(decision="delete", closed_at=None)
    qbit.seed["h"] = 1 * D
    assert tick(jobs, qbit, 1) == []
    qbit.seed["h"] = 3 * D
    assert tick(jobs, qbit, 1) == [j.id]


def test_mixed_protected_removes_only_unprotected_units(env):
    jobs, lib, qbit, mk = env
    S = Item("season", 1, 1)
    qbit.seed["h"] = 3 * D
    mk(item=S, units=["episode:1:1:1", "episode:1:1:2"], protected=["episode:1:1:1"])
    assert len(tick(jobs, qbit, 7 * D)) == 1
    assert lib.removed == [(S, ["episode:1:1:2"], True)]


def test_drop_entry_false_when_not_added_or_shared(env):
    jobs, lib, qbit, mk = env
    qbit.seed["h"] = 3 * D
    mk(added=False)
    tick(jobs, qbit, 7 * D)
    assert lib.removed[-1][2] is False
    mk(item=Item("season", 5, 1), units=["episode:5:1:1"])
    mk(item=Item("episode", 5, 1, 2), units=["episode:5:1:2"], closed_at=7 * D)  # same series, still active
    tick(jobs, qbit, 7 * D)
    assert lib.removed[-1][2] is False


def test_failure_does_not_stop_other_jobs(env):
    jobs, lib, qbit, mk = env
    qbit.seed["h"] = 3 * D
    a, b = mk(), mk(item=Item("movie", 7), units=["movie:7"])
    real = lib.remove
    lib.remove = lambda item, *r: (_ for _ in ()).throw(RuntimeError()) if item == M else real(item, *r)
    assert tick(jobs, qbit, 7 * D) == [b.id]
    assert jobs.store.get(a.id).deleted_at is None


def test_tick_refreshes_pending(env):
    jobs, lib, qbit, mk = env
    j = mk(state="pending", closed_at=None)
    tick(jobs, qbit, 0)
    assert jobs.store.get(j.id).state == "ready"
