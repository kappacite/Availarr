import logging
import os
import threading
import time

from availarr.common import Item, release_title as make_title
from availarr.server.config import ServerConfig
from availarr.server.db import Job, JobStore
from availarr.server.library import Library
from availarr.server.quality import Requirement, file_tags, satisfies

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
        self._lock = threading.Lock()  # ponytail: process-wide lock, fine for one agent
        self.transfer: dict[int, tuple[int, int]] = {}  # job id -> (bytes served, total); filled by the API

    def lib(self, item: Item) -> Library:
        return self.radarr if item.kind == "movie" else self.sonarr

    def requirement(self, b_profile: str) -> Requirement | None:
        req = self.cfg.profiles.get(b_profile) or self.cfg.profiles.get("default")
        if b_profile not in self.cfg.profiles:
            self.unmapped.add(b_profile)
        return req

    def lookup(self, item: Item, b_profile: str) -> dict | None:
        req = self.requirement(b_profile)
        if req is None:
            log.warning("lookup %s: B profile %r has no [profiles] entry and no 'default'", item.key(), b_profile)
            return None
        if self.store.failed_for(item):
            log.info("lookup %s: blocked after a failure (panel: Réessayer)", item.key())
            return None
        lib = self.lib(item)
        units = lib.units(item)
        if not units:
            log.info("lookup %s: nothing to offer (no matching units)", item.key())
            return None
        name, year = lib.title(item)
        if all(satisfies(u.file, req) for u in units):
            return {"title": make_title(name, year, item, file_tags(units[0].file)),
                    "size": sum(u.file.size for u in units)}
        gb = self.cfg.movie_gb if item.kind == "movie" else self.cfg.episode_gb * len(units)
        return {"title": make_title(name, year, item, req.announce), "size": int(gb * 10**9)}

    def request(self, item: Item, b_profile: str, release_title: str) -> Job:
        with self._lock:
            return self._request(item, b_profile, release_title)

    def _request(self, item: Item, b_profile: str, release_title: str) -> Job:
        if job := self.store.active_for(item):
            # B grabs it again: a fresh transfer, so the old close/"delete" must not delete it mid-way; "keep" stays
            self.store.set(job.id, closed_at=None, outcome=None)
            if job.decision == "delete":
                self.store.set(job.id, decision="none")
            return self.store.get(job.id)
        req = self.requirement(b_profile)
        if req is None:
            raise ValueError(f"unmapped profile: {b_profile}")
        lib = self.lib(item)
        units = lib.units(item)
        protected = [u.key for u in units if u.file or u.monitored]  # before ensure() touches anything
        added = False
        if not all(satisfies(u.file, req) for u in units):
            current = lib.profile_name(item)
            change = current is not None and current not in req.a_profiles
            added = lib.ensure(item, req.a_profiles[0], change)
        job = self.store.create(item, b_profile, release_title, [u.key for u in units], protected,
                                added, self.clock())
        return self.refresh(job)

    def refresh(self, job: Job) -> Job:
        if job.state != "pending":
            return job
        lib, now = self.lib(job.item), self.clock()
        req = self.requirement(job.b_profile)
        if req is None:
            job.state, job.error = "failed", f"Profil de B sans correspondance : {job.b_profile}"
            self.store.update(job)
            return job
        units = lib.units(job.item)
        if units and all(satisfies(u.file, req) for u in units):
            job.state, job.ready_at = "ready", now
            job.units = [u.key for u in units]
            job.hashes = lib.hashes(job.item, job.created_at, job.protected_units)
        elif now - job.created_at >= self.cfg.fail_days * 86400:
            job.state, job.error = "failed", f"Introuvable chez A après {self.cfg.fail_days:g} j"
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
            return {"state": "failed", "progress": 0.0, "error": "Supprimé chez A", "files": []}
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

    def close(self, job: Job, outcome: str) -> None:
        job.outcome, job.closed_at = outcome, self.clock()
        self.store.set(job.id, outcome=job.outcome, closed_at=job.closed_at)
