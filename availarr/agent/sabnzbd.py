import base64
import json
import logging
import os
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from availarr.agent.db import AgentJob
from availarr.agent.transfer import ETA
from availarr.common import Item, parse_nzb

log = logging.getLogger(__name__)
MB = 2**20


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def router(cfg, store, server) -> APIRouter:
    r = APIRouter()

    root = os.path.abspath(cfg.complete_dir)

    def inside(path: str) -> bool:
        p = os.path.abspath(path)
        return p != root and os.path.commonpath([p, root]) == root

    def add(payload: dict, cat: str) -> dict:
        try:
            if "placeholder" in payload:
                raise ValueError("placeholder release")
            item, prof, title, size = payload["item"], payload["b_profile"], payload["title"], payload["size"]
            if not (isinstance(item, str) and isinstance(prof, str) and isinstance(title, str)
                    and isinstance(size, int) and not isinstance(size, bool)):
                raise ValueError("bad payload")
            if cat not in ("", "*") and cat not in cfg.categories:
                raise ValueError("unknown category")
            if (not title or title in (".", "..") or "/" in title or "\\" in title
                    or os.path.basename(title) != title or os.path.isabs(title)):
                raise ValueError("bad title")
            storage = os.path.join(*[cfg.complete_dir] + ([cat] if cat not in ("", "*") else []) + [title])
            if not inside(storage):
                raise ValueError("bad storage path")
            sid = server.request(Item.from_key(item), prof, title)
            nzo = "SABnzbd_nzo_" + secrets.token_hex(8)
            store.add(AgentJob(nzo, sid, title, cat, "queued", size, 0.0, 0, storage, None, False, time.time()))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            return {"status": False, "error": str(e) or type(e).__name__}
        return {"status": True, "nzo_ids": [nzo]}

    def clean_parts(storage: str) -> None:
        if not inside(storage):
            return
        for dirpath, _, files in os.walk(storage, topdown=False):
            for f in files:
                if f.endswith(".part"):
                    os.remove(os.path.join(dirpath, f))
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    def in_cat(p: dict, j) -> bool:
        return p.get("cat") in (None, "", "*") or p["cat"] == j.cat

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
                    store.set_reported(nzo)
                except Exception as e:  # noqa: BLE001 - cancel must succeed locally
                    log.warning("server.close failed for %s: %s", nzo, e)
                clean_parts(j.storage)
                done.append(nzo)
            return {"status": True, "nzo_ids": done}
        slots = []
        for j in store.active():
            if not in_cat(p, j):
                continue
            pct = j.percentage
            slots.append({
                "nzo_id": j.nzo_id, "filename": j.name, "cat": j.cat,
                "status": "Downloading" if j.state == "transferring" else "Queued",
                "mb": "%.1f" % (j.size / MB), "mbleft": "%.1f" % max(0.0, j.size * (1 - pct / 100) / MB),
                "percentage": str(pct), "timeleft": _hms(ETA.get(j.nzo_id, 0)), "priority": "Normal",
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
        } for j in store.all() if j.state in ("completed", "failed") and not j.hidden and in_cat(p, j)]
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
            try:
                return add(parse_nzb(upload), p.get("cat", ""))
            except ValueError as e:
                return {"status": False, "error": str(e)}
        if mode == "addurl":
            # our own newznab link only: decode locally, never fetch (SSRF)
            try:
                u = urlsplit(p.get("name", ""))
                if not u.path.endswith("/newznab/nzb"):
                    raise ValueError
                payload = json.loads(base64.urlsafe_b64decode(parse_qs(u.query)["p"][0]))
                if not isinstance(payload, dict):
                    raise ValueError
            except (ValueError, KeyError, IndexError):
                return {"status": False, "error": "unsupported url"}
            return add(payload, p.get("cat", ""))
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
