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


def _claims(job: Job, o: Job) -> bool:
    """o still needs the files of job's entry. Failed/retried jobs hold no claim, or a refusal would block forever."""
    return o.deleted_at is None and o.state not in ("failed", "retried") and _shares_entry(job, o)


def _follows(o: Job, job: Job) -> bool:  # an older request of the same agent for the same item: the newest decides
    return o.agent == job.agent and o.item == job.item and o.id < job.id


def remove_entry(jobs: Jobs, qbit, job: Job, all_jobs: list[Job], now: float) -> None:
    covered = [o for o in all_jobs if o.deleted_at is None and o.item == job.item
               and (o.id == job.id or _follows(o, job) or _claims(job, o))]
    ids = {o.id for o in covered}
    # a partly protected entry must never be dropped: Sonarr would delete the protected episodes too
    drop = job.added_by_tool and not job.protected_units and not any(
        o.id not in ids and _claims(job, o) for o in all_jobs)
    jobs.lib(job.item).remove(job.item, [u for u in job.units if u not in job.protected_units], drop)
    for h in job.hashes:
        qbit.delete(h)
    for o in covered:  # every request of this item shares these files: gone with them
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
            claimants = [o for o in jobs.store.all() if o.id != job.id and _claims(job, o)]
            # one remover per item: each agent's newest job decides for its older ones (requested again), and
            # among agents the first one removes, its job being the one that added and fetched the files
            group = [job] + [o for o in claimants if o.item == job.item]
            if min(o.id for o in group if not any(_follows(o, p) for p in group)) != job.id:
                continue
            # every other agent still on these files must be done with them too
            if not all(_follows(o, job) or _eligible(jobs.store.get(o.id), qbit, now, cfg.keep_days, cfg.seed_days)
                       for o in claimants):
                continue
            job = jobs.store.get(job.id)
            if not _eligible(job, qbit, now, cfg.keep_days, cfg.seed_days):
                continue
            fresh = jobs.store.all()  # no network call between this read and remove()
            ok = {job.id} | {o.id for o in claimants}
            last = next(o for o in fresh if o.id == job.id)
            if last.decision == "keep" or last.deleted_at is not None or any(
                    o.id not in ok or o.decision == "keep" for o in fresh if _claims(last, o)):
                continue
            remove_entry(jobs, qbit, last, fresh, now)
            done.append(job.id)
        except Exception:
            log.exception("retention: job %s failed", snap.id)
    return done
