import math
import re
import secrets
import time
from html import escape

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from availarr.server.config import ServerConfig
from availarr.server.jobs import Jobs
from availarr.server.retention import fully_protected, seed_ok, seed_seconds

FILTERS = {"all": "Tout", "active": "En cours", "pending": "À décider", "kept": "Gardés", "deleted": "Supprimés"}
ACTIONS = {"keep": "keep", "delete": "delete", "cancel": "none"}
POLL_MS = 4000  # ponytail: each poll asks A's Radarr/Sonarr queue per downloading job; fine for a handful


def fmt_seed(seconds: int | None, seed_days: float) -> str:
    if seconds is None:
        return "—"
    d, h = divmod(int(seconds), 86400)
    h //= 3600
    if seconds >= seed_days * 86400:
        return f"{d} j {h:02d} h ✅"
    return f"{d} j {h:02d} h / {seed_days:g} j"


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


def fmt_bytes(n: float) -> str:
    for unit in ("o", "Ko", "Mo"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} Go".replace(".", ",")


def fmt_ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 3600:
        return f"il y a {max(1, s // 60)} min"
    if s < 86400:
        return f"il y a {s // 3600} h"
    return f"il y a {s // 86400} j"


def fmt_eta(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{max(1, m)} min"


def what(item) -> str:
    if item.kind == "movie":
        return "Film"
    if item.kind == "season":
        return f"Série, saison {item.season}"
    return f"Série, S{item.season:02d}E{item.episode:02d}"


def split_title(release: str) -> tuple[str, list[str]]:
    """'Dune.Part.Two.2024.MULTi.2160p.WEB-DL-Availarr' -> ('Dune Part Two (2024)', ['MULTi', '2160p', 'WEB-DL'])."""
    parts = release.removesuffix("-Availarr").split(".")
    for i in range(len(parts) - 1, 0, -1):  # last match, so 'Blade.Runner.2049.2017' keeps 2049 in the name
        if re.fullmatch(r"(19|20)\d\d", parts[i]):
            return f"{' '.join(parts[:i])} ({parts[i]})", parts[i + 1:]
        if re.fullmatch(r"S\d\d(E\d\d)?", parts[i]):
            return f"{' '.join(parts[:i])} {parts[i]}", parts[i + 1:]
    return " ".join(parts), []


CSS = """
:root{--bg:#1c2333;--surface:#242d40;--line:#34405a;--text:#e6eaf2;--muted:#8e99ae;
--dl:#f0a443;--tx:#43c6db;--ok:#6fcf8e;--bad:#e5707a;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:32px 24px 64px}
header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:24px}
h1{margin:0;font-size:30px;font-weight:800;letter-spacing:-.02em}
h1 small{display:block;font-size:14px;font-weight:400;letter-spacing:0;color:var(--muted);margin-top:2px}
.live{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px}
.live i{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block}
body.stale .live i{background:var(--bad)}
.warn{background:#4a3a1c;border:1px solid var(--dl);color:#ffe2b0;padding:10px 14px;border-radius:8px;margin:0 0 16px}
nav{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
nav a{color:var(--muted);text-decoration:none;padding:6px 12px;border-radius:999px;border:1px solid var(--line)}
nav a b{color:var(--text);font-weight:600;margin-left:6px}
nav a.on{background:var(--text);color:var(--bg);border-color:var(--text)}
nav a.on b{color:var(--bg)}
nav a:focus-visible,button:focus-visible{outline:2px solid var(--tx);outline-offset:2px}
ul{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
li{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,2fr) minmax(0,1fr) 190px;gap:24px;align-items:center;
padding:18px 4px;border-bottom:1px solid var(--line)}
li.dim{opacity:.55}
.t{font-weight:650;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:13px;margin-top:2px}
.err{color:var(--bad);font-size:13px;margin-top:4px}
.bar{margin:4px 0}
.bar .lbl{display:flex;justify-content:space-between;gap:8px;font-size:12.5px;color:var(--muted)}
.bar .lbl b{color:var(--text);font-weight:600}
.track{height:8px;border-radius:4px;background:#1a2130;overflow:hidden;margin-top:3px}
.fill{height:100%;border-radius:4px;transition:width .8s ease}
.dl .fill{background:var(--dl)}.tx .fill{background:var(--tx)}.seed .fill{background:var(--ok)}
.done .fill{opacity:.45}
.run .fill{background-image:linear-gradient(90deg,transparent,rgba(255,255,255,.28),transparent);
background-size:200% 100%;animation:run 1.6s linear infinite}
@keyframes run{from{background-position:200% 0}to{background-position:-200% 0}}
.rm{font-size:13px;margin-top:6px}
.acts{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}
.tags{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.tags span{font-size:11.5px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 6px}
form{margin:0}
button{font:inherit;font-size:13px;color:var(--text);background:var(--surface);border:1px solid var(--line);
border-radius:6px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--muted)}
button.del{color:var(--bad)}
.empty{color:var(--muted);padding:40px 4px}
@media (max-width:820px){li{grid-template-columns:1fr;gap:10px}.acts{justify-content:flex-start}}
@media (prefers-reduced-motion:reduce){.fill{transition:none}.run .fill{animation:none}}
"""

JS = """
const main=document.getElementById('live');
async function tick(){
  if(document.hidden)return;
  try{
    const r=await fetch('/fragment'+location.search,{cache:'no-store'});
    if(!r.ok)throw 0;
    main.innerHTML=await r.text();document.body.classList.remove('stale');
  }catch(e){document.body.classList.add('stale')}
}
setInterval(tick,%d);
""" % POLL_MS


def create_panel(jobs: Jobs, qbit, cfg: ServerConfig, clock=time.time) -> FastAPI:
    basic = HTTPBasic()
    samples: dict[int, tuple[float, int, float]] = {}  # job id -> (monotonic time, bytes, smoothed bytes/s)

    def auth(c: HTTPBasicCredentials = Depends(basic)) -> None:
        if not secrets.compare_digest(c.password.encode(), cfg.admin_password.encode()):
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    app = FastAPI(dependencies=[Depends(auth)])

    def rate(job_id: int, pos: int) -> float:
        now = time.monotonic()
        t0, p0, r0 = samples.get(job_id, (now, pos, 0.0))
        r = r0
        if now - t0 >= 1:
            inst = max(0.0, (pos - p0) / (now - t0))
            r = inst if r0 == 0 else 0.6 * inst + 0.4 * r0
            samples[job_id] = (now, pos, r)
        elif job_id not in samples:
            samples[job_id] = (now, pos, 0.0)
        return r

    def bar(kind: str, label: str, pct: float | None, value: str, extra: str = "", running=False, done=False) -> str:
        w = 0 if pct is None else max(0.0, min(100.0, pct))
        cls = f"bar {kind}" + (" run" if running else "") + (" done" if done else "")
        right = escape(value) + (f", {escape(extra)}" if extra else "")
        return (f'<div class="{cls}"><div class="lbl"><span>{escape(label)}</span><b>{right}</b></div>'
                f'<div class="track" role="progressbar" aria-label="{escape(label)}" aria-valuemin="0" '
                f'aria-valuemax="100" aria-valuenow="{round(w)}"><div class="fill" style="width:{w:.1f}%"></div>'
                f"</div></div>")

    def download_bar(job) -> str:
        label = "Téléchargement chez toi"
        if fully_protected(job) and not job.hashes and job.state == "ready":
            return bar("dl", label, 100, "Déjà chez toi", done=True)
        if job.state == "pending":
            try:
                st = jobs.status(job)
            except Exception:  # A's Radarr/Sonarr down: the page must still render
                return bar("dl", label, None, "Téléchargement …")
            p, eta = st["progress"] * 100, st.get("eta")
            extra = "en attente d'un torrent" if p == 0 else (f"reste {fmt_eta(eta)}" if eta else "")
            return bar("dl", label, p, f"{round(p)} %", extra, running=p > 0)
        if job.state == "ready" or job.outcome:
            return bar("dl", label, 100, "Terminé", done=True)
        return bar("dl", label, None, "—")

    def transfer_bar(job) -> str:
        label = "Transfert vers B"
        if job.outcome == "delivered":
            return bar("tx", label, 100, "Livré ✅", done=True)
        if job.outcome == "cancelled":
            return bar("tx", label, None, "Annulé chez B")
        t = jobs.transfer.get(job.id)
        if t and job.state == "ready":
            pos, total = t
            pct = 100 * pos / total if total else 0
            r = rate(job.id, pos)
            extra = f"{fmt_bytes(pos)} / {fmt_bytes(total)}"
            if r > 0 and pos < total:
                extra += f", {fmt_bytes(r)}/s, reste {fmt_eta((total - pos) / r)}"
            return bar("tx", label, pct, f"{round(pct)} %", extra, running=pos < total)
        if job.state == "ready":
            return bar("tx", label, 0, "En attente de B")
        if job.state == "pending":
            return bar("tx", label, 0, "Après le téléchargement")
        return bar("tx", label, None, "—")

    def seed(job) -> tuple[str, float | None, bool]:
        if not job.hashes:
            return "—", None, True
        try:
            s = seed_seconds(job, qbit)
            ok = seed_ok(job, qbit, cfg.seed_days)
        except Exception:
            return "qBittorrent injoignable", None, False
        if s is None:
            return "torrent absent", None, ok
        return fmt_seed(s, cfg.seed_days), 100 * s / (cfg.seed_days * 86400), ok

    def buttons(job, f: str) -> str:
        def form(act, label, cls=""):
            c = f' class="{cls}"' if cls else ""
            return f'<form method="post" action="/jobs/{job.id}/{act}?f={f}"><button{c}>{label}</button></form>'
        if job.state == "failed":
            return form("retry", "Réessayer")
        if job.deleted_at is not None:
            return ""
        if fully_protected(job) and job.decision != "keep":
            return ""
        if job.decision == "none":
            return form("keep", "Garder") + form("delete", "Supprimer", "del")
        return form("cancel", "Annuler la décision")

    def active(job) -> bool:
        return job.deleted_at is None and job.outcome is None and job.state in ("pending", "ready")

    def show(job, f: str) -> bool:
        if f == "deleted":
            return job.deleted_at is not None
        if f == "kept":
            return job.decision == "keep" or fully_protected(job)
        if f == "active":
            return active(job)
        if f == "pending":
            return (job.deleted_at is None and job.decision == "none" and not fully_protected(job)
                    and job.state in ("pending", "ready"))
        return True

    def row(job, now: float, f: str) -> str:
        seed_txt, seed_pct, ok = seed(job)
        meta = f"{what(job.item)}, profil {job.b_profile}, demandé {fmt_ago(now - job.created_at)}"
        err = f'<div class="err">Échec : {escape(job.error or "")}</div>' if job.state == "failed" else ""
        seed_html = (bar("seed", "Seed réel", seed_pct, seed_txt, done=seed_pct is not None and seed_pct >= 100)
                     if job.hashes else bar("seed", "Seed réel", None, seed_txt))
        dim = " dim" if job.deleted_at is not None or job.state == "failed" else ""
        name, tags = split_title(job.release_title)
        chips = "".join(f"<span>{escape(t)}</span>" for t in tags)
        return (f'<li class="job{dim}"><div><div class="t" title="{escape(job.release_title)}">{escape(name)}</div>'
                f'<div class="meta">{escape(meta)}</div>{err}<div class="tags">{chips}</div></div>'
                f"<div>{download_bar(job)}{transfer_bar(job)}</div>"
                f'<div>{seed_html}<div class="rm">Suppression : {escape(fmt_removal(job, now, cfg, ok))}</div></div>'
                f'<div class="acts">{buttons(job, f)}</div></li>')

    def fragment(f: str) -> str:
        f = f if f in FILTERS else "all"
        now, all_jobs = clock(), sorted(jobs.store.all(), key=lambda j: j.created_at, reverse=True)
        banner = (f'<p class="warn">Profils de B sans correspondance dans [profiles] : '
                  f'{escape(", ".join(sorted(jobs.unmapped)))}</p>') if jobs.unmapped else ""
        tabs = "".join(f'<a href="/?f={k}"{" class=on" if k == f else ""}>{v}'
                       f"<b>{sum(show(j, k) for j in all_jobs)}</b></a>" for k, v in FILTERS.items())
        rows = "".join(row(j, now, f) for j in all_jobs if show(j, f))
        return (f'{banner}<nav aria-label="Filtres">{tabs}</nav>'
                + (f"<ul>{rows}</ul>" if rows else '<p class="empty">Aucune demande ici pour l\'instant. '
                   "Elles apparaissent dès que le Radarr ou le Sonarr de B récupère une release Availarr.</p>"))

    @app.get("/fragment", response_class=HTMLResponse)
    def frag(f: str = "all"):
        return fragment(f)

    @app.get("/", response_class=HTMLResponse)
    def index(f: str = "all"):
        return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Availarr</title>'
                f"<style>{CSS}</style></head><body><div class=\"wrap\"><header><h1>Availarr"
                "<small>Les demandes de B, de ton qBittorrent jusqu'à sa bibliothèque</small></h1>"
                f'<div class="live"><i></i>Mise à jour toutes les {POLL_MS // 1000} s</div></header>'
                f'<main id="live">{fragment(f)}</main></div><script>{JS}</script></body></html>')

    @app.post("/jobs/{id}/{action}")
    def act(id: int, action: str, f: str = "all"):
        job = jobs.store.get(id)
        if job is None or action not in (*ACTIONS, "retry"):
            raise HTTPException(404)
        if action == "retry":
            if job.state != "failed":
                raise HTTPException(409)
            jobs.store.set(id, state="retried")
        else:
            jobs.store.set(id, decision=ACTIONS[action])
        return RedirectResponse(f"/?f={f if f in FILTERS else 'all'}", 303)

    return app
