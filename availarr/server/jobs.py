import os
import time

from availarr.common import Item, release_title as make_title
from availarr.server.config import ServerConfig
from availarr.server.db import Job, JobStore
from availarr.server.library import Library
from availarr.server.quality import Requirement, file_tags, satisfies


class Jobs:
    def __init__(self, store: JobStore, radarr: Library, sonarr: Library, cfg: ServerConfig, clock=time.time):
        self.store, self.radarr, self.sonarr, self.cfg, self.clock = store, radarr, sonarr, cfg, clock
        self.unmapped: set[str] = set()
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
        if req is None or self.store.failed_for(item):
            return None
        lib = self.lib(item)
        units = lib.units(item)
        if not units:
            return None
        name, year = lib.title(item)
        if all(satisfies(u.file, req) for u in units):
            return {"title": make_title(name, year, item, file_tags(units[0].file)),
                    "size": sum(u.file.size for u in units)}
        gb = self.cfg.movie_gb if item.kind == "movie" else self.cfg.episode_gb * len(units)
        return {"title": make_title(name, year, item, req.announce), "size": int(gb * 10**9)}

    def request(self, item: Item, b_profile: str, release_title: str) -> Job:
        if job := self.store.active_for(item):
            return job
        req = self.requirement(b_profile)
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
        units = lib.units(job.item)
        if units and all(satisfies(u.file, req) for u in units):
            job.state, job.ready_at = "ready", now
            job.units = [u.key for u in units]
            job.hashes = lib.hashes(job.item, job.created_at)
        elif now - job.created_at >= self.cfg.fail_days * 86400:
            job.state, job.error = "failed", f"Introuvable chez A après {self.cfg.fail_days:g} j"
        else:
            return job
        self.store.update(job)
        return job

    def _files(self, job: Job):
        if job.state != "ready":
            return []
        return [u.file for u in self.lib(job.item).units(job.item) if u.file]

    def status(self, job: Job) -> dict:
        progress = 1.0 if job.state == "ready" else self.lib(job.item).progress(job.item) if job.state == "pending" else 0.0
        files = [{"index": i, "name": os.path.basename(f.path), "size": f.size} for i, f in enumerate(self._files(job))]
        return {"state": job.state, "progress": progress, "error": job.error, "files": files}

    def file_path(self, job: Job, index: int) -> tuple[str, int]:
        files = self._files(job)
        if not 0 <= index < len(files):
            raise IndexError(index)
        return files[index].path, files[index].size

    def close(self, job: Job, outcome: str) -> None:
        job.outcome, job.closed_at = outcome, self.clock()
        self.store.update(job)
