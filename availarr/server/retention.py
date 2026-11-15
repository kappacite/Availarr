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


def _done(job: Job, qbit, now: float, keep_days: float, seed_days: float) -> bool:
    """Through with its files: delivered long enough ago (or marked for deletion) and seeded."""
    if job.deleted_at is not None or job.state != "ready" or job.decision == "keep":
        return False
    if not seed_ok(job, qbit, seed_days):
        return False
    return job.decision == "delete" or (
        job.closed_at is not None and now - job.closed_at >= keep_days * 86400)


def _eligible(job: Job, qbit, now: float, keep_days: float, seed_days: float) -> bool:
    return not fully_protected(job) and _done(job, qbit, now, keep_days, seed_days)


def _shares_entry(a: Job, b: Job) -> bool:
    return a.item.ext_id == b.item.ext_id and (a.item.kind == "movie") == (b.item.kind == "movie")


def _keys(job: Job) -> list[str]:
    return [u for u in job.units if u not in job.protected_units]


def _live(job: Job, o: Job) -> bool:
    """o still holds job's entry. Failed/retried jobs hold nothing, or a refusal would block forever."""
    return o.deleted_at is None and o.state not in ("failed", "retried") and _shares_entry(job, o)


def _follows(o: Job, job: Job) -> bool:  # an older request of the same agent for the same item: the newest decides
    return o.agent == job.agent and o.item == job.item and o.id < job.id


def _covered(job: Job, all_jobs: list[Job]) -> tuple[list[Job], list[str]]:
    """job, its older requests and, transitively, every job needing a file one of them would delete;
    with the units they delete together (a unit goes if one of them caused it)."""
    covered = {job.id: job} | {o.id: o for o in all_jobs if o.deleted_at is None and _follows(o, job)}
    while True:
        keys = list(dict.fromkeys(u for o in covered.values() for u in _keys(o)))
        more = {o.id: o for o in all_jobs
                if o.id not in covered and _live(job, o) and not set(o.units).isdisjoint(keys)}
        if not more:
            return list(covered.values()), keys
        covered |= more


def remove_entry(jobs: Jobs, qbit, job: Job, all_jobs: list[Job], now: float) -> None:
    covered, keys = _covered(job, all_jobs)
    ids = {o.id for o in covered}
    # a partly protected entry must never be dropped: Sonarr would delete the protected episodes too
    drop = job.added_by_tool and {u for o in covered for u in o.units} <= set(keys) and not any(
        o.id not in ids and _live(job, o) for o in all_jobs)
    jobs.lib(job.item).remove(job.item, keys, drop)
    for h in dict.fromkeys(h for o in covered for h in o.hashes):
        qbit.delete(h)
    for o in covered:  # they all shared these files: gone with them
        jobs.store.set(o.id, deleted_at=now)


def tick(jobs: Jobs, qbit, now: float) -> list[int]:
    for job in jobs.store.all():
        if job.state == "pending":
            try:
                jobs.refresh(job)
            except Exception:
                log.exception("retention: refreshing job %s failed", job.id)
    all_jobs = jobs.store.all()
    cfg = jobs.cfg
    done = []
    for snap in all_jobs:
        try:
            job = jobs.store.get(snap.id)  # the panel may have decided since the snapshot
            if not _eligible(job, qbit, now, cfg.keep_days, cfg.seed_days):
                continue
            # read again: a job removed earlier in this tick no longer claims anything
            claimants = [o for o in _covered(job, jobs.store.all())[0] if o.id != job.id]
            # one remover per item: each agent's newest job decides for its older ones (requested again), and
            # among agents the first one removes, its job being the one that added and fetched the files
            # (a fully protected job never removes: it didn't fetch anything)
            group = [job] + [o for o in claimants if o.item == job.item]
            if min(o.id for o in group if not fully_protected(o) and not any(_follows(o, p) for p in group)) != job.id:
                continue
            # every other job still on these files must be done with them too, even one that found them already here
            if not all(_follows(o, job) or _done(jobs.store.get(o.id), qbit, now, cfg.keep_days, cfg.seed_days)
                       for o in claimants):
                continue
            job = jobs.store.get(job.id)
            if not _eligible(job, qbit, now, cfg.keep_days, cfg.seed_days):
                continue
            fresh = jobs.store.all()  # no network call between this read and remove()
            ok = {job.id} | {o.id for o in claimants}
            last = next(o for o in fresh if o.id == job.id)
            if last.deleted_at is not None or any(
                    o.id not in ok or o.decision == "keep" for o in _covered(last, fresh)[0]):
                continue
            remove_entry(jobs, qbit, last, fresh, now)
            done.append(job.id)
        except Exception:
            log.exception("retention: job %s failed", snap.id)
    return done
