from dataclasses import replace

from availarr.agent.config import load_agent_config
from availarr.agent.db import AgentJob, AgentStore
import pytest


def _job(**kw):
    d = dict(nzo_id="n1", server_job_id=1, name="x", cat="radarr", state="queued", size=1000,
             phase=0.5, done=0, storage="", fail_message=None, hidden=False, created_at=1.0)
    return AgentJob(**{**d, **kw})


def test_agent_job_percentage():
    assert _job().percentage == 25
    assert _job(state="transferring", done=500).percentage == 75
    assert _job(state="completed").percentage == 100


def test_agent_store_roundtrip_and_active(tmp_path):
    s = AgentStore(str(tmp_path / "a.db"))
    j = _job(hidden=True)
    s.add(j)
    assert s.get("n1") == j and s.get("zz") is None
    s.add(_job(nzo_id="n2"))
    s.update(replace(j, state="completed"))
    assert [x.nzo_id for x in s.active()] == ["n2"]
    assert len(s.all()) == 2


def test_config(tmp_path):
    p = tmp_path / "a.toml"
    p.write_text('server_url="u"\ntoken="t"\napi_key="k"\npublic_url="p"\ncomplete_dir="/c"\n'
                 '[radarr]\nurl="r"\nkey="rk"\n[sonarr]\nurl="s"\nkey="sk"\n')
    c = load_agent_config(str(p))
    assert c.port == 8789 and c.sonarr_key == "sk"
    p.write_text('token="t"\n')
    with pytest.raises(ValueError):
        load_agent_config(str(p))


def test_migrates_reported_column(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE agent_jobs (nzo_id TEXT PRIMARY KEY, server_job_id INTEGER, name TEXT, cat TEXT, "
                  "state TEXT, size INTEGER, phase REAL, done INTEGER, storage TEXT, fail_message TEXT, "
                  "hidden INTEGER, created_at REAL)")
        c.execute("INSERT INTO agent_jobs VALUES ('o',1,'x','radarr','completed',1,1,1,'',NULL,0,1.0)")
    c.close()
    s = AgentStore(db)
    assert s.get("o").reported is False
    s.set_reported("o")
    assert s.get("o").reported is True
    AgentStore(db)  # idempotent
