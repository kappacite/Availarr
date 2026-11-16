import difflib
import math
import re
import secrets
import time
from html import escape
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from availarr.server import stats
from availarr.setup import SETUP_CSS, SETUP_JS, same_origin, _restart as default_restart, config_router
from availarr.ui import CSS, LOGO, fmt_bytes, fmt_eta, fmt_mo  # noqa: F401 (re-exported)
from availarr.server.config import ServerConfig, load_server_config
from availarr.server.db import Agent
from availarr.server.jobs import Jobs
from availarr.server.retention import fully_protected

FILTERS = {"all": "Tout", "active": "En cours", "pending": "À décider", "kept": "Gardés", "deleted": "Supprimés"}
ACTIONS = {"keep": "keep", "delete": "delete", "cancel": "none"}
REFUSED = "Refusée par l'envoyeur"
POLL_MS = 4000  # ponytail: each poll asks A's Radarr/Sonarr queue per downloading job; fine for a handful
STATS_POLL_MS = 15000
AGENT_ONLINE_S = 120  # the agent pings every poll_seconds (30 s by default)
BACK_OK = re.compile(r"/|/agents/[^/?#\s]+|/jobs/\d+")


def fmt_date(ts: float) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(ts))


def fmt_dh(seconds: float) -> str:
    d, h = divmod(int(seconds), 86400)
    return f"{d} j {h // 3600:02d} h"


def fmt_seed(seconds: int | None, seed_days: float) -> str:
    if seconds is None:
        return "—"
    d, h = divmod(int(seconds), 86400)
    h //= 3600
    if seconds >= seed_days * 86400:
        return f"{d} j {h:02d} h ✅"
    return f"{d} j {h:02d} h / {seed_days:g} j"


def fmt_seedline(ts, seed_days: float) -> str:
    if any(t.progress < 1 for t in ts):
        return f"↓ {fmt_mo(sum(t.dlspeed for t in ts))} · {sum(t.peers for t in ts)} pairs"
    left = seed_days * 86400 - min(t.seeding or 0 for t in ts)
    d, h = divmod(int(left), 86400)
    reste = f"reste <b>{d} j {h // 3600:02d} h</b> · " if left > 0 else ""
    ratio = f"{min(t.ratio for t in ts):.2f}".replace(".", ",")
    return f"{reste}↑ {fmt_mo(sum(t.upspeed for t in ts))} · ratio {ratio}"


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


def fmt_ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 3600:
        return f"il y a {max(1, s // 60)} min"
    if s < 86400:
        return f"il y a {s // 3600} h"
    return f"il y a {s // 86400} j"


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
if(+main.dataset.every)setInterval(tick,+main.dataset.every);
"""


def create_panel(jobs: Jobs, qbit, cfg: ServerConfig, clock=time.time, config_path=None, restart=None) -> FastAPI:
    basic = HTTPBasic()
    samples: dict[int, tuple[float, int, float]] = {}  # job id -> (monotonic time, bytes, smoothed bytes/s)

    def auth(c: HTTPBasicCredentials = Depends(basic)) -> None:
        if not secrets.compare_digest(c.password.encode(), cfg.admin_password.encode()):
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    app = FastAPI(dependencies=[Depends(auth), Depends(same_origin)])

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
        if job.outcome == "cancelled":
            return bar("dl", label, None, "Annulé chez le receveur")
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

    def torrent_hashes(job) -> list[str]:
        if job.hashes or job.state != "pending":
            return job.hashes
        try:
            return jobs.lib(job.item).queue_hashes(job.item)
        except Exception:  # A's Radarr/Sonarr down: the page must still render
            return []

    def seed(job, info: dict | None = None) -> tuple[str, float | None, bool, str]:
        hashes = torrent_hashes(job)
        if not hashes:
            return "—", None, True, ""
        try:
            ts = list((qbit.info(hashes) if info is None else info).values())
        except Exception:
            return "qBittorrent injoignable", None, False, ""
        secs = [t.seeding for t in ts if t.progress >= 1 and t.seeding is not None]
        line = fmt_seedline(ts, cfg.seed_days) if ts else ""
        ok = all(t.progress >= 1 and (t.seeding is None or t.seeding >= cfg.seed_days * 86400) for t in ts)  # as retention.seed_ok: downloading is not ok
        if not secs:
            return ("torrent absent" if job.hashes and not ts else "—"), None, ok, line
        s = min(secs)
        return fmt_seed(s, cfg.seed_days), 100 * s / (cfg.seed_days * 86400), ok, line

    def buttons(job, f: str, back: str = "/") -> str:
        def form(act, label, cls=""):
            c = f' class="{cls}"' if cls else ""
            return (f'<form method="post" action="/jobs/{job.id}/{act}?f={f}&amp;back={quote(back, safe="")}">'
                    f"<button{c}>{label}</button></form>")
        hide = form("hide", "Retirer", "ghost")
        if job.state == "failed":
            return form("retry", "Réessayer") + hide
        if job.outcome == "cancelled":
            return (form("delete", "Supprimer", "del") if job.deleted_at is None and job.decision == "none" else "") + hide
        if job.deleted_at is not None or job.outcome == "delivered":
            return hide
        if fully_protected(job) and job.decision != "keep":
            base = ""
        elif job.decision == "none":
            base = form("keep", "Garder") + form("delete", "Supprimer", "del")
        else:
            base = form("cancel", "Annuler la décision")
        return base + form("refuse", "Refuser", "ghost" if base else "del")

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
            return (job.deleted_at is None and job.outcome != "cancelled" and job.decision == "none" and not fully_protected(job)
                    and job.state in ("pending", "ready"))
        return True

    def pill(job) -> str:
        if job.error == REFUSED:
            return '<span class="pill p-bad">Refusée</span>'
        if job.outcome == "cancelled":
            return '<span class="pill">Annulé chez le receveur</span>'
        if job.deleted_at is not None:
            return '<span class="pill">Supprimé</span>'
        if job.state == "failed":
            return '<span class="pill p-bad">Échec</span>'
        if job.outcome == "delivered":
            return '<span class="pill p-ok">Livré</span>'
        if job.state == "pending":
            return '<span class="pill p-dl">Téléchargement</span>'
        if jobs.transfer.get(job.id):
            return '<span class="pill p-tx">Transfert</span>'
        if fully_protected(job) and not job.hashes:
            return '<span class="pill p-lib">Dans ta bibliothèque</span>'
        return '<span class="pill p-tx">Prêt à envoyer</span>'

    def row(job, now: float, f: str, back: str) -> str:
        if job.outcome == "cancelled":
            seed_txt, seed_pct, ok, line = "figé, demande annulée", None, True, ""
        else:
            seed_txt, seed_pct, ok, line = seed(job)
        meta = f"{what(job.item)}, profil {job.b_profile}, demandé {fmt_ago(now - job.created_at)}"
        err = f'<div class="err">Échec : {escape(job.error or "")}</div>' if job.state == "failed" else ""
        seed_html = (bar("seed", "Seed réel", seed_pct, seed_txt, done=seed_pct is not None and seed_pct >= 100)
                     if seed_pct is not None else bar("seed", "Seed réel", None, seed_txt))
        if line:
            seed_html += f'<div class="seedline">{line}</div>'
        dim = " dim" if job.deleted_at is not None or job.state == "failed" or job.outcome == "cancelled" else ""
        name, tags = split_title(job.release_title)
        chips = "".join(f'<span class="tag">{escape(t)}</span>' for t in tags)
        return (f'<li class="job{dim}"><div><div class="t" title="{escape(job.release_title)}">'
                f'<a href="/jobs/{job.id}">{escape(name)}</a></div>'
                f'<div class="meta">{escape(meta)}</div>{err}<div class="line">{pill(job)}{chips}</div></div>'
                f"<div>{download_bar(job)}{transfer_bar(job)}</div>"
                f'<div>{seed_html}<div class="rm">Suppression : <b>{escape(fmt_removal(job, now, cfg, ok))}</b>'
                f'</div></div><div class="acts">{buttons(job, f, back)}</div></li>')

    def summary(all_jobs) -> str:
        dl = sum(active(j) and j.state == "pending" for j in all_jobs)
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

    def agent_list() -> list[Agent]:
        known = {a.id: a for a in jobs.store.agents()}
        for j in jobs.store.all():  # an agent that never pinged (the pre-multi-agent "legacy") still has its jobs listed
            known.setdefault(j.agent, Agent(j.agent, "Agent" if j.agent == "legacy" else j.agent, 0, None, False, False))
        return list(known.values())

    def find_agent(id: str) -> Agent:
        a = next((a for a in agent_list() if a.id == id), None)
        if a is None:
            raise HTTPException(404)
        return a

    def online(a: Agent, now: float) -> bool:
        return a.last_seen is not None and now - a.last_seen < AGENT_ONLINE_S

    def agent_pill(a: Agent, now: float) -> str:
        if a.blocked:
            return '<span class="pill">Bloqué</span>'
        if online(a, now):
            return '<span class="pill p-ok">En ligne</span>'
        ago = f", vu {fmt_ago(now - a.last_seen)}" if a.last_seen is not None else ", jamais vu"
        return f'<span class="pill p-bad">Hors ligne{ago}</span>'

    def visible(agent: str | None = None) -> list:
        return sorted((j for j in jobs.store.all() if j.hidden_at is None and agent in (None, j.agent)),
                      key=lambda j: j.created_at, reverse=True)

    def unmapped_banner() -> str:
        return (f'<p class="warn">Profils du receveur sans correspondance dans [profiles] : '
                f'{escape(", ".join(sorted(jobs.unmapped)))}</p>') if jobs.unmapped else ""

    def agent_card(a: Agent, mine: list, now: float) -> str:
        cls = " blocked" if a.blocked else "" if online(a, now) else " off"
        nums = "".join(f"<div><b>{v}</b><span>{k}</span></div>" for k, v in (
            ("En cours", sum(active(j) for j in mine)), ("À décider", sum(show(j, "pending") for j in mine)),
            ("Livrés", sum(j.outcome == "delivered" for j in mine))))
        if mine:
            name = split_title(mine[0].release_title)[0]
            last = f"Dernière demande : <b>{escape(name)}</b>, {fmt_ago(now - mine[0].created_at)}"
        else:
            last = "Aucune demande pour l'instant"
        short = a.id if len(a.id) <= 13 else f"{a.id[:4]}…{a.id[-4:]}"
        return (f'<a class="card agent{cls}" href="/agents/{quote(a.id, safe="")}">'
                f'<div class="nm"><h3>{escape(a.name)}</h3>{agent_pill(a, now)}</div>'
                f'<div class="id">agent {escape(short)}</div><div class="nums">{nums}</div>'
                f'<div class="last">{last}</div></a>')

    def agents_fragment() -> str:
        now = clock()
        all_jobs = visible()
        order = sorted(agent_list(), key=lambda a: (not online(a, now), -(a.last_seen or 0)))
        cards = "".join(agent_card(a, [j for j in all_jobs if j.agent == a.id], now) for a in order)
        body = (f'<div class="agents">{cards}</div>' if cards else
                '<p class="empty">Aucun agent pour l\'instant.</p>')
        return (f'<div class="head"><div><h2>Agents</h2><p class="sub">{escape(summary(all_jobs))}</p></div></div>'
                f'{unmapped_banner()}{body}'
                '<p class="note">Un agent apparaît ici à sa première connexion avec le jeton partagé. '
                "Son nom vient de sa configuration ; tu peux le renommer ou le bloquer depuis sa page.</p>")

    def agent_fragment(id: str, f: str) -> str:
        a = find_agent(id)
        f = f if f in FILTERS else "all"
        now = clock()
        mine = visible(a.id)
        back = f"/agents/{quote(a.id, safe='')}"
        seg = "".join(f'<a href="{back}?f={k}"{" class=on" if k == f else ""}>{v}'
                      f"<b>{sum(show(j, k) for j in mine)}</b></a>" for k, v in FILTERS.items())
        rows = "".join(row(j, now, f, back) for j in mine if show(j, f))
        table = ('<div class="card"><div class="cols-h"><span>Titre</span><span>Progression</span>'
                 '<span>Seed et suppression</span><span></span></div>'
                 + (f'<ul class="jobs">{rows}</ul>' if rows else '<p class="empty">Aucune demande dans ce filtre.</p>')
                 + "</div>")
        toggle = (f'<form method="post" action="{back}/unblock"><button>Débloquer</button></form>' if a.blocked else
                  f'<form method="post" action="{back}/block"><button class="del">Bloquer</button></form>')
        rename = (f'<form class="rename" method="post" action="{back}/rename"><input name="name" required '
                  f'value="{escape(a.name)}" aria-label="Nom de l\'agent"><button>Renommer</button></form>')
        return (f'<div class="crumb"><a href="/">Agents</a> › {escape(a.name)}</div>'
                f'<div class="head"><div><h2>{escape(a.name)}</h2><p class="sub">{escape(summary(mine))}</p>'
                f'<div class="line">{agent_pill(a, now)}<span class="tag mono">{escape(a.id)}</span>{rename}{toggle}</div></div>'
                f'<nav class="seg" aria-label="Filtres">{seg}</nav></div>{unmapped_banner()}{table}'
                '<p class="note">Clique sur un titre pour ouvrir le détail de la demande.</p>')

    def steps(job, now: float, seed_ok: bool) -> str:
        live = job.deleted_at is None and job.outcome is None
        items = [("ok", "Demandée par le receveur", f"profil {job.b_profile}", fmt_date(job.created_at))]
        if job.state == "failed":
            items.append(("", "Échec", job.error or "", "—"))
        else:
            dl_now = live and job.state == "pending"
            items.append(("ok" if job.ready_at is not None else "now" if dl_now else "", "Téléchargée chez toi",
                          "torrent terminé" if job.ready_at is not None else "",
                          fmt_date(job.ready_at) if job.ready_at is not None else "en cours" if dl_now else "—"))
            tx_now = live and job.state == "ready"
            items.append(("ok" if job.outcome == "delivered" else "now" if tx_now else "", "Transfert vers le receveur",
                          "", "en cours" if tx_now else "—"))
            if job.outcome:
                items.append(("ok", "Livrée" if job.outcome == "delivered" else "Annulée chez le receveur", "",
                              fmt_date(job.closed_at) if job.closed_at else "—"))
            else:
                items.append(("", "Livrée", "le receveur confirme l'import", "—"))
        if job.deleted_at is not None:
            items.append(("ok", "Supprimée chez toi", "", fmt_date(job.deleted_at)))
        else:
            items.append(("", "Suppression chez toi", fmt_removal(job, now, cfg, seed_ok), "—"))
        return "".join(f'<li class="{c}"><i></i><div>{escape(t)}<br><small>{escape(d)}</small></div>'
                       f'<span class="when">{escape(w)}</span></li>' for c, t, d, w in items)

    def torrents_card(job) -> tuple[str, bool]:
        head = "<h3>Torrent chez toi</h3>"
        if job.outcome == "cancelled":
            return f'<div class="card">{head}<p class="note">Demande annulée : données figées, torrent non interrogé.</p></div>', True
        hashes = torrent_hashes(job)
        link = (f'<p class="note"><a href="/jobs/{job.id}/link">Lier un torrent</a> si le bon n’est pas trouvé '
                f"(seed affiché ; supprimé avec la demande si elle l'est un jour).</p>" if job.deleted_at is None else "")
        if not hashes:
            return f'<div class="card">{head}<p class="note">Aucun torrent pour cette demande.</p>{link}</div>', True
        try:
            ts = qbit.info(hashes)
        except Exception:
            return f'<div class="card">{head}<p class="note">qBittorrent injoignable</p></div>', False
        rows = ""
        for h in hashes:
            t = ts.get(h)
            short = f'<small style="color:var(--faint)">{escape(h[:4])}…{escape(h[-4:])}</small>'
            if t is None:
                rows += f'<tr><td class="mono">{short}</td><td>Absent</td>' + '<td class="r">—</td>' * 4 + "</tr>"
                continue
            done = t.progress >= 1 and t.seeding is not None
            left = cfg.seed_days * 86400 - (t.seeding or 0)
            rows += (f'<tr><td class="mono">{escape(t.name)} {short}</td>'
                     f'<td>{"En seed" if t.progress >= 1 else "Téléchargement"}</td>'
                     f'<td class="r">{fmt_dh(t.seeding) if done else "—"}</td>'
                     f'<td class="r">{fmt_dh(left) if done and left > 0 else "—"}</td>'
                     f'<td class="r">{fmt_mo(t.upspeed) if t.progress >= 1 else "↓ " + fmt_mo(t.dlspeed)}</td>'
                     f'<td class="r">{f"{t.ratio:.2f}".replace(".", ",")}</td></tr>')
        txt, pct, ok, _ = seed(job, ts)
        linked = "".join(
            f'<form method="post" action="/jobs/{job.id}/torrent"><input type="hidden" name="action" value="unlink">'
            f'<input type="hidden" name="hash" value="{escape(h)}"><button class="ghost">Délier {escape(h[:4])}…</button></form>'
            for h in job.hashes) if job.deleted_at is None else ""
        return (f'<div class="card">{head}<table><tr><th>Torrent</th><th>État</th><th class="r">Seed</th>'
                f'<th class="r">Reste</th><th class="r">Envoi</th><th class="r">Ratio</th></tr>{rows}</table>'
                f'<div style="margin-top:14px">{bar("seed", "Seed réel", pct, txt, done=pct is not None and pct >= 100)}</div>'
                f'<div class="acts" style="justify-content:flex-start;margin-top:12px">{linked}</div>{link}</div>', ok)

    def files_card(job) -> str:
        files = []
        if job.state == "ready" and job.deleted_at is None and job.outcome != "cancelled":
            try:
                files = jobs.status(job)["files"]
            except Exception:  # A's Radarr/Sonarr down: the page must still render
                files = []
        rows = "".join(f'<tr><td class="mono">{escape(f["name"])}</td><td class="r">{fmt_bytes(f["size"])}</td></tr>'
                       for f in files)
        return ('<div class="card"><h3>Fichiers servis</h3>'
                + (f'<table><tr><th>Fichier</th><th class="r">Taille</th></tr>{rows}</table>' if rows else
                   '<p class="note">Aucun fichier à servir pour l\'instant.</p>') + "</div>")

    def sends_card(job) -> str:
        ts = [t for t in jobs.store.transfers() if t["job_id"] == job.id]
        if not ts:
            return '<div class="card"><h3>Envois</h3><p class="note">Aucun envoi pour l\'instant.</p></div>'
        secs = sum(t["seconds"] for t in ts)
        rate = sum(t["bytes"] for t in ts) / secs if secs >= 1 else None
        rows = "".join(
            f'<tr><td>{fmt_date(t["started_at"])}</td><td class="r">{fmt_bytes(t["bytes"])}</td>'
            f'<td class="r">{fmt_dur(t["seconds"])}</td>'
            f'<td class="r">{fmt_mo(t["bytes"] / t["seconds"] if t["seconds"] >= 1 else None)}</td>'
            f'<td>{"Complet" if t["complete"] else "Interrompu"}</td></tr>' for t in ts)
        return ('<div class="card"><h3>Envois</h3><dl>'
                f'<div><dt>Tentatives</dt><dd>{len(ts)}</dd></div>'
                f'<div><dt>Interrompus</dt><dd>{sum(not t["complete"] for t in ts)}</dd></div>'
                f'<div><dt>Débit moyen</dt><dd>{fmt_mo(rate)}</dd></div></dl>'
                '<table style="margin-top:12px"><tr><th>Début</th><th class="r">Taille</th><th class="r">Durée</th>'
                f'<th class="r">Débit</th><th>État</th></tr>{rows}</table></div>')

    def job_fragment(id: int) -> str:
        job = jobs.store.get(id)
        if job is None:
            raise HTTPException(404)
        a = find_agent(job.agent)
        now = clock()
        back = f"/agents/{quote(a.id, safe='')}"
        name, tags = split_title(job.release_title)
        chips = "".join(f'<span class="tag">{escape(t)}</span>' for t in tags)
        torrents, seed_ok = torrents_card(job)
        acts = buttons(job, "all", f"/jobs/{job.id}")
        note = "<p>Refuser : la demande échoue chez le receveur, qui cherche ailleurs.</p>" if "/refuse?" in acts else ""
        req = jobs.cfg.profiles.get(job.b_profile) or jobs.cfg.profiles.get("default")
        kind = what(job.item) + (f" ({plural(len(job.units), 'épisode')})" if job.item.kind == "season" else "")
        facts = [("Agent", f'<a href="{back}">{escape(a.name)}</a>'), ("Type", escape(kind)),
                 ("Profil du receveur", escape(job.b_profile)),
                 ("Profil chez toi", escape(req.a_profiles[0]) if req else "non mappé"),
                 ("Exigence", escape(f"{req.min_resolution}p minimum, {', '.join(req.languages) or 'toute langue'}"
                         + (f" (ou sous-titres {', '.join(req.subtitles)})" if req.subtitles else ""))
                  if req else "—"),
                 ("Release annoncée", f'<span class="mono">{escape(job.release_title)}</span>'),
                 ("Ajoutée par Availarr", "oui, l'entrée sera retirée à la suppression" if job.added_by_tool else "non"),
                 ("Demande n°", str(job.id))]
        dl = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)
        eps = ""
        if job.item.kind != "movie":
            eps = ('<div class="card"><h3>Épisodes</h3><dl>'
                   f'<div><dt>Demandés</dt><dd>{len(job.units)}</dd></div>'
                   f'<div><dt>Déjà chez toi avant</dt><dd>{len(job.protected_units)}</dd></div>'
                   f'<div><dt>Protégés 🔒</dt><dd>{len(job.protected_units) or "aucun"}</dd></div></dl></div>')
        return (f'<div class="crumb"><a href="/">Agents</a> › <a href="{back}">{escape(a.name)}</a> › {escape(name)}</div>'
                f'<div class="head"><div><h2>{escape(name)}</h2><div class="line">{pill(job)}{chips}</div></div>'
                f'<div class="big-acts">{acts}{note}</div></div>'
                '<div class="detail"><div>'
                f'<div class="card"><h3>Progression</h3>{download_bar(job)}{transfer_bar(job)}'
                f'<ul class="steps" style="margin-top:18px">{steps(job, now, seed_ok)}</ul></div>'
                f"{torrents}{files_card(job)}{sends_card(job)}</div>"
                f'<div class="side"><div class="card"><h3>Demande</h3><dl>{dl}</dl></div>{eps}</div></div>')

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

    def tabs_html(tab: str) -> str:
        return "".join(f'<a href="{href}"{" aria-current=page" if tab == name else ""}>{name}</a>'
                       for name, href in (("Agents", "/"), ("Statistiques", "/stats"))
                       + ((("Configuration", "/config"),) if config_path else ()))

    def page(tab: str, inner: str, src: str, every: int, title: str | None = None) -> str:
        tabs = tabs_html(tab)
        return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{escape(title or tab)}, Availarr</title><style>{CSS}</style></head><body>"
                f'<header class="top"><div class="in"><a class="brand" href="/">{LOGO}Availarr</a>'
                f'<nav class="tabs" aria-label="Sections">{tabs}</nav>'
                f'<div class="live"><i></i><span>À jour toutes les {every // 1000} s</span></div></div></header>'
                f'<main class="page" id="live" data-src="{src}" data-every="{every}">{inner}</main>'
                f"<script>{JS}</script></body></html>")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return page("Agents", agents_fragment(), "/fragment", POLL_MS)

    @app.get("/fragment", response_class=HTMLResponse)
    def frag():
        return agents_fragment()

    @app.get("/agents/{id}", response_class=HTMLResponse)
    def agent_page(id: str, f: str = "all"):
        return page("Agents", agent_fragment(id, f), f"/agents/{quote(id, safe='')}/fragment", POLL_MS,
                    find_agent(id).name)

    @app.get("/agents/{id}/fragment", response_class=HTMLResponse)
    def agent_frag(id: str, f: str = "all"):
        return agent_fragment(id, f)

    @app.post("/agents/{id}/{action}")
    def agent_act(id: str, action: str, name: str = Form("")):
        a = find_agent(id)
        if jobs.store.agent(id) is None:  # jobs-only agent (legacy never seen): give it a row so the change sticks
            jobs.store.ensure_agent(id, a.name)
        if action == "rename":
            if name.strip():
                jobs.store.set_agent(id, name=name.strip(), renamed=1)
        elif action in ("block", "unblock"):
            jobs.store.set_agent(id, blocked=int(action == "block"))
        else:
            raise HTTPException(404)
        return RedirectResponse(f"/agents/{quote(id, safe='')}", 303)

    @app.get("/jobs/{id}", response_class=HTMLResponse)
    def job_page(id: int):
        inner = job_fragment(id)  # 404s on an unknown job
        return page("Agents", inner, f"/jobs/{id}/fragment", POLL_MS, split_title(jobs.store.get(id).release_title)[0])

    @app.get("/jobs/{id}/fragment", response_class=HTMLResponse)
    def job_frag(id: int):
        return job_fragment(id)

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page():
        return page("Statistiques", stats_fragment(), "/stats/fragment", STATS_POLL_MS)

    @app.get("/stats/fragment", response_class=HTMLResponse)
    def stats_frag():
        return stats_fragment()

    def norm(t: str) -> str:
        return re.sub(r"[._\-\[\]()]+", " ", t).lower().strip()

    @app.get("/jobs/{id}/link", response_class=HTMLResponse)
    def link_page(id: int):
        job = jobs.store.get(id)
        if job is None:
            raise HTTPException(404)
        name = split_title(job.release_title)[0]
        try:
            ts = [t for t in qbit.all() if t.hash not in job.hashes]
        except Exception:
            ts = None
        if ts is None:
            body = '<p class="warn">qBittorrent injoignable.</p>'
        else:
            # ponytail: plain similarity on names, top 30; a search box if lists get long
            want = norm(name)
            ts.sort(key=lambda t: difflib.SequenceMatcher(None, want, norm(t.name)).ratio(), reverse=True)
            rows = "".join(
                f'<tr><td class="mono">{escape(t.name)}</td><td>{"En seed" if t.progress >= 1 else f"{t.progress:.0%}"}</td>'
                f'<td class="r"><form method="post" action="/jobs/{id}/torrent"><input type="hidden" name="action" value="link">'
                f'<input type="hidden" name="hash" value="{escape(t.hash)}"><button>Lier</button></form></td></tr>'
                for t in ts[:30])
            body = (f"<div class=\"card block\"><table><tr><th>Torrent</th><th>État</th><th></th></tr>{rows}</table></div>"
                    if rows else '<p class="empty">Aucun torrent dans qBittorrent.</p>')
        inner = (f'<div class="crumb"><a href="/jobs/{id}">{escape(name)}</a> › Lier un torrent</div>'
                 f'<div class="head"><div><h2>Lier un torrent</h2><p class="sub">Choisis le torrent de qBittorrent qui '
                 f"correspond à cette demande. Si elle attend encore, ses fichiers sont envoyés au receveur. Son seed "
                 f"s'affiche sur la demande ; si la demande est supprimée un jour, "
                 f"ce torrent l'est aussi. Les plus ressemblants au titre sont en haut.</p></div></div>{body}")
        return page("Agents", inner, f"/jobs/{id}/link", 0, "Lier un torrent")

    @app.post("/jobs/{id}/torrent")
    async def torrent(id: int, request: Request):
        job = jobs.store.get(id)
        if job is None:
            raise HTTPException(404)
        form = await request.form()
        action, h = form.get("action"), str(form.get("hash", "")).strip().lower()
        if action not in ("link", "unlink") or not re.fullmatch(r"[0-9a-f]{40}", h):
            raise HTTPException(400)
        if job.deleted_at is not None:
            raise HTTPException(409)
        if action == "link":
            if h not in {t.hash for t in qbit.all()}:
                raise HTTPException(400, "torrent inconnu de qBittorrent")
            hashes = list(dict.fromkeys(job.hashes + [h]))
        else:
            hashes = [x for x in job.hashes if x != h]
        jobs.link(job, hashes)
        return RedirectResponse(f"/jobs/{id}", 303)

    @app.post("/jobs/{id}/{action}")
    def act(id: int, action: str, f: str = "all", back: str = "/"):
        job = jobs.store.get(id)
        if job is None or action not in (*ACTIONS, "retry", "refuse", "hide"):
            raise HTTPException(404)
        if action in ("refuse", "hide"):
            try:
                jobs.refuse(job, qbit) if action == "refuse" else jobs.hide(job)
            except ValueError:
                raise HTTPException(409)
        elif action == "retry":
            if job.state != "failed":
                raise HTTPException(409)
            jobs.store.set(id, state="retried")
        else:
            jobs.store.set(id, decision=ACTIONS[action])
        return RedirectResponse(f"{back if BACK_OK.fullmatch(back) else '/'}?f={f if f in FILTERS else 'all'}", 303)

    if config_path:
        def config_page(title: str, inner: str) -> str:
            return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    f"<title>{escape(title)}, Availarr</title><style>{CSS}{SETUP_CSS}</style></head>"
                    '<body data-test="/config/test"><header class="top"><div class="in">'
                    f'<a class="brand" href="/">{LOGO}Availarr</a><nav class="tabs" aria-label="Sections">'
                    f'{tabs_html("Configuration")}</nav></div></header><main class="page">{inner}</main>'
                    f"<script>{SETUP_JS}</script></body></html>")
        app.include_router(config_router("server", config_path, load_server_config, restart or default_restart,
                                         config_page))

    return app
