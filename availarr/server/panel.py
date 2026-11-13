import math
import re
import secrets
import time
from html import escape

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from availarr.server import stats
from availarr.ui import CSS, LOGO
from availarr.server.config import ServerConfig
from availarr.server.jobs import Jobs
from availarr.server.retention import fully_protected, seed_ok, seed_seconds

FILTERS = {"all": "Tout", "active": "En cours", "pending": "À décider", "kept": "Gardés", "deleted": "Supprimés"}
ACTIONS = {"keep": "keep", "delete": "delete", "cancel": "none"}
POLL_MS = 4000  # ponytail: each poll asks A's Radarr/Sonarr queue per downloading job; fine for a handful
STATS_POLL_MS = 15000
AGENT_ONLINE_S = 120  # the agent pings every poll_seconds (30 s by default)


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


def fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    if s < 86400:
        return f"{s // 3600} h {s // 60 % 60:02d} min"
    return f"{s // 86400} j {s // 3600 % 24} h"


def fmt_mo(bps: float | None) -> str:
    return f"{bps / 2**20:.1f} Mo/s".replace(".", ",") if bps else "—"


def fmt_mbit(bps: float | None) -> str:
    return f"{bps * 8 / 1e6:.0f} Mbit/s" if bps else "—"


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


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n > 1 else ''}"


JS = """
const main=document.getElementById('live');
async function tick(){
  if(document.hidden)return;
  try{
    const r=await fetch(main.dataset.src+location.search,{cache:'no-store'});
    if(!r.ok)throw 0;
    main.innerHTML=await r.text();document.body.classList.remove('stale');
  }catch(e){document.body.classList.add('stale')}
}
setInterval(tick,+main.dataset.every);
"""


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
        label = "Transfert vers le receveur"
        if job.outcome == "delivered":
            return bar("tx", label, 100, "Livré ✅", done=True)
        if job.outcome == "cancelled":
            return bar("tx", label, None, "Annulé chez le receveur")
        t = jobs.transfer.get(job.id)
        if t and job.state == "ready":
            pos, total = t
            pct = 100 * pos / total if total else 0
            r = rate(job.id, pos)
            extra = f"{fmt_bytes(pos)} / {fmt_bytes(total)}"
            if r > 0 and pos < total:
                extra += f", {fmt_mo(r)}, reste {fmt_eta((total - pos) / r)}"
            return bar("tx", label, pct, f"{round(pct)} %", extra, running=pos < total)
        if job.state == "ready":
            return bar("tx", label, 0, "En attente du receveur")
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

    def pill(job) -> str:
        if job.deleted_at is not None:
            return '<span class="pill">Supprimé</span>'
        if job.state == "failed":
            return '<span class="pill p-bad">Échec</span>'
        if job.outcome == "delivered":
            return '<span class="pill p-ok">Livré</span>'
        if job.outcome == "cancelled":
            return '<span class="pill">Annulé chez le receveur</span>'
        if job.state == "pending":
            return '<span class="pill p-dl">Téléchargement</span>'
        if jobs.transfer.get(job.id):
            return '<span class="pill p-tx">Transfert</span>'
        if fully_protected(job) and not job.hashes:
            return '<span class="pill p-lib">Dans ta bibliothèque</span>'
        return '<span class="pill p-tx">Prêt à envoyer</span>'

    def row(job, now: float, f: str) -> str:
        seed_txt, seed_pct, ok = seed(job)
        meta = f"{what(job.item)}, profil {job.b_profile}, demandé {fmt_ago(now - job.created_at)}"
        err = f'<div class="err">Échec : {escape(job.error or "")}</div>' if job.state == "failed" else ""
        seed_html = (bar("seed", "Seed réel", seed_pct, seed_txt, done=seed_pct is not None and seed_pct >= 100)
                     if job.hashes else bar("seed", "Seed réel", None, seed_txt))
        dim = " dim" if job.deleted_at is not None or job.state == "failed" else ""
        name, tags = split_title(job.release_title)
        chips = "".join(f'<span class="tag">{escape(t)}</span>' for t in tags)
        return (f'<li class="job{dim}"><div><div class="t" title="{escape(job.release_title)}">{escape(name)}</div>'
                f'<div class="meta">{escape(meta)}</div>{err}<div class="line">{pill(job)}{chips}</div></div>'
                f"<div>{download_bar(job)}{transfer_bar(job)}</div>"
                f'<div>{seed_html}<div class="rm">Suppression : <b>{escape(fmt_removal(job, now, cfg, ok))}</b>'
                f'</div></div><div class="acts">{buttons(job, f)}</div></li>')

    def summary(all_jobs) -> str:
        dl = sum(j.state == "pending" and j.deleted_at is None for j in all_jobs)
        tx = sum(active(j) and j.state == "ready" and bool(jobs.transfer.get(j.id)) for j in all_jobs)
        todo = sum(show(j, "pending") for j in all_jobs)
        if not (dl or tx or todo):
            return ("Rien en cours. Les demandes du receveur apparaissent ici dès que son Radarr ou son Sonarr "
                    "récupère une release Availarr.")
        parts = [p for p in (f"{dl} en téléchargement chez toi" if dl else "",
                             f"{tx} en transfert vers le receveur" if tx else "") if p]
        txt = (", ".join(parts) + ". ") if parts else ""
        if todo:
            txt += f"{plural(todo, 'demande')} {'attendent' if todo > 1 else 'attend'} ta décision."
        return txt

    def agent_status(now: float) -> str:
        seen = jobs.agent_seen
        if seen is not None and now - seen < AGENT_ONLINE_S:
            return '<span class="pill p-ok">Agent en ligne</span>'
        txt = "jamais vu depuis le démarrage" if seen is None else f"vu {fmt_ago(now - seen)}"
        return f'<span class="pill p-bad">Agent hors ligne, {txt}</span>'

    def requests_fragment(f: str) -> str:
        f = f if f in FILTERS else "all"
        now, all_jobs = clock(), sorted(jobs.store.all(), key=lambda j: j.created_at, reverse=True)
        banner = (f'<p class="warn">Profils du receveur sans correspondance dans [profiles] : '
                  f'{escape(", ".join(sorted(jobs.unmapped)))}</p>') if jobs.unmapped else ""
        seg = "".join(f'<a href="/?f={k}"{" class=on" if k == f else ""}>{v}'
                      f"<b>{sum(show(j, k) for j in all_jobs)}</b></a>" for k, v in FILTERS.items())
        rows = "".join(row(j, now, f) for j in all_jobs if show(j, f))
        table = ('<div class="card"><div class="cols-h"><span>Titre</span><span>Progression</span>'
                 '<span>Seed et suppression</span><span></span></div>'
                 + (f'<ul class="jobs">{rows}</ul>' if rows else '<p class="empty">Aucune demande dans ce filtre.</p>')
                 + "</div>")
        return (f'<div class="head"><div><h2>Demandes</h2><p class="sub">{escape(summary(all_jobs))}</p>'
                f'<div class="line">{agent_status(now)}</div></div>'
                f'<nav class="seg" aria-label="Filtres">{seg}</nav></div>{banner}{table}')

    def stats_fragment() -> str:
        st = stats.compute(jobs.store.all(), jobs.store.transfers(), clock())
        k = st["kinds"]
        kinds = ", ".join(p for p in (plural(k["movie"], "film") if k["movie"] else "",
                                      plural(k["season"], "saison") if k["season"] else "",
                                      plural(k["episode"], "épisode") if k["episode"] else "") if p)

        def fig(label: str, value: str, note: str) -> str:
            return f'<div class="fig"><span>{label}</span><b>{escape(value)}</b><small>{escape(note)}</small></div>'
        figs = (fig("Livrés au receveur", str(st["delivered"]), kinds or "aucune livraison pour l'instant")
                + fig("Volume envoyé", fmt_bytes(st["sent"]), plural(st["transfers"], "envoi"))
                + fig("Débit moyen", fmt_mo(st["rate_mean"]),
                      fmt_mbit(st["rate_mean"]) if st["rate_mean"] else "pas encore de mesure")
                + fig("Délai médian", fmt_dur(st["lead_median"]), "de la demande à la livraison"))

        peak = max(b for _, b in st["days"])
        cols = "".join(
            f'<div class="col" tabindex="0"><i style="height:{100 * b / (peak or 1):.1f}%"></i>'
            f'<span class="tip">{time.strftime("%d/%m", time.localtime(d))} : {fmt_bytes(b)}</span></div>'
            for d, b in st["days"])
        chart = (f'<div class="card block"><h3>Volume envoyé par jour</h3>'
                 f'<p class="sub" style="margin-top:-8px">{stats.DAYS} derniers jours. '
                 f"Survole une barre pour voir le détail.</p>"
                 f'<div class="chart" role="img" aria-label="Volume envoyé au receveur par jour, sur {stats.DAYS} jours">'
                 f'<div class="grid">{fmt_bytes(peak) if peak else ""}</div>{cols}</div>'
                 f'<div class="axis"><span>{time.strftime("%d/%m", time.localtime(st["days"][0][0]))}</span>'
                 f"<span>aujourd'hui</span></div></div>")

        def facts(title: str, items: list[tuple[str, str]]) -> str:
            rows = "".join(f"<div><dt>{escape(a)}</dt><dd>{escape(b)}</dd></div>" for a, b in items)
            return f'<div class="card"><h3>{title}</h3><dl>{rows}</dl></div>'

        def both(bps):
            return f"{fmt_mo(bps)} ({fmt_mbit(bps)})" if bps else "—"
        three = ('<div class="three">'
                 + facts("Chez toi", [("Demandes reçues", str(st["requests"])),
                                      ("Téléchargées pour le receveur", str(st["downloaded"])),
                                      ("Servies depuis ta bibliothèque", str(st["from_library"])),
                                      ("Temps moyen de téléchargement", fmt_dur(st["dl_mean"])),
                                      ("Échecs", str(st["failed"]))])
                 + facts("Envois au receveur", [("Débit moyen", both(st["rate_mean"])),
                                           ("Meilleur débit", both(st["rate_best"])),
                                           ("Dernier envoi", both(st["rate_last"])),
                                           ("Envois interrompus", str(st["interrupted"])),
                                           ("Limite configurée", f"{cfg.bandwidth_mbps:g} Mbit/s"
                                            if cfg.bandwidth_mbps else "aucune")])
                 + facts("Rétention", [("Gardés", str(st["kept"])), ("Supprimés", str(st["deleted"])),
                                       ("Espace libéré (environ)", fmt_bytes(st["freed"])),
                                       ("Suppression", f"{cfg.keep_days:g} j après livraison"),
                                       ("Seed minimum", f"{cfg.seed_days:g} j")])
                 + "</div>")

        recent = ""
        for t, j in st["recent"]:
            name = split_title(j.release_title)[0] if j else f"Demande {t['job_id']}"
            bps = t["bytes"] / t["seconds"] if t["seconds"] >= 1 else None
            recent += (f"<tr><td>{escape(name)}</td>"
                       f'<td>{time.strftime("%d/%m %H:%M", time.localtime(t["started_at"]))}</td>'
                       f'<td class="r">{fmt_bytes(t["bytes"])}</td><td class="r">{fmt_dur(t["seconds"])}</td>'
                       f'<td class="r">{fmt_mo(bps)}</td><td>{"Complet" if t["complete"] else "Interrompu"}</td></tr>')
        table = ('<div class="card block"><h3>Derniers envois</h3>'
                 + ('<table><tr><th>Titre</th><th>Début</th><th class="r">Taille</th><th class="r">Durée</th>'
                    f'<th class="r">Débit</th><th>État</th></tr>{recent}</table>' if recent else
                    '<p class="sub">Aucun envoi pour l\'instant. Le premier transfert vers le receveur apparaîtra ici.</p>')
                 + "</div>")
        return ('<div class="head"><div><h2>Statistiques</h2><p class="sub">Ce que tu as téléchargé et envoyé pour le receveur. '
                "Le débit moyen des derniers envois sert aussi à estimer le temps restant affiché chez le receveur.</p>"
                f'</div></div><div class="card figs">{figs}</div>{chart}{three}{table}')

    def page(tab: str, inner: str, src: str, every: int) -> str:
        tabs = "".join(f'<a href="{href}"{" aria-current=page" if tab == name else ""}>{name}</a>'
                       for name, href in (("Demandes", "/"), ("Statistiques", "/stats")))
        return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{tab}, Availarr</title><style>{CSS}</style></head><body>"
                f'<header class="top"><div class="in"><a class="brand" href="/">{LOGO}Availarr</a>'
                f'<nav class="tabs" aria-label="Sections">{tabs}</nav>'
                f'<div class="live"><i></i><span>À jour toutes les {every // 1000} s</span></div></div></header>'
                f'<main class="page" id="live" data-src="{src}" data-every="{every}">{inner}</main>'
                f"<script>{JS}</script></body></html>")

    @app.get("/", response_class=HTMLResponse)
    def index(f: str = "all"):
        return page("Demandes", requests_fragment(f), "/fragment", POLL_MS)

    @app.get("/fragment", response_class=HTMLResponse)
    def frag(f: str = "all"):
        return requests_fragment(f)

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page():
        return page("Statistiques", stats_fragment(), "/stats/fragment", STATS_POLL_MS)

    @app.get("/stats/fragment", response_class=HTMLResponse)
    def stats_frag():
        return stats_fragment()

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
