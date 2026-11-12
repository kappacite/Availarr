"""Figures for the panel's Statistiques tab, computed from the jobs and the recorded transfers."""
import time
from collections import Counter
from statistics import mean, median

from availarr.server.retention import fully_protected

DAYS = 30


def compute(jobs: list, transfers: list[dict], now: float) -> dict:
    by_id = {j.id: j for j in jobs}
    delivered = [j for j in jobs if j.outcome == "delivered"]
    downloaded = [j for j in jobs if not fully_protected(j) and j.ready_at]  # fetched by A's torrents for B
    timed = [t for t in transfers if t["seconds"] >= 1 and t["bytes"] > 0]
    rates = [t["bytes"] / t["seconds"] for t in timed]
    sent, secs = sum(t["bytes"] for t in transfers), sum(t["seconds"] for t in timed)

    day0 = _midnight(now) - (DAYS - 1) * 86400
    per_day = Counter()
    for t in transfers:
        if t["started_at"] >= day0:
            per_day[int((t["started_at"] - day0) // 86400)] += t["bytes"]

    deleted = [j for j in jobs if j.deleted_at is not None]
    return {
        "delivered": len(delivered),
        "kinds": Counter(j.item.kind for j in delivered),
        "requests": len(jobs),
        "downloaded": len(downloaded),
        "from_library": sum(fully_protected(j) for j in jobs),
        "failed": sum(j.state == "failed" for j in jobs),
        "dl_mean": mean(j.ready_at - j.created_at for j in downloaded) if downloaded else None,
        "lead_median": median(j.closed_at - j.created_at for j in delivered if j.closed_at) if delivered else None,
        "sent": sent,
        "transfers": len(transfers),
        "interrupted": sum(not t["complete"] for t in transfers),
        "rate_mean": sent / secs if secs else None,
        "rate_best": max(rates) if rates else None,
        "rate_last": rates[0] if rates else None,
        "kept": sum(j.decision == "keep" for j in jobs),
        "deleted": len(deleted),
        "freed": sum(t["bytes"] for t in transfers if (j := by_id.get(t["job_id"])) and j.deleted_at is not None),
        "days": [(day0 + i * 86400, per_day[i]) for i in range(DAYS)],
        "recent": [(t, by_id.get(t["job_id"])) for t in transfers[:12]],
    }


def _midnight(ts: float) -> float:
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
