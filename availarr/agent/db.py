import sqlite3
from contextlib import closing
from dataclasses import dataclass, fields

_SCHEMA = """CREATE TABLE IF NOT EXISTS agent_jobs (
 nzo_id TEXT PRIMARY KEY, server_job_id INTEGER, name TEXT, cat TEXT, state TEXT, size INTEGER,
 phase REAL, done INTEGER, storage TEXT, fail_message TEXT, hidden INTEGER, created_at REAL,
 reported INTEGER DEFAULT 0)"""


@dataclass
class AgentJob:
    nzo_id: str
    server_job_id: int
    name: str
    cat: str
    state: str  # queued | transferring | completed | failed | cancelled
    size: int
    phase: float
    done: int
    storage: str
    fail_message: str | None
    hidden: bool
    created_at: float
    reported: bool = False  # server.close succeeded (completed/cancelled jobs)

    @property
    def percentage(self) -> int:
        if self.state == "queued":
            return int(50 * min(1.0, max(0.0, self.phase)))
        if self.state == "transferring":
            return 50 + int(50 * min(1.0, self.done / self.size)) if self.size else 50
        return 100 if self.state == "completed" else 0


_NAMES = [f.name for f in fields(AgentJob)]


class AgentStore:
    def __init__(self, path: str):
        self.path = path
        self._run(_SCHEMA)
        if "reported" not in {r["name"] for r in self._run("PRAGMA table_info(agent_jobs)")}:
            self._run("ALTER TABLE agent_jobs ADD COLUMN reported INTEGER DEFAULT 0")

    def _run(self, sql: str, args=()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            with c:
                return c.execute(sql, args).fetchall()

    @staticmethod
    def _job(r: sqlite3.Row) -> AgentJob:
        return AgentJob(**{**dict(r), "hidden": bool(r["hidden"]), "reported": bool(r["reported"])})

    def add(self, job: AgentJob) -> None:
        self._run(
            f"INSERT INTO agent_jobs VALUES ({','.join('?' * len(_NAMES))})",
            [getattr(job, n) for n in _NAMES],
        )

    def get(self, nzo_id: str) -> AgentJob | None:
        rows = self._run("SELECT * FROM agent_jobs WHERE nzo_id=?", (nzo_id,))
        return self._job(rows[0]) if rows else None

    def update(self, job: AgentJob) -> None:
        sets = ",".join(f"{n}=?" for n in _NAMES[1:])
        self._run(f"UPDATE agent_jobs SET {sets} WHERE nzo_id=?",
                  [getattr(job, n) for n in _NAMES[1:]] + [job.nzo_id])

    def set_done(self, nzo_id: str, done: int) -> bool:
        """Progress write that never resurrects a job cancelled meanwhile; False if it is no longer transferring."""
        return bool(self._run("UPDATE agent_jobs SET done=? WHERE nzo_id=? AND state='transferring' RETURNING nzo_id",
                              (done, nzo_id)))

    def set_reported(self, nzo_id: str) -> None:
        self._run("UPDATE agent_jobs SET reported=1 WHERE nzo_id=?", (nzo_id,))

    def all(self) -> list[AgentJob]:
        return [self._job(r) for r in self._run("SELECT * FROM agent_jobs ORDER BY created_at")]

    def active(self) -> list[AgentJob]:
        return [j for j in self.all() if j.state in ("queued", "transferring")]
