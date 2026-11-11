import logging
import os
from pathlib import Path
from typing import Callable

import httpx

from availarr.agent.db import AgentStore

log = logging.getLogger(__name__)


class ServerClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=base_url, timeout=httpx.Timeout(None, connect=30))
        self.headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        r = self.client.request(method, path, headers=self.headers, **kw)
        r.raise_for_status()
        return r

    def lookup(self, item: str, b_profile: str) -> dict | None:
        return self._call("POST", "/v1/lookup", json={"item": item, "b_profile": b_profile}).json()["release"]

    def request(self, item: str, b_profile: str, title: str) -> int:
        body = {"item": item, "b_profile": b_profile, "release_title": title}
        return self._call("POST", "/v1/requests", json=body).json()["job_id"]

    def status(self, job_id: int) -> dict:
        return self._call("GET", f"/v1/requests/{job_id}").json()

    def close(self, job_id: int, outcome: str) -> None:
        self._call("POST", f"/v1/requests/{job_id}/close", json={"outcome": outcome})

    def download(self, job_id: int, index: int, dest: Path, size: int, on_progress: Callable[[int], None]) -> None:
        # size is advisory: the server serves the on-disk size, which is the authoritative total
        dest = Path(dest)
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {**self.headers, **({"Range": f"bytes={have}-"} if have else {})}
        with self.client.stream("GET", f"/v1/requests/{job_id}/files/{index}", headers=headers) as r:
            if r.status_code == 416 and have:
                part.replace(dest)  # .part already holds the whole file
                return
            r.raise_for_status()
            if r.status_code == 206:
                total = int(r.headers["Content-Range"].rpartition("/")[2])
            else:
                have, total = 0, int(r.headers["Content-Length"])
            with open(part, "ab" if have else "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    have += len(chunk)
                    on_progress(have)
        if have != total:
            raise OSError(f"flux incomplet: {have}/{total}")
        part.replace(dest)


def _safe(name) -> bool:
    return (isinstance(name, str) and name not in ("", ".", "..") and "/" not in name
            and "\\" not in name and os.path.basename(name) == name and not os.path.isabs(name))


def step(store: AgentStore, server: ServerClient) -> None:
    for job in store.active():
        try:
            st = server.status(job.server_job_id)
            if st["state"] == "failed":
                job.state, job.fail_message = "failed", st["error"]
            elif st["state"] == "pending":
                job.phase = st["progress"]
            elif st["state"] == "ready":
                _transfer(store, server, job, st["files"])
            else:
                continue
            store.update(job)
        except (httpx.HTTPError, OSError) as e:
            log.warning("job %s: %s", job.nzo_id, e)


def _transfer(store, server, job, files) -> None:
    if not all(_safe(f["name"]) for f in files):
        job.state, job.fail_message = "failed", "nom de fichier invalide"
        return
    job.state, job.size = "transferring", sum(f["size"] for f in files)
    store.update(job)
    base = 0
    for f in files:
        def progress(n, base=base):
            job.done = base + n
            store.update(job)
        server.download(job.server_job_id, f["index"], Path(job.storage) / f["name"], f["size"], progress)
        base += f["size"]
        job.done = base
    job.state = "completed"
    server.close(job.server_job_id, "delivered")
