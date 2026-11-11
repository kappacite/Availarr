import re
import secrets
import time
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from availarr.common import Item
from availarr.server.config import ServerConfig
from availarr.server.jobs import Jobs


class LookupIn(BaseModel):
    item: str
    b_profile: str


class RequestIn(LookupIn):
    release_title: str


class CloseIn(BaseModel):
    outcome: Literal["delivered", "cancelled"]


def iter_file(path: str, start: int, mbps: float, chunk: int = 1 << 20,
              clock=time.monotonic, sleep=time.sleep) -> Iterator[bytes]:
    # ponytail: per-connection limit, global token bucket if several transfers saturate upload
    t0, sent, rate = clock(), 0, mbps * 125_000
    with open(path, "rb") as f:
        f.seek(start)
        while data := f.read(chunk):
            sent += len(data)
            yield data
            if rate > 0 and (wait := sent / rate - (clock() - t0)) > 0:
                sleep(wait)


def _item(key: str) -> Item:
    try:
        return Item.from_key(key)
    except ValueError:
        raise HTTPException(400, f"bad item key: {key}")


def create_api(jobs: Jobs, cfg: ServerConfig) -> FastAPI:
    def auth(authorization: str = Header("")):
        if not secrets.compare_digest(authorization.encode(), f"Bearer {cfg.token}".encode()):
            raise HTTPException(401, "bad token")

    app = FastAPI(dependencies=[Depends(auth)])

    def job_or_404(id: int):
        if (job := jobs.store.get(id)) is None:
            raise HTTPException(404, "unknown job")
        return job

    @app.post("/v1/lookup")
    def lookup(body: LookupIn):
        return {"release": jobs.lookup(_item(body.item), body.b_profile)}

    @app.post("/v1/requests")
    def request(body: RequestIn):
        item = _item(body.item)
        try:
            return {"job_id": jobs.request(item, body.b_profile, body.release_title).id}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/v1/requests/{id}")
    def status(id: int):
        return jobs.status(jobs.refresh(job_or_404(id)))

    @app.get("/v1/requests/{id}/files/{index}")
    def file(id: int, index: int, request: Request):
        try:
            path, size = jobs.file_path(jobs.refresh(job_or_404(id)), index)
        except IndexError:
            raise HTTPException(404, "unknown file")
        start, status_code = 0, 200
        if m := re.fullmatch(r"bytes=(\d+)-", request.headers.get("range", "")):
            start, status_code = int(m[1]), 206
            if start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        headers = {"Content-Length": str(size - start), "Accept-Ranges": "bytes"}
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"

        def body():
            pos = start
            for data in iter_file(path, start, cfg.bandwidth_mbps):
                pos += len(data)
                jobs.transfer[id] = (max(pos, jobs.transfer.get(id, (0, 0))[0]), size)
                yield data

        return StreamingResponse(body(), status_code=status_code, headers=headers,
                                 media_type="application/octet-stream")

    @app.post("/v1/requests/{id}/close", status_code=204)
    def close(id: int, body: CloseIn):
        jobs.close(job_or_404(id), body.outcome)

    return app
