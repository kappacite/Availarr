import pytest

from availarr.common import Item
from availarr.server.db import JobStore
from availarr.server.jobs import Jobs
from availarr.server.library import Unit
from availarr.server.retention import seed_ok, seed_seconds, tick
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
        jobs.store.set(j.id, **{k: getattr(j, k) for k in ("decision", "closed_at", "outcome", "deleted_at")})
        return jobs.store.get(j.id)
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
    assert lib.removed == [(S, ["episode:1:1:2"], False)]


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

    def flaky(item, *rest):
        if item == M:
            raise RuntimeError("boom")
        real(item, *rest)
    lib.remove = flaky
    assert tick(jobs, qbit, 7 * D) == [b.id]
    assert jobs.store.get(a.id).deleted_at is None


def test_tick_refreshes_pending(env):
    jobs, lib, qbit, mk = env
    j = mk(state="pending", closed_at=None)
    tick(jobs, qbit, 0)
    assert jobs.store.get(j.id).state == "ready"


def test_refresh_error_does_not_block_other_jobs(env):
    jobs, lib, qbit, mk = env
    qbit.seed["h"] = 3 * D
    bad = mk(state="pending", closed_at=None)
    good = mk(item=Item("movie", 7), units=["movie:7"])
    real = jobs.refresh

    def refresh(job):
        if job.id == bad.id:
            raise RuntimeError("boom")
        return real(job)
    jobs.refresh = refresh
    assert tick(jobs, qbit, 7 * D) == [good.id]


def test_seed_seconds_and_multi_hash(env):
    jobs, lib, qbit, mk = env
    j = mk()
    j.hashes = ["a", "b", "c"]
    qbit.seed.update(a=5 * D, b=2 * D, c=None)
    assert seed_seconds(j, qbit) == 2 * D and not seed_ok(j, qbit, 3)
    qbit.seed.update(a=None, b=None)
    assert seed_seconds(j, qbit) is None and seed_ok(j, qbit, 3)
    j.hashes = []
    assert seed_seconds(j, qbit) is None and seed_ok(j, qbit, 3)
    j.hashes = ["a", "b"]
    qbit.seed.update(a=None, b=2 * D)
    assert not seed_ok(j, qbit, 3)


def test_keep_set_during_eligibility_is_honoured(env):
    jobs, lib, qbit, mk = env
    j = mk()

    class FlipQbit(FakeQbit):
        def seeding_seconds(self, h):  # the panel clicks "Garder" while the tick is judging
            jobs.store.set(j.id, decision="keep")
            return 9 * D
    q = FlipQbit({})
    assert tick(jobs, q, 100 * D) == [] and lib.removed == [] and q.deleted == []
    assert jobs.store.get(j.id).deleted_at is None
