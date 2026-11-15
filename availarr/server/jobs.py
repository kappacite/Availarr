import logging
import os
import threading
import time

from availarr.common import Item, release_title as make_title
from availarr.server.config import ServerConfig
from availarr.server.db import Job, JobStore
from availarr.server.library import Library
from availarr.server.quality import Requirement, file_tags, original_title, satisfies

log = logging.getLogger(__name__)


def _disk_size(f) -> int:
    """The size the API will actually serve; the arr's recorded size can lag behind the file on disk."""
    try:
        return os.stat(f.path).st_size
    except OSError:
        return f.size


class Jobs:
    def __init__(self, store: JobStore, radarr: Library, sonarr: Library, cfg: ServerConfig, clock=time.time,
                 qbit=None):
        self.store, self.radarr, self.sonarr, self.cfg, self.clock, self.qbit = store, radarr, sonarr, cfg, clock, qbit
        self.unmapped: set[str] = set()
        self._lock = threading.Lock()  # ponytail: process-wide lock, fine for a handful of agents
        self.transfer: dict[int, tuple[int, int]] = {}  # job id -> (bytes served, total); filled by the API
        self.served: dict[int, dict[int, int]] = {}  # job id -> file index -> bytes served

    def lib(self, item: Item) -> Library:
        return self.radarr if item.kind == "movie" else self.sonarr

    def requirement(self, b_profile: str) -> Requirement | None:
        req = self.cfg.profiles.get(b_profile) or self.cfg.profiles.get("default")
        if b_profile not in self.cfg.profiles:
            self.unmapped.add(b_profile)
        return req

    def lookup(self, item: Item, b_profile: str, episodes: int = 0, agent: str = "legacy") -> dict | None:
        req = self.requirement(b_profile)
        if req is None:
            log.warning("lookup %s: B profile %r has no [profiles] entry and no 'default'", item.key(), b_profile)
            return None
        if self.store.failed_for(item, agent):
            log.info("lookup %s: blocked after a failure (panel: Réessayer)", item.key())
            return None
        lib = self.lib(item)
        units = lib.units(item)
        if not units:
            log.info("lookup %s: nothing to offer (no matching units)", item.key())
            return None
        name, year = lib.title(item)
        if all(satisfies(u.file, req) for u in units):
            own = original_title(units[0].file, req) if len(units) == 1 else None  # a season pack has many names
            return {"title": own or make_title(name, year, item, file_tags(units[0].file)),
                    "size": sum(u.file.size for u in units)}
        gb = self.cfg.movie_gb if item.kind == "movie" else self.cfg.episode_gb * max(len(units), episodes)
        return {"title": make_title(name, year, item, req.announce), "size": int(gb * 10**9)}

    def request(self, item: Item, b_profile: str, release_title: str, agent: str = "legacy") -> Job:
        with self._lock:
            return self._request(item, b_profile, release_title, agent)

    def _request(self, item: Item, b_profile: str, release_title: str, agent: str) -> Job:
        if prev := self.store.active_for(item, agent):
            # requested again: a new entry that takes over the old one's state (protection, torrents, progress).
            # Retention only acts on the newest job of an item, so the old one can't delete files mid-transfer.
            job = self.store.create(item, b_profile, release_title, prev.units, prev.protected_units,
                                    prev.added_by_tool, self.clock(), agent)
            self.store.set(job.id, state=prev.state, hashes=prev.hashes, ready_at=prev.ready_at,
                           decision="keep" if prev.decision == "keep" else "none")
            return self.refresh(self.store.get(job.id))
        req = self.requirement(b_profile)
        if req is None:
            raise ValueError(f"unmapped profile: {b_profile}")
        lib = self.lib(item)
        units = lib.units(item)
        # A's own = there (file or monitored) before Availarr touched it: not what an earlier request had monitored
        caused = self._caused(item, None)
        protected = [u.key for u in units if (u.file or u.monitored) and not self._hit(u.key, caused)]
        added = False
        if not all(satisfies(u.file, req) for u in units):
            current = lib.profile_name(item)
            change = current is not None and current not in req.a_profiles
            added = lib.ensure(item, req.a_profiles[0], change)
        job = self.store.create(item, b_profile, release_title, [u.key for u in units], protected,
                                added, self.clock(), agent)
        return self.refresh(job)

    @staticmethod
    def _hit(unit: str, keys: set[str]) -> bool:
        """unit is in keys, or under a season placeholder (season:x:s covers episode:x:s:*) of a job never ready."""
        return unit in keys or any(k.startswith("season:") and unit.startswith("episode:" + k[7:] + ":") for k in keys)

    def _earlier(self, item: Item, before: int | None, jobs: list[Job] | None = None) -> list[Job]:
        # ponytail: an earlier job that failed and was never cleaned keeps counting; if the user deleted and
        # re-added that entry themselves, it is no longer protected (Garder covers it)
        return [o for o in (jobs if jobs is not None else self.store.all())
                if o.deleted_at is None and (before is None or o.id < before) and o.item.ext_id == item.ext_id
                and (o.item.kind == "movie") == (item.kind == "movie")]

    def _caused(self, item: Item, before: int | None, jobs: list[Job] | None = None) -> set[str]:
        """Units an earlier, not deleted request on the same movie/series made A fetch or monitor."""
        out: set[str] = set()
        for o in self._earlier(item, before, jobs):
            out |= set(o.units) - set(o.protected_units)
        return out

    def _since(self, job: Job, jobs: list[Job] | None = None) -> float:
        """Torrents count from the first request that made A fetch these units, not only from this one."""
        return min([job.created_at] + [o.created_at for o in self._earlier(job.item, job.id, jobs)
                                       if any(self._hit(u, set(o.units) - set(o.protected_units)) for u in job.units)])

    def repair_protection(self) -> None:
        """Jobs saved before the fix above may count an earlier request's episodes as A's own: never deleted,
        torrents ignored. Drop that protection and pick their torrents up again; retried on each tick."""
        all_jobs = sorted(self.store.all(), key=lambda j: j.id)
        for job in all_jobs:
            if job.deleted_at is not None or not job.protected_units:
                continue
            caused = self._caused(job.item, job.id, all_jobs)
            keep = [u for u in job.protected_units if not self._hit(u, caused)]
            if keep == job.protected_units:
                continue
            try:
                hashes = (list(dict.fromkeys(job.hashes + self.lib(job.item).hashes(job.item, self._since(job, all_jobs), keep)))
                          if job.state == "ready" else job.hashes)
            except Exception as e:  # noqa: BLE001 - arr down: try again next tick
                log.warning("job %s: protection not repaired yet: %s", job.id, e)
                continue
            self.store.set(job.id, protected_units=keep, hashes=hashes)
            job.protected_units, job.hashes = keep, hashes  # later jobs judge against the repaired value
            log.info("job %s: protection repaired, %d unit(s) no longer counted as already there",
                     job.id, len(caused & set(job.units)))

    def refresh(self, job: Job) -> Job:
        if job.state != "pending":
            return job
        lib, now = self.lib(job.item), self.clock()
        req = self.requirement(job.b_profile)
        if req is None:
            job.state, job.error = "failed", f"Profil du receveur sans correspondance : {job.b_profile}"
            self.store.update(job)
            return job
        units = lib.units(job.item)
        if units and all(satisfies(u.file, req) for u in units):
            job.state, job.ready_at = "ready", now
            job.units = [u.key for u in units]
            job.hashes = list(dict.fromkeys(job.hashes + lib.hashes(job.item, self._since(job), job.protected_units)))  # keep linked ones
        elif now - job.created_at >= self.cfg.fail_days * 86400:
            job.state, job.error = "failed", f"Introuvable chez l'envoyeur après {self.cfg.fail_days:g} j"
        else:
            return job
        self.store.update(job)
        return job

    def _files(self, job: Job):
        if job.state != "ready" or job.deleted_at is not None:
            return []
        return [u.file for u in self.lib(job.item).units(job.item) if u.file]

    def status(self, job: Job) -> dict:
        if job.deleted_at is not None:
            return {"state": "failed", "progress": 0.0, "error": "Supprimé chez l'envoyeur", "files": []}
        progress, eta = 0.0, None
        if job.state == "ready":
            progress, eta = 1.0, 0
        elif job.state == "pending" and self.qbit:
            etas: dict[str, int | None] = {}

            def live(hashes):  # the library maps the item to its torrents; we keep their ETAs on the way
                now = self.qbit.live(hashes)
                etas.update({h: e for h, (_, e) in now.items()})
                return {h: p for h, (p, _) in now.items()}
            progress = self.lib(job.item).progress(job.item, live)
            eta = max(etas.values()) if etas and None not in etas.values() else None
        elif job.state == "pending":
            progress = self.lib(job.item).progress(job.item)
        files = [{"index": i, "name": os.path.basename(f.path), "size": _disk_size(f)}
                 for i, f in enumerate(self._files(job))]
        return {"state": job.state, "progress": progress, "eta": eta, "rate": self.store.avg_rate(),
                "error": job.error, "files": files}

    def file_path(self, job: Job, index: int) -> tuple[str, int]:
        files = self._files(job)
        if not 0 <= index < len(files):
            raise IndexError(index)
        return files[index].path, files[index].size

    def refuse(self, job: Job, qbit) -> None:
        """The sender refuses a request: it fails on the receiver's side; files nobody else needs are cleaned."""
        from availarr.server.retention import remove_entry  # retention imports Jobs
        if job.outcome == "delivered" or job.deleted_at is not None or job.state in ("failed", "retried"):
            raise ValueError("demande déjà livrée ou supprimée")
        self.store.set(job.id, state="failed", error="Refusée par l'envoyeur")
        try:  # remove_entry guards overlap and keep itself; tick retries a failed cleanup
            remove_entry(self, qbit, self.store.get(job.id), self.store.all(), self.clock())
        except Exception:
            log.warning("refuse: cleaning job %s failed, retention will retry", job.id, exc_info=True)

    def hide(self, job: Job) -> None:
        if job.deleted_at is None and job.outcome is None and job.state in ("pending", "ready"):
            raise ValueError("demande encore active")
        self.store.set(job.id, hidden_at=self.clock())

    def close(self, job: Job, outcome: str) -> None:
        job.outcome, job.closed_at = outcome, self.clock()
        self.store.set(job.id, outcome=job.outcome, closed_at=job.closed_at)
