import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, fields

from availarr.common import Item

_SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, b_profile TEXT, release_title TEXT,
 state TEXT, units TEXT, protected_units TEXT, added_by_tool INTEGER, hashes TEXT,
 created_at REAL, ready_at REAL, closed_at REAL, outcome TEXT, decision TEXT,
 deleted_at REAL, error TEXT, agent TEXT DEFAULT 'legacy', hidden_at REAL)"""
_SCHEMA_AGENTS = """CREATE TABLE IF NOT EXISTS agents (
 id TEXT PRIMARY KEY, name TEXT, first_seen REAL, last_seen REAL, blocked INTEGER DEFAULT 0,
 renamed INTEGER DEFAULT 0)"""
_SCHEMA_TRANSFERS = """CREATE TABLE IF NOT EXISTS transfers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, started_at REAL, seconds REAL, bytes INTEGER,
 complete INTEGER)"""
_JSON = ("units", "protected_units", "hashes")
_OWNED = ("decision", "closed_at", "outcome", "deleted_at", "hidden_at")  # written only through set(), never by update()


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
    agent: str = "legacy"
    hidden_at: float | None = None


@dataclass
class Agent:
    id: str
    name: str
    first_seen: float
    last_seen: float
    blocked: bool
    renamed: bool


def _row(r: sqlite3.Row) -> Job:
    d = dict(r)
    d["item"] = Item.from_key(d["item"])
    d["added_by_tool"] = bool(d["added_by_tool"])
    for k in _JSON:
        d[k] = json.loads(d[k])
    return Job(**d)


def _agent(r: sqlite3.Row) -> Agent:
    return Agent(**{**dict(r), "blocked": bool(r["blocked"]), "renamed": bool(r["renamed"])})


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
        self._run(_SCHEMA_TRANSFERS)
        self._run(_SCHEMA_AGENTS)
        cols = {r["name"] for r in self._run("PRAGMA table_info(jobs)")}
        for col, decl in (("agent", "TEXT DEFAULT 'legacy'"), ("hidden_at", "REAL")):
            if col not in cols:
                self._run(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")

    def _run(self, sql: str, args=(), ids=None) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            with c:
                cur = c.execute(sql, args)
                rows = cur.fetchall()
            if ids is not None:
                ids.append(cur.lastrowid)
            return rows

    def create(self, item, b_profile, release_title, units, protected_units, added_by_tool, now,
               agent: str = "legacy") -> Job:
        ids: list[int] = []
        self._run(
            "INSERT INTO jobs (item,b_profile,release_title,state,units,protected_units,added_by_tool,"
            "hashes,created_at,decision,agent) VALUES (?,?,?,'pending',?,?,?,'[]',?,'none',?)",
            (item.key(), b_profile, release_title, json.dumps(units), json.dumps(protected_units),
             int(added_by_tool), now, agent),
            ids,
        )
        return self.get(ids[0])

    def get(self, id: int) -> Job | None:
        rows = self._run("SELECT * FROM jobs WHERE id=?", (id,))
        return _row(rows[0]) if rows else None

    def update(self, job: Job) -> None:
        """Write the refresh-owned columns; decision/closed_at/outcome/deleted_at go through set()."""
        v = {k: x for k, x in _vals(job).items() if k not in _OWNED}
        sets = ",".join(f"{k}=:{k}" for k in v if k != "id")
        self._run(f"UPDATE jobs SET {sets} WHERE id=:id", v)

    def set(self, id: int, **cols) -> None:
        """Column-targeted write, so concurrent writers (panel, API, retention) never clobber each other."""
        names = {f.name for f in fields(Job)} - {"id", "item"}
        if not cols or set(cols) - names:
            raise ValueError(f"bad columns: {sorted(cols)}")
        v = {k: json.dumps(x) if k in _JSON else x for k, x in cols.items()}
        self._run(f"UPDATE jobs SET {','.join(f'{k}=:{k}' for k in v)} WHERE id=:id", {**v, "id": id})

    def add_transfer(self, job_id: int, started_at: float, seconds: float, nbytes: int, complete: bool) -> None:
        """One file served to B (or a piece of it, when the connection dropped and B resumed later)."""
        self._run("INSERT INTO transfers (job_id,started_at,seconds,bytes,complete) VALUES (?,?,?,?,?)",
                  (job_id, started_at, seconds, nbytes, int(complete)))

    def transfers(self) -> list[dict]:
        return [dict(r) for r in self._run("SELECT * FROM transfers ORDER BY started_at DESC, id DESC")]

    def avg_rate(self, last: int = 20) -> float | None:
        """Mean A -> B throughput (bytes/s) over the last transfers, None before the first one."""
        r = self._run("SELECT SUM(bytes) b, SUM(seconds) s FROM (SELECT bytes, seconds FROM transfers "
                      "WHERE seconds >= 1 AND bytes > 0 ORDER BY id DESC LIMIT ?)", (last,))[0]
        return r["b"] / r["s"] if r["s"] else None

    def all(self) -> list[Job]:
        return [_row(r) for r in self._run("SELECT * FROM jobs ORDER BY id DESC")]

    def _one(self, item: Item, agent: str, cond: str) -> Job | None:
        rows = self._run(f"SELECT * FROM jobs WHERE item=? AND agent=? AND {cond} ORDER BY id DESC LIMIT 1",
                         (item.key(), agent))
        return _row(rows[0]) if rows else None

    def active_for(self, item: Item, agent: str) -> Job | None:
        return self._one(item, agent, "deleted_at IS NULL AND state IN ('pending','ready')")

    def failed_for(self, item: Item, agent: str) -> Job | None:
        return self._one(item, agent, "state='failed'")

    def seen(self, agent_id: str, name: str, now: float) -> Agent:
        """Record a call from an agent. The first UUID agent adopts the jobs of the pre-multi-agent days."""
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            with c:
                if c.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                    c.execute("UPDATE agents SET last_seen=?, name=CASE WHEN renamed THEN name ELSE ? END WHERE id=?",
                              (now, name, agent_id))
                else:
                    first = agent_id != "legacy" and not c.execute(
                        "SELECT 1 FROM agents WHERE id!='legacy'").fetchone()
                    c.execute("INSERT INTO agents (id,name,first_seen,last_seen) VALUES (?,?,?,?)",
                              (agent_id, name, now, now))
                    if first:
                        c.execute("UPDATE jobs SET agent=? WHERE agent='legacy'", (agent_id,))
                return _agent(c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())

    def ensure_agent(self, id: str, name: str) -> None:
        """Create the row of an agent known only through its jobs, without marking it seen."""
        self._run("INSERT OR IGNORE INTO agents (id,name) VALUES (?,?)", (id, name))

    def agents(self) -> list[Agent]:
        return [_agent(r) for r in self._run("SELECT * FROM agents ORDER BY first_seen, id")]

    def agent(self, id: str) -> Agent | None:
        rows = self._run("SELECT * FROM agents WHERE id=?", (id,))
        return _agent(rows[0]) if rows else None

    def set_agent(self, id: str, **cols) -> None:
        if not cols or set(cols) - {"name", "blocked", "renamed"}:
            raise ValueError(f"bad columns: {sorted(cols)}")
        self._run(f"UPDATE agents SET {','.join(f'{k}=:{k}' for k in cols)} WHERE id=:id", {**cols, "id": id})
