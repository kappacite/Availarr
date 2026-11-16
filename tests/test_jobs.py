import pytest

from availarr.common import Item
from availarr.server.db import JobStore
from availarr.server.jobs import Jobs
from availarr.server.library import Unit
from availarr.server.quality import FileInfo
from tests.fakes import FakeLibrary, make_cfg

GOOD = FileInfo(1080, "Bluray-1080p", ["French", "English"], "/m/Dune.mkv", 9_000_000_000)
LOW = FileInfo(720, "WEBDL-720p", ["English"], "/m/x.mkv", 1)
M = Item("movie", 603)
NONE = Unit("movie:603", False, False, None)
P = "HD-1080p FR"


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def mk(tmp_path, clock):
    def make(lib):
        db = str(tmp_path / f"j{len(list(tmp_path.iterdir()))}.db")
        return Jobs(JobStore(db), lib, lib, make_cfg(), clock)
    return make


def test_lookup_existing_satisfying_file_returns_real_release(mk):
    assert mk(FakeLibrary([Unit("movie:603", True, True, GOOD)])).lookup(M, P) == {
        "title": "Dune.2024.MULTi.1080p.BluRay-Availarr", "size": 9_000_000_000}


def test_lookup_missing_returns_synthetic(mk):
    assert mk(FakeLibrary([NONE])).lookup(M, P) == {
        "title": "Dune.2024.MULTi.1080p.WEB-DL-Availarr", "size": 8 * 10**9}


def test_lookup_series_synthetic_size(mk):
    units = [Unit(f"episode:1:1:{n}", False, False, None) for n in (1, 2, 3)]
    r = mk(FakeLibrary(units)).lookup(Item("season", 1, 1), P)
    assert r["size"] == 6 * 10**9
    # series absent on A: one placeholder unit, B's episode count sizes the season pack
    assert mk(FakeLibrary([Unit("season:1:1", False, False, None)])).lookup(Item("season", 1, 1), P, 10)["size"] == 20 * 10**9


def test_lookup_empty_units_returns_none(mk):
    assert mk(FakeLibrary([])).lookup(M, P) is None


def test_lookup_unmapped_profile_returns_none_and_records(mk):
    j = mk(FakeLibrary([NONE]))
    assert j.lookup(M, "Ultra") is None and "Ultra" in j.unmapped


def test_lookup_blocked_after_failure(mk, clock):
    j = mk(FakeLibrary([NONE]))
    job = j.request(M, P, "T")
    clock.t = 7 * 86400
    job = j.refresh(job)
    assert job.state == "failed" and job.error == "Introuvable chez l'envoyeur après 7 j"
    assert j.lookup(M, P) is None
    job.state = "retried"
    j.store.update(job)
    assert j.lookup(M, P) is not None


def test_request_protects_existing_file_or_monitored(mk):
    units = [Unit("episode:1:1:1", True, False, GOOD), Unit("episode:1:1:2", True, True, None),
             Unit("episode:1:1:3", True, False, None)]
    job = mk(FakeLibrary(units)).request(Item("season", 1, 1), P, "T")
    assert job.protected_units == ["episode:1:1:1", "episode:1:1:2"]


def test_request_upgrades_insufficient_file(mk):
    lib = FakeLibrary([Unit("movie:603", True, True, LOW)], profile="HD-720p")
    mk(lib).request(M, P, "T")
    assert lib.ensure_calls == [(M, "1080p MULTi", True)]
    lib2 = FakeLibrary([Unit("movie:603", True, True, LOW)], profile="1080p MULTi")
    mk(lib2).request(M, P, "T")
    assert lib2.ensure_calls == [(M, "1080p MULTi", False)]


def test_request_added_flag(mk):
    job = mk(FakeLibrary([NONE])).request(M, P, "T")
    assert job.added_by_tool and job.state == "pending"


def test_request_satisfied_is_ready_immediately(mk):
    lib = FakeLibrary([Unit("movie:603", True, True, GOOD)])
    job = mk(lib).request(M, P, "T")
    assert job.state == "ready" and lib.ensure_calls == [] and job.protected_units == ["movie:603"]
    assert not job.added_by_tool


def test_request_again_is_a_new_entry_with_the_same_state(mk):
    j = mk(FakeLibrary([NONE]))
    a = j.request(M, P, "T")
    j.store.set(a.id, state="ready", hashes=["h"], decision="keep")
    b = j.request(M, P, "T")
    assert a.id != b.id and len(j.lib(M).ensure_calls) == 1
    assert (b.state, b.hashes, b.decision, b.added_by_tool) == ("ready", ["h"], "keep", a.added_by_tool)


def test_refresh_ready_records_hashes_and_units(mk, clock):
    lib = FakeLibrary([NONE])
    j = mk(lib)
    job = j.request(M, P, "T")
    assert job.state == "pending"
    clock.t = 50
    lib.units, lib.hashes = [Unit("movie:603", True, True, GOOD)], ["abc"]
    job = j.refresh(job)
    assert (job.state, job.hashes, job.ready_at) == ("ready", ["abc"], 50)
    assert lib.calls["hashes"] == [(M, job.created_at, job.protected_units)]


def test_request_unmapped_profile_raises(mk):
    with pytest.raises(ValueError, match="unmapped profile: Ultra"):
        mk(FakeLibrary([NONE])).request(M, "Ultra", "T")


def test_refresh_unmapped_profile_fails_job(mk):
    j = mk(FakeLibrary([NONE]))
    job = j.request(M, P, "T")
    j.cfg.profiles.clear()
    job = j.refresh(job)
    assert job.state == "failed" and job.error == "Profil du receveur sans correspondance : HD-1080p FR"


def test_status_and_file_path(mk):
    j = mk(FakeLibrary([Unit("movie:603", True, True, GOOD)]))
    job = j.request(M, P, "T")
    assert j.status(job) == {"state": "ready", "progress": 1.0, "eta": 0, "rate": None, "error": None,
                             "files": [{"index": 0, "name": "Dune.mkv", "size": 9_000_000_000}]}
    assert j.file_path(job, 0) == ("/m/Dune.mkv", 9_000_000_000)
    with pytest.raises(IndexError):
        j.file_path(job, 1)


def test_status_pending_progress_and_close(mk, clock):
    j = mk(FakeLibrary([NONE], progress=0.4))
    job = j.request(M, P, "T")
    assert j.status(job)["progress"] == 0.4 and j.status(job)["files"] == []
    clock.t = 9
    j.close(job, "cancelled")
    got = j.store.get(job.id)
    assert (got.outcome, got.closed_at) == ("cancelled", 9)


def test_new_entry_starts_open_and_undecided(mk, clock):
    j = mk(FakeLibrary([NONE]))
    a = j.request(M, P, "T")
    clock.t = 9
    j.close(a, "cancelled")
    j.store.set(a.id, decision="delete")
    b = j.request(M, P, "T")
    assert b.id != a.id and (b.closed_at, b.outcome, b.decision) == (None, None, "none")


def test_deleted_job_reports_failed_and_no_files(mk):
    j = mk(FakeLibrary([Unit("movie:603", True, True, GOOD)]))
    job = j.request(M, P, "T")
    j.store.set(job.id, deleted_at=5.0)
    job = j.store.get(job.id)
    assert j.status(job) == {"state": "failed", "progress": 0.0, "error": "Supprimé chez l'envoyeur", "files": []}
    with pytest.raises(IndexError):
        j.file_path(job, 0)


def test_reuse_keeps_keep_decision(mk, clock):
    from availarr.server.retention import tick
    from tests.fakes import FakeQbit
    lib = FakeLibrary([NONE])
    j = mk(lib)
    a = j.request(M, P, "T")
    j.store.set(a.id, decision="keep")
    j.request(M, P, "T")
    assert j.store.get(a.id).decision == "keep"
    lib.units = [Unit("movie:603", True, True, GOOD)]
    j.refresh(j.store.get(a.id))
    j.close(j.store.get(a.id), "delivered")
    q = FakeQbit({})
    assert tick(j, q, 10**9) == [] and lib.removed == [] and j.store.get(a.id).deleted_at is None


def test_status_eta_from_qbittorrent(mk):
    class Lib(FakeLibrary):
        def __init__(self, units):
            super().__init__(units)
            del self.progress  # FakeLibrary stores a plain value; use the method below

        def progress(self, item, live=None):
            return live(["a", "b"])["a"] if live else 0.0

    class Q:
        def __init__(self, etas):
            self.etas = etas

        def live(self, hashes):
            return {h: (0.25, self.etas[h]) for h in hashes}

    j = mk(Lib([NONE]))
    job = j.request(M, P, "T")
    j.qbit = Q({"a": 60, "b": 300})
    assert j.status(job)["progress"] == 0.25 and j.status(job)["eta"] == 300
    j.qbit = Q({"a": 60, "b": None})
    assert j.status(job)["eta"] is None


def test_request_per_agent_creates_two_jobs(mk):
    jobs = mk(FakeLibrary([NONE]))
    a = jobs.request(M, P, "t", agent="a1")
    b = jobs.request(M, P, "t", agent="a2")
    assert a.id != b.id and (a.agent, b.agent) == ("a1", "a2")
    assert len(jobs.lib(M).ensure_calls) == 2


def test_failure_blocks_only_that_agent(mk):
    jobs = mk(FakeLibrary([NONE]))
    j = jobs.request(M, P, "t", agent="a1")
    jobs.store.set(j.id, state="failed")
    assert jobs.lookup(M, P, agent="a1") is None
    assert jobs.lookup(M, P, agent="a2")


S1 = Item("season", 1, 4)
EPS = ["episode:1:4:1", "episode:1:4:2"]


def test_rerequest_after_failure_is_not_protected(mk):
    # first request: A doesn't have the season; Availarr makes Sonarr monitor it, then the request fails
    lib = FakeLibrary([Unit(k, True, False, None) for k in EPS])
    j = mk(lib)
    first = j.request(S1, P, "T")
    assert first.protected_units == []
    j.store.set(first.id, state="failed")
    lib.units = [Unit(k, True, True, None) for k in EPS]  # monitored by Availarr, not by the user
    again = j.request(S1, P, "T")
    assert again.id != first.id and again.protected_units == []


def test_rerequest_keeps_protection_of_what_was_already_there(mk):
    lib = FakeLibrary([Unit(EPS[0], True, True, None), Unit(EPS[1], True, False, None)])
    j = mk(lib)
    first = j.request(S1, P, "T")
    assert first.protected_units == [EPS[0]]
    j.store.set(first.id, state="failed")
    lib.units = [Unit(k, True, True, None) for k in EPS]
    assert j.request(S1, P, "T").protected_units == [EPS[0]]


def test_deleted_earlier_job_does_not_count(mk):
    lib = FakeLibrary([Unit(k, True, False, None) for k in EPS])
    j = mk(lib)
    first = j.request(S1, P, "T")
    j.store.set(first.id, state="failed", deleted_at=1.0)
    lib.units = [Unit(k, True, True, None) for k in EPS]  # the user monitors it themselves since
    assert j.request(S1, P, "T").protected_units == EPS


def test_repair_protection_fixes_old_jobs_and_their_torrents(mk):
    lib = FakeLibrary([Unit(k, True, True, GOOD) for k in EPS], hashes=["h"])
    j = mk(lib)
    first = j.store.create(S1, P, "T", EPS, [], True, 0)
    j.store.set(first.id, state="failed")
    wrong = j.store.create(S1, P, "T", EPS, EPS, False, 1)  # computed by the old code: wrongly protected
    j.store.set(wrong.id, state="ready", ready_at=2.0)
    j.repair_protection()
    fixed = j.store.get(wrong.id)
    assert fixed.protected_units == [] and fixed.hashes == ["h"]
    j.repair_protection()  # idempotent
    assert j.store.get(wrong.id).protected_units == []


def test_repair_protection_retries_when_arr_is_down(mk):
    lib = FakeLibrary([Unit(k, True, True, GOOD) for k in EPS])
    j = mk(lib)
    first = j.store.create(S1, P, "T", EPS, [], True, 0)
    j.store.set(first.id, state="failed")
    wrong = j.store.create(S1, P, "T", EPS, EPS, False, 1)
    j.store.set(wrong.id, state="ready", ready_at=2.0)

    class Down:
        def hashes(self, *a):
            raise OSError("sonarr down")
    j.lib = lambda item: Down()
    j.repair_protection()
    assert j.store.get(wrong.id).protected_units == EPS  # unchanged: retried next tick


def test_rerequest_after_failed_season_placeholder(mk):
    # Availarr created the series: the first job failed before ready, keeping the placeholder key season:1:4
    lib = FakeLibrary([Unit(k, True, True, None) for k in EPS])
    j = mk(lib)
    first = j.store.create(S1, P, "T", ["season:1:4"], [], True, 0)
    j.store.set(first.id, state="failed")
    assert j.request(S1, P, "T").protected_units == []


def test_hashes_include_torrent_grabbed_for_the_earlier_request(mk, clock):
    # the first request's grab was imported before the second request existed: still this request's torrent
    lib = FakeLibrary([Unit(k, True, True, None) for k in EPS])
    j = mk(lib)
    first = j.store.create(S1, P, "T", EPS, [], True, 5)
    j.store.set(first.id, state="failed")
    clock.t = 100
    again = j.request(S1, P, "T")
    lib.units = [Unit(k, True, True, GOOD) for k in EPS]
    j.refresh(again)
    assert lib.calls["hashes"][-1][1] == 5  # since the earlier request, not 100


def test_repair_keeps_protection_that_was_already_there(mk):
    lib = FakeLibrary([Unit(k, True, True, GOOD) for k in EPS], hashes=["h"])
    j = mk(lib)
    first = j.store.create(S1, P, "T", EPS, [EPS[0]], True, 0)  # E1 was A's own before any request
    j.store.set(first.id, state="failed")
    later = j.store.create(S1, P, "T", EPS, EPS, False, 1)
    j.store.set(later.id, state="ready", ready_at=2.0)
    j.repair_protection()
    assert j.store.get(later.id).protected_units == [EPS[0]]


def test_refresh_keeps_a_manually_linked_torrent(mk):
    lib = FakeLibrary([NONE], hashes=["found"])
    j = mk(lib)
    job = j.request(M, P, "T")
    j.store.set(job.id, hashes=["linked"])
    lib.units = [Unit("movie:603", True, True, GOOD)]
    assert j.refresh(j.store.get(job.id)).hashes == ["linked", "found"]


def test_linking_a_torrent_to_a_waiting_request_serves_its_files(mk, tmp_path):
    from availarr.server.qbit import Torrent
    from tests.fakes import FakeQbit
    j = mk(FakeLibrary([NONE]))
    j.qbit = q = FakeQbit({})
    f = tmp_path / "Dune.mkv"
    f.write_bytes(b"x" * 5)
    h = "a" * 40
    q.torrents[h] = Torrent(h, "Dune", "downloading", 0.5, None, 0, 0, 0.0, 0)
    q.content = {h: [(str(f), 5)]}
    job = j.request(M, P, "T")
    j.link(job, [h])
    assert j.store.get(job.id).state == "pending"  # not sent half downloaded
    q.torrents[h].progress = 1.0
    job = j.refresh(j.store.get(job.id))
    assert job.state == "ready" and job.ready_at is not None
    assert j.status(job)["files"] == [{"index": 0, "name": "Dune.mkv", "size": 5}]
    assert j.file_path(job, 0) == (str(f), 5)
