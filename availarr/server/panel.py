import math
import secrets
import time
from html import escape

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from availarr.server.config import ServerConfig
from availarr.server.jobs import Jobs
from availarr.server.retention import fully_protected, seed_ok, seed_seconds

FILTERS = {"all": "Tout", "pending": "À décider", "kept": "Gardés", "deleted": "Supprimés"}
ACTIONS = {"keep": "keep", "delete": "delete", "cancel": "none"}


def fmt_seed(seconds: int | None, seed_days: float) -> str:
    if seconds is None:
        return "—"
    d, h = divmod(int(seconds), 86400)
    h //= 3600
    if seconds >= seed_days * 86400:
        return f"{d} j {h:02d} h ✅"
    n = round(10 * min(1, seconds / (seed_days * 86400)))
    return f"{d} j {h:02d} h / {seed_days:g} j {'▓' * n}{'░' * (10 - n)}"


def fmt_removal(job, now: float, cfg: ServerConfig, seed_is_ok: bool) -> str:
    if job.deleted_at is not None:
        return "Supprimé"
    if job.decision == "keep":
        return "Gardé 📌"
    if fully_protected(job):
        return "jamais 🔒"
    if job.decision == "delete" and not seed_is_ok:
        return f"⏳ dès {cfg.seed_days:g} j de seed"
    if job.closed_at is not None:
        return f"dans {max(0, math.ceil((job.closed_at + cfg.keep_days * 86400 - now) / 86400))} j"
    return "—"


def create_panel(jobs: Jobs, qbit, cfg: ServerConfig, clock=time.time) -> FastAPI:
    basic = HTTPBasic()

    def auth(c: HTTPBasicCredentials = Depends(basic)) -> None:
        if not secrets.compare_digest(c.password.encode(), cfg.admin_password.encode()):
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    app = FastAPI(dependencies=[Depends(auth)])

    def state(job) -> str:
        if job.state == "ready" and fully_protected(job) and not job.hashes:
            return "Déjà chez toi"
        if job.outcome == "delivered":
            return "Livré ✅"
        if job.outcome == "cancelled":
            return "Annulé chez B"
        if job.state == "failed":
            return f"Échec : {job.error}"
        if job.state == "pending":
            return f"Téléchargement {round(jobs.status(job)['progress'] * 100)}%"
        if job.state == "ready":
            t = jobs.transfer.get(job.id)
            return f"Transfert {round(100 * t[0] / t[1]) if t[1] else 0}%" if t else "Prêt"
        return job.state

    def seed(job) -> tuple[str, bool]:
        if not job.hashes:
            return "—", True
        try:
            s = seed_seconds(job, qbit)
            return ("torrent absent" if s is None else fmt_seed(s, cfg.seed_days)), seed_ok(job, qbit, cfg.seed_days)
        except Exception:
            return "—", False

    def buttons(job) -> str:
        def form(act, label):
            return f'<form method="post" action="/jobs/{job.id}/{act}"><button>{label}</button></form>'
        if job.state == "failed":
            return form("retry", "Réessayer")
        if job.deleted_at is not None:
            return ""
        if job.decision == "none":
            return form("keep", "Garder") + form("delete", "Suppr.")
        return form("cancel", "Annuler")

    def show(job, f: str) -> bool:
        if f == "deleted":
            return job.deleted_at is not None
        if f == "kept":
            return job.decision == "keep" or fully_protected(job)
        if f == "pending":
            return (job.deleted_at is None and job.decision == "none" and not fully_protected(job)
                    and job.state in ("pending", "ready"))
        return True

    @app.get("/", response_class=HTMLResponse)
    def index(f: str = "all"):
        now = clock()
        rows = ""
        for job in sorted(jobs.store.all(), key=lambda j: j.created_at, reverse=True):
            if not show(job, f):
                continue
            seed_txt, ok = seed(job)
            rows += (f"<tr><td>{escape(job.release_title)}</td><td>{escape(state(job))}</td>"
                     f"<td>{escape(seed_txt)}</td><td>{escape(fmt_removal(job, now, cfg, ok))}</td>"
                     f"<td>{buttons(job)}</td></tr>")
        banner = (f'<p class="warn">Profils de B sans correspondance : '
                  f'{escape(", ".join(sorted(jobs.unmapped)))}</p>') if jobs.unmapped else ""
        links = " ".join(f'<a href="/?f={k}">{v}</a>' for k, v in FILTERS.items())
        return (f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Availarr</title><style>'
                "body{font-family:sans-serif;margin:2em}table{border-collapse:collapse;width:100%}"
                "td,th{border-bottom:1px solid #ccc;padding:.4em;text-align:left}form{display:inline}"
                ".warn{background:#fe9;padding:.5em}</style></head><body><h1>Availarr</h1>"
                f"{banner}<p>{links}</p><table><tr><th>Titre</th><th>État</th><th>Seed</th>"
                f"<th>Suppression</th><th>Actions</th></tr>{rows}</table></body></html>")

    @app.post("/jobs/{id}/{action}")
    def act(id: int, action: str):
        job = jobs.store.get(id)
        if job is None or action not in (*ACTIONS, "retry"):
            raise HTTPException(404)
        if action == "retry":
            if job.state != "failed":
                raise HTTPException(409)
            job.state = "retried"
        else:
            job.decision = ACTIONS[action]
        jobs.store.update(job)
        return RedirectResponse("/", 303)

    return app
