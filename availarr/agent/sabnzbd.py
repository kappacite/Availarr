import logging
import os
import secrets
import time

import httpx
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from availarr.agent.db import AgentJob
from availarr.common import Item, parse_nzb

log = logging.getLogger(__name__)
MB = 2**20


def router(cfg, store, server) -> APIRouter:
    r = APIRouter()

    def add(data: bytes, cat: str) -> dict:
        try:
            payload = parse_nzb(data)
            item, prof, title = Item.from_key(payload["item"]), payload["b_profile"], payload["title"]
            size = payload["size"]
            sid = server.request(item, prof, title)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return {"status": False, "error": str(e) or type(e).__name__}
        nzo = "SABnzbd_nzo_" + secrets.token_hex(8)
        parts = [cfg.complete_dir] + ([cat] if cat not in ("", "*") else []) + [title]
        store.add(AgentJob(nzo, sid, title, cat, "queued", size, 0.0, 0, os.path.join(*parts), None, False, time.time()))
        return {"status": True, "nzo_ids": [nzo]}

    def clean_parts(storage: str) -> None:
        for root, _, files in os.walk(storage, topdown=False):
            for f in files:
                if f.endswith(".part"):
                    os.remove(os.path.join(root, f))
            try:
                os.rmdir(root)
            except OSError:
                pass

    def ids(p: dict):
        return [i for i in p.get("value", "").split(",") if i]

    def queue(p: dict) -> dict:
        if p.get("name") == "delete":
            done = []
            for nzo in ids(p):
                j = store.get(nzo)
                if not j or j.state not in ("queued", "transferring"):
                    continue
                j.state = "cancelled"
                store.update(j)
                try:
                    server.close(j.server_job_id, "cancelled")
                except Exception as e:  # noqa: BLE001 - cancel must succeed locally
                    log.warning("server.close failed for %s: %s", nzo, e)
                clean_parts(j.storage)
                done.append(nzo)
            return {"status": True, "nzo_ids": done}
        slots = []
        for j in store.active():
            pct = j.percentage
            slots.append({
                "nzo_id": j.nzo_id, "filename": j.name, "cat": j.cat,
                "status": "Downloading" if j.state == "transferring" else "Queued",
                "mb": "%.1f" % (j.size / MB), "mbleft": "%.1f" % (j.size * (1 - pct / 100) / MB),
                "percentage": str(pct), "timeleft": "0:00:00", "priority": "Normal",
            })
        return {"queue": {"paused": False, "slots": slots}}

    def history(p: dict) -> dict:
        if p.get("name") == "delete":
            done = []
            for nzo in ids(p):
                j = store.get(nzo)
                if j:
                    j.hidden = True
                    store.update(j)
                    done.append(nzo)
            return {"status": True, "nzo_ids": done}
        slots = [{
            "nzo_id": j.nzo_id, "name": j.name, "nzb_name": j.name, "category": j.cat,
            "status": "Completed" if j.state == "completed" else "Failed", "storage": j.storage,
            "bytes": j.size, "fail_message": j.fail_message or "", "download_time": 0,
        } for j in store.all() if j.state in ("completed", "failed") and not j.hidden]
        return {"history": {"slots": slots}}

    def handle(p: dict, upload: bytes | None) -> dict:
        mode = p.get("mode", "")
        if not secrets.compare_digest(str(p.get("apikey", "")).encode(), cfg.api_key.encode()):
            return {"status": False, "error": "API Key Incorrect"}
        if mode == "version":
            return {"version": "4.3.3"}
        if mode == "get_config":
            cats = [{"name": "*", "dir": "", "pp": "3", "script": "None", "priority": -100}]
            cats += [{"name": c, "dir": c, "pp": "", "script": "Default", "priority": -100} for c in cfg.categories]
            return {"config": {"misc": {"complete_dir": cfg.complete_dir, "history_retention": "0",
                                        "history_retention_option": "all", "pre_check": False},
                               "categories": cats, "sorters": []}}
        if mode == "fullstatus":
            return {"status": {"completedir": cfg.complete_dir}}
        if mode == "addfile" and upload is not None:
            return add(upload, p.get("cat", ""))
        if mode == "addurl":
            try:
                resp = httpx.get(p.get("name", ""), follow_redirects=True, timeout=30)
                resp.raise_for_status()
            except (httpx.HTTPError, ValueError) as e:
                return {"status": False, "error": str(e) or type(e).__name__}
            return add(resp.content, p.get("cat", ""))
        if mode == "queue":
            return queue(p)
        if mode == "history":
            return history(p)
        return {"status": False, "error": "not implemented"}

    @r.api_route("/sabnzbd/api", methods=["GET", "POST"])
    async def api(request: Request):
        p: dict = dict(request.query_params)
        upload = None
        if request.method == "POST":
            for k, v in (await request.form()).items():
                if isinstance(v, UploadFile):
                    if k in ("name", "nzbfile") and upload is None:
                        upload = await v.read()
                else:
                    p[k] = v
        return await run_in_threadpool(handle, p, upload)

    return r
