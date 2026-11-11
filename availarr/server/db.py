import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, fields

from availarr.common import Item

_SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, b_profile TEXT, release_title TEXT,
 state TEXT, units TEXT, protected_units TEXT, added_by_tool INTEGER, hashes TEXT,
 created_at REAL, ready_at REAL, closed_at REAL, outcome TEXT, decision TEXT,
 deleted_at REAL, error TEXT)"""
_JSON = ("units", "protected_units", "hashes")


@dataclass
class Job:
    id: int
    item: Item
    b_profile: str
    release_title: str
    state: str  # pending | ready | failed | retried
    units: list[str]
    protected_units: list[str]
    added_by_tool: bool
    hashes: list[str]
    created_at: float
    ready_at: float | None
    closed_at: float | None
    outcome: str | None  # delivered | cancelled
    decision: str  # none | keep | delete
    deleted_at: float | None
    error: str | None


def _row(r: sqlite3.Row) -> Job:
    d = dict(r)
    d["item"] = Item.from_key(d["item"])
    d["added_by_tool"] = bool(d["added_by_tool"])
    for k in _JSON:
        d[k] = json.loads(d[k])
    return Job(**d)


def _vals(j: Job) -> dict:
    d = {f.name: getattr(j, f.name) for f in fields(Job)}
    d["item"] = j.item.key()
    for k in _JSON:
        d[k] = json.dumps(d[k])
    return d


class JobStore:
    def __init__(self, path: str):
        self.path = path
        self._run(_SCHEMA)

    def _run(self, sql: str, args=(), ids=None) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            with c:
                cur = c.execute(sql, args)
                rows = cur.fetchall()
            if ids is not None:
                ids.append(cur.lastrowid)
            return rows

    def create(self, item, b_profile, release_title, units, protected_units, added_by_tool, now) -> Job:
        ids: list[int] = []
        self._run(
            "INSERT INTO jobs (item,b_profile,release_title,state,units,protected_units,added_by_tool,"
            "hashes,created_at,decision) VALUES (?,?,?,'pending',?,?,?,'[]',?,'none')",
            (item.key(), b_profile, release_title, json.dumps(units), json.dumps(protected_units),
             int(added_by_tool), now),
            ids,
        )
        return self.get(ids[0])

    def get(self, id: int) -> Job | None:
        rows = self._run("SELECT * FROM jobs WHERE id=?", (id,))
        return _row(rows[0]) if rows else None

    def update(self, job: Job) -> None:
        v = _vals(job)
        sets = ",".join(f"{k}=:{k}" for k in v if k != "id")
        self._run(f"UPDATE jobs SET {sets} WHERE id=:id", v)

    def all(self) -> list[Job]:
        return [_row(r) for r in self._run("SELECT * FROM jobs ORDER BY id DESC")]

    def _one(self, item: Item, cond: str) -> Job | None:
        rows = self._run(f"SELECT * FROM jobs WHERE item=? AND {cond} ORDER BY id DESC LIMIT 1", (item.key(),))
        return _row(rows[0]) if rows else None

    def active_for(self, item: Item) -> Job | None:
        return self._one(item, "deleted_at IS NULL AND state IN ('pending','ready')")

    def failed_for(self, item: Item) -> Job | None:
        return self._one(item, "state='failed'")
