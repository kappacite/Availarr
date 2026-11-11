import logging

from availarr.server.db import Job
from availarr.server.jobs import Jobs

log = logging.getLogger(__name__)


def seed_seconds(job: Job, qbit) -> int | None:
    secs = [s for h in job.hashes if (s := qbit.seeding_seconds(h)) is not None]
    return min(secs) if secs else None


def seed_ok(job: Job, qbit, seed_days: float) -> bool:
    return all((s := qbit.seeding_seconds(h)) is None or s >= seed_days * 86400 for h in job.hashes)


def fully_protected(job: Job) -> bool:
    return set(job.units) <= set(job.protected_units)


def _eligible(job: Job, qbit, now: float, keep_days: float, seed_days: float) -> bool:
    if job.deleted_at is not None or job.state != "ready" or job.decision == "keep":
        return False
    if fully_protected(job) or not seed_ok(job, qbit, seed_days):
        return False
    return job.decision == "delete" or (
        job.closed_at is not None and now - job.closed_at >= keep_days * 86400)


def _shares_entry(a: Job, b: Job) -> bool:
    return a.item.ext_id == b.item.ext_id and (a.item.kind == "movie") == (b.item.kind == "movie")


def tick(jobs: Jobs, qbit, now: float) -> list[int]:
    for job in jobs.store.all():
        if job.state == "pending":
            try:
                jobs.refresh(job)
            except Exception:
                log.exception("retention: refreshing job %s failed", job.id)
    all_jobs = jobs.store.all()
    done = []
    for snap in all_jobs:
        try:
            job = jobs.store.get(snap.id)  # the panel may have decided since the snapshot
            if not _eligible(job, qbit, now, jobs.cfg.keep_days, jobs.cfg.seed_days):
                continue
            # a partly protected entry must never be dropped: Sonarr would delete the protected episodes too
            drop = job.added_by_tool and not job.protected_units and not any(
                o.id != job.id and o.deleted_at is None and _shares_entry(job, o) for o in all_jobs)
            keys = [u for u in job.units if u not in job.protected_units]
            job = jobs.store.get(job.id)  # last look right before the irreversible part
            if not _eligible(job, qbit, now, jobs.cfg.keep_days, jobs.cfg.seed_days):
                continue
            jobs.lib(job.item).remove(job.item, keys, drop)
            for h in job.hashes:
                qbit.delete(h)
            jobs.store.set(job.id, deleted_at=now)
            done.append(job.id)
        except Exception:
            log.exception("retention: job %s failed", snap.id)
    return done
