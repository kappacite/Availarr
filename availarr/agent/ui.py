"""The agent's own pages: what its Radarr/Sonarr asked for and where it stands, plus the config page. Basic auth."""
import secrets
import time
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from availarr.agent.config import AgentConfig, load_agent_config
from availarr.agent.db import AgentJob, AgentStore
from availarr.agent.transfer import ETA, SERVER
from availarr.setup import SETUP_CSS, SETUP_JS, config_router
from availarr.ui import CSS, LOGO, fmt_bytes, fmt_eta, fmt_mo

POLL_MS = 4000
SERVER_ONLINE_S = 120
JS = """
const main=document.getElementById('live');
async function tick(){
  if(document.hidden)return;
  try{
    const r=await fetch(main.dataset.src,{cache:'no-store'});
    if(!r.ok)throw 0;
    main.innerHTML=await r.text();document.body.classList.remove('stale');
  }catch(e){document.body.classList.add('stale')}
}
setInterval(tick,+main.dataset.every);
"""
TABS = [("Téléchargements", "/"), ("Configuration", "/config")]
BADGES = {"queued": ("p-dl", "L'envoyeur télécharge"), "transferring": ("p-tx", "Transfert"),
          "completed": ("p-ok", "Terminé"), "cancelled": ("", "Annulé")}


def bar(kind, label, pct, value, extra="", running=False, done=False) -> str:
    w = 0 if pct is None else max(0, min(100, pct))
    cls = f"bar {kind}" + (" run" if running else "") + (" done" if done else "")
    right = escape(value) + (f", {escape(extra)}" if extra else "")
    return (f'<div class="{cls}"><div class="lbl"><span>{escape(label)}</span><b>{right}</b></div>'
            f'<div class="track" role="progressbar" aria-label="{escape(label)}" aria-valuemin="0" aria-valuemax="100" '
            f'aria-valuenow="{round(w)}"><div class="fill" style="width:{w}%"></div></div></div>')


def job_row(j: AgentJob) -> str:
    eta = ETA.get(j.nzo_id)
    left = f"reste {fmt_eta(eta)}" if eta else ""
    if j.state == "failed":
        pill = f'<span class="pill p-bad">{escape(j.fail_message or "Échec")}</span>'
        b = bar("dl", "—", None, "—")
    else:
        cls, text = BADGES[j.state]
        pill = f'<span class="pill {cls}">{escape(text)}</span>'
        if j.state == "queued":
            b = bar("dl", "Chez l'envoyeur", j.percentage, f"{j.percentage} %", left, running=True)
        elif j.state == "transferring":
            b = bar("tx", "Transfert vers toi", j.percentage, f"{j.percentage} %",
                    f"{fmt_bytes(j.done)} / {fmt_bytes(j.size)}" + (f", {left}" if left else ""), running=True)
        elif j.state == "completed":
            b = bar("tx", "Transfert vers toi", 100, "Terminé", done=True)
        else:
            b = bar("dl", "—", None, "Annulé")
    return (f'<li class="job"><div><div class="t">{escape(j.name)}</div><div class="line">{pill}</div></div>'
            f"<div>{b}</div><div></div><div></div></li>")


def fragment(cfg: AgentConfig, store: AgentStore, clock=time.time) -> str:
    jobs = sorted((j for j in store.all() if not j.hidden), key=lambda j: j.created_at, reverse=True)
    now = clock()
    ok = SERVER["ok_at"]
    if ok is not None and now - ok < SERVER_ONLINE_S:
        srv = f"<b>Connecté</b><small>réponse il y a {int(now - ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srv = (f"<b>Injoignable</b><small>{escape(SERVER['error'] or 'pas encore de réponse')}, "
               f"{escape(cfg.server_url)}</small>")
    tx = [j for j in jobs if j.state == "transferring"]
    dl = [j for j in jobs if j.state == "queued"]
    # ponytail: no live rate is tracked here; derived from ETA (= remaining / rate while transferring). Add a
    # real counter in transfer.py if this proves too rough.
    bps = sum((j.size - j.done) / ETA[j.nzo_id] for j in tx if ETA.get(j.nzo_id))
    rows = "".join(job_row(j) for j in jobs) or '<li class="job"><div class="t">Aucun téléchargement pour le moment.</div></li>'
    mbit = f"{bps * 8 / 1e6:.0f} Mbit/s" if bps else "aucun transfert actif"
    return ('<div class="card figs">'
            f'<div class="fig"><span>Server de l\'envoyeur</span>{srv}</div>'
            f'<div class="fig"><span>Cet agent</span><b>{escape(cfg.agent_name)}</b>'
            f'<small class="mono">{escape(cfg.agent_id)}</small></div>'
            f'<div class="fig"><span>En cours</span><b>{len(tx) + len(dl)}</b>'
            f"<small>{len(dl)} chez l'envoyeur, {len(tx)} en transfert</small></div>"
            f'<div class="fig"><span>Débit vers toi</span><b>{fmt_mo(bps)}</b><small>{mbit}</small></div></div>'
            '<div class="card" style="margin-top:20px"><div class="cols-h"><span>Release</span><span>Progression</span>'
            f'<span></span><span></span></div><ul class="jobs">{rows}</ul></div>')


def chrome(tab: str, inner: str, live: bool = False, config: bool = False) -> str:
    tabs = "".join(f'<a href="{h}"{" aria-current=page" if n == tab else ""}>{n}</a>' for n, h in TABS)
    main = (f'<main class="page" id="live" data-src="/fragment" data-every="{POLL_MS}">{inner}</main>' if live
            else f'<main class="page">{inner}</main>')
    sub = '<small style="font:500 13px var(--sans);color:var(--muted)">agent</small>'
    liv = f'<div class="live"><i></i><span>À jour toutes les {POLL_MS // 1000} s</span></div>' if live else ""
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{escape(tab)}, Availarr agent</title><style>{CSS}{SETUP_CSS if config else ''}</style></head>"
            f'<body{" data-test=/config/test" if config else ""}><header class="top"><div class="in">'
            f'<a class="brand" href="/">{LOGO}Availarr {sub}</a>'
            f'<nav class="tabs" aria-label="Sections">{tabs}</nav>{liv}</div></header>{main}'
            f"<script>{JS if live else SETUP_JS}</script></body></html>")


def create_ui(cfg: AgentConfig, store: AgentStore, config_path: str, restart, clock=time.time) -> APIRouter:
    basic = HTTPBasic(auto_error=False)

    def auth(c: HTTPBasicCredentials | None = Depends(basic)) -> None:
        # an empty password refuses everyone rather than leaving the pages open
        if not cfg.admin_password or c is None or not secrets.compare_digest(
                c.password.encode(), cfg.admin_password.encode()):
            raise HTTPException(401, "Mot de passe requis", headers={"WWW-Authenticate": 'Basic realm="Availarr"'})

    r = APIRouter(dependencies=[Depends(auth)])

    @r.get("/", response_class=HTMLResponse)
    def index():
        return chrome("Téléchargements", '<div class="head"><div><h2>Téléchargements</h2><p class="sub">Ce que ton Radarr '
                      "et ton Sonarr ont demandé à l'envoyeur via Availarr.</p></div></div>"
                      + fragment(cfg, store, clock), live=True)

    @r.get("/fragment", response_class=HTMLResponse)
    def frag():
        return fragment(cfg, store, clock)

    r.include_router(config_router("agent", config_path, load_agent_config, restart,
                                   page=lambda t, inner: chrome("Configuration", inner, config=True)))
    return r
