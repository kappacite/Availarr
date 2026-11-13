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
