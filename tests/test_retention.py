import pytest

from availarr.common import Item
from availarr.server.db import JobStore
from availarr.server.jobs import Jobs
from availarr.server.library import Unit
from availarr.server.retention import remove_entry, seed_ok, seed_seconds, tick
from tests.fakes import FakeLibrary, FakeQbit, make_cfg
from tests.test_jobs import Clock, GOOD, M, P

D = 86400


@pytest.fixture
def env(tmp_path):
    lib = FakeLibrary([Unit("movie:603", False, False, GOOD)])
    jobs = Jobs(JobStore(str(tmp_path / "r.db")), lib, lib, make_cfg(), Clock())
    qbit = FakeQbit({"h": 5 * D})

    def mk(item=M, units=("movie:603",), protected=(), added=True, agent="legacy", **kw):
        j = jobs.store.create(item, P, "t", list(units), list(protected), added, 0, agent)
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
    assert lib.removed[-1] == (Item("season", 5, 1), ["episode:5:1:1"], False)


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


def test_keep_set_during_second_check_is_honoured(env):
    jobs, lib, qbit, mk = env
    j = mk()

    class FlipQbit(FakeQbit):
        calls = 0

        def seeding_seconds(self, h):
            self.calls += 1
            if self.calls == 2:  # the second eligibility round
                jobs.store.set(j.id, decision="keep")
            return 9 * D
    q = FlipQbit({})
    assert tick(jobs, q, 100 * D) == [] and lib.removed == [] and q.deleted == []
    assert q.calls == 2 and jobs.store.get(j.id).deleted_at is None


def test_requested_again_newest_job_decides(env):
    jobs, lib, qbit, mk = env
    old = mk()
    new = mk(closed_at=None)
    assert tick(jobs, qbit, 30 * D) == [] and lib.removed == []  # old one expired, but B asked again
    jobs.store.set(new.id, closed_at=30 * D)
    assert tick(jobs, qbit, 37 * D) == [new.id]
    assert lib.removed == [(M, ["movie:603"], True)]
    assert jobs.store.get(old.id).deleted_at == 37 * D
    assert tick(jobs, qbit, 40 * D) == []


def test_two_agents_waits_for_both(env):
    jobs, lib, qbit, mk = env
    a1 = mk(agent="a1")
    a2 = mk(agent="a2", closed_at=None)
    assert tick(jobs, qbit, 8 * D) == [] and lib.removed == [] and qbit.deleted == []
    jobs.store.set(a2.id, closed_at=8 * D)
    assert tick(jobs, qbit, 16 * D) == [a1.id]
    assert lib.removed == [(M, ["movie:603"], True)]
    assert jobs.store.get(a1.id).deleted_at == jobs.store.get(a2.id).deleted_at == 16 * D
    assert tick(jobs, qbit, 20 * D) == []


def test_keep_by_one_agent_protects_entry(env):
    jobs, lib, qbit, mk = env
    mk(agent="a1")
    mk(agent="a2", decision="keep")
    assert tick(jobs, qbit, 100 * D) == [] and lib.removed == [] and qbit.deleted == []


def test_keep_on_older_job_of_same_agent_protects_entry(env):
    jobs, lib, qbit, mk = env
    mk(decision="keep")
    mk()
    assert tick(jobs, qbit, 100 * D) == [] and lib.removed == []


def test_failed_job_of_other_agent_does_not_block(env):
    jobs, lib, qbit, mk = env
    a1 = mk(agent="a1")
    a2 = mk(agent="a2", state="failed", closed_at=None)
    assert tick(jobs, qbit, 7 * D) == [a1.id]
    assert lib.removed == [(M, ["movie:603"], True)]
    assert jobs.store.get(a2.id).deleted_at is None


def test_keep_on_other_agent_set_during_tick_is_honoured(env):
    jobs, lib, qbit, mk = env
    mk(agent="a1")
    a2 = mk(agent="a2")

    class FlipQbit(FakeQbit):
        calls = 0

        def seeding_seconds(self, h):
            self.calls += 1
            if self.calls == 3:  # after a2 was judged eligible, before the files go
                jobs.store.set(a2.id, decision="keep")
            return 9 * D
    q = FlipQbit({})
    assert tick(jobs, q, 100 * D) == [] and lib.removed == [] and q.deleted == []


def test_remove_entry_covers_same_item_claimants(env):
    jobs, lib, qbit, mk = env
    a1, a2 = mk(agent="a1"), mk(agent="a2", closed_at=None)
    remove_entry(jobs, qbit, a1, jobs.store.all(), 5)
    assert lib.removed == [(M, ["movie:603"], True)] and qbit.deleted == ["h"]
    assert jobs.store.get(a1.id).deleted_at == jobs.store.get(a2.id).deleted_at == 5


def test_first_agent_requested_again_still_removes(env):
    jobs, lib, qbit, mk = env
    old1, a2, new1 = mk(agent="a1"), mk(agent="a2"), mk(agent="a1")
    assert tick(jobs, qbit, 7 * D) == [a2.id]  # a1's newest comes after a2's: a2 is the first head
    assert lib.removed == [(M, ["movie:603"], True)]
    assert all(jobs.store.get(j.id).deleted_at == 7 * D for j in (old1, a2, new1))


def test_other_season_pending_does_not_block(env):
    jobs, lib, qbit, mk = env
    S1, S2 = Item("season", 5, 1), Item("season", 5, 2)
    s1 = mk(item=S1, units=["episode:5:1:1"])
    mk(item=S2, units=["episode:5:2:1"], agent="a2", state="pending", closed_at=None)
    assert tick(jobs, qbit, 7 * D) == [s1.id]
    assert lib.removed == [(S1, ["episode:5:1:1"], False)]  # S2 still holds the series entry


def test_keep_on_other_season_does_not_keep_this_one(env):
    jobs, lib, qbit, mk = env
    S1, S2 = Item("season", 5, 1), Item("season", 5, 2)
    s1 = mk(item=S1, units=["episode:5:1:1"])
    mk(item=S2, units=["episode:5:2:1"], agent="a2", decision="keep")
    assert tick(jobs, qbit, 7 * D) == [s1.id]


def test_overlapping_episode_job_blocks_season(env):
    jobs, lib, qbit, mk = env
    S, E = Item("season", 5, 1), Item("episode", 5, 1, 2)
    s = mk(item=S, units=["episode:5:1:1", "episode:5:1:2"])
    e = mk(item=E, units=["episode:5:1:2"], protected=["episode:5:1:2"], agent="a2", closed_at=7 * D)
    assert tick(jobs, qbit, 7 * D) == [] and lib.removed == []
    assert tick(jobs, qbit, 14 * D) == [s.id]
    assert lib.removed == [(S, ["episode:5:1:1", "episode:5:1:2"], True)]  # e is covered: nothing else holds the series
    assert jobs.store.get(e.id).deleted_at == 14 * D


def test_fully_protected_other_agent_waits_then_goes(env):
    jobs, lib, qbit, mk = env
    a1 = mk(agent="a1")
    a2 = mk(agent="a2", protected=["movie:603"], closed_at=None)  # asked while a1's download was pending
    assert tick(jobs, qbit, 8 * D) == [] and lib.removed == []
    jobs.store.set(a2.id, closed_at=8 * D)
    assert tick(jobs, qbit, 15 * D) == [a1.id]
    assert lib.removed == [(M, ["movie:603"], True)]
    assert jobs.store.get(a1.id).deleted_at == jobs.store.get(a2.id).deleted_at == 15 * D


def test_torrents_of_all_covered_jobs_deleted(env):
    jobs, lib, qbit, mk = env
    a1 = mk(agent="a1")
    mk(agent="a2", hashes=["h", "h2"])
    qbit.seed["h2"] = 5 * D
    assert tick(jobs, qbit, 7 * D) == [a1.id]
    assert qbit.deleted == ["h", "h2"]


def test_units_caused_by_any_covered_job_are_removed(env):
    jobs, lib, qbit, mk = env
    S, E = Item("season", 1, 1), Item("episode", 1, 1, 2)
    a1 = mk(item=E, units=["episode:1:1:2"], agent="a1")
    a2 = mk(item=S, units=["episode:1:1:1", "episode:1:1:2"], protected=["episode:1:1:2"], agent="a2")
    remove_entry(jobs, qbit, a1, jobs.store.all(), 5)  # one call: a2 needs E02, and its own E01 goes along
    assert lib.removed == [(E, ["episode:1:1:2", "episode:1:1:1"], True)]
    assert jobs.store.get(a1.id).deleted_at == jobs.store.get(a2.id).deleted_at == 5


def test_units_caused_by_any_covered_job_are_removed_by_tick(env):
    jobs, lib, qbit, mk = env
    S, E = Item("season", 1, 1), Item("episode", 1, 1, 2)
    a1 = mk(item=E, units=["episode:1:1:2"], agent="a1")
    a2 = mk(item=S, units=["episode:1:1:1", "episode:1:1:2"], protected=["episode:1:1:2"], agent="a2")
    assert sorted(tick(jobs, qbit, 7 * D)) == [a1.id, a2.id]
    assert sorted(u for _, keys, _ in lib.removed for u in keys) == ["episode:1:1:1", "episode:1:1:2"]
    assert lib.removed[-1][2] is True  # the last one drops the entry
    assert jobs.store.get(a1.id).deleted_at == jobs.store.get(a2.id).deleted_at == 7 * D


def test_unit_protected_by_every_covered_job_is_kept(env):
    jobs, lib, qbit, mk = env
    S = Item("season", 1, 1)
    a1 = mk(item=S, units=["episode:1:1:1", "episode:1:1:2"], protected=["episode:1:1:1"], agent="a1")
    mk(item=S, units=["episode:1:1:1", "episode:1:1:2"], protected=["episode:1:1:1"], agent="a2")
    assert tick(jobs, qbit, 7 * D) == [a1.id]
    assert lib.removed == [(S, ["episode:1:1:2"], False)]
