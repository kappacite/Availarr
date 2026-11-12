"""First-run web setup: when the config file is missing, serve a form that writes it, then hand over."""
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from html import escape

import httpx
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from availarr.ui import CSS, LOGO

log = logging.getLogger(__name__)

SETUP_CSS = """
.page{max-width:880px}
fieldset{border:0;margin:0 0 20px;padding:22px 24px}
legend{float:left;width:100%;padding:0;margin:0 0 4px;font:600 19px/1.2 var(--serif)}
legend+*{clear:both}
.hint{color:var(--muted);font-size:13px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 18px}
.grid .wide{grid-column:1/-1}
label{display:block;font-size:13px;color:var(--muted)}
label small{color:var(--faint)}
input,select{display:block;width:100%;margin-top:5px;font:14px var(--sans);color:var(--text);background:#121a2b;
border:1px solid var(--line);border-radius:8px;padding:8px 10px}
input:focus{border-color:var(--tx);outline:none}
input[readonly]{color:var(--ivory)}
.test{display:flex;align-items:center;gap:12px;margin-top:14px;font-size:13px}
.test output{color:var(--muted)}
.test output.ok{color:var(--ok)}.test output.ko{color:var(--bad)}
.prow{display:grid;grid-template-columns:1.2fr 1.2fr .7fr 1fr 1.2fr auto;gap:10px;align-items:end;margin-top:10px}
.prow button{margin-bottom:1px}
.actions{display:flex;justify-content:flex-end;gap:12px;align-items:center}
button.primary{background:var(--ivory);color:#1b2130;border-color:var(--ivory);font-weight:600;padding:9px 18px}
a.btn{display:inline-block;margin-top:4px;background:var(--ivory);color:#1b2130;text-decoration:none;
font-weight:600;padding:8px 16px;border-radius:8px}
p.ok{color:var(--ok);margin:0}
.error{background:rgba(226,101,111,.12);border:1px solid rgba(226,101,111,.5);color:#f6c2c6;padding:10px 14px;
border-radius:10px;margin:0 0 18px}
@media (max-width:720px){.grid,.prow{grid-template-columns:1fr}}
"""

SETUP_JS = """
const code=()=>document.querySelector('[name=setup_code]').value;
async function test(btn){
  const box=btn.closest('fieldset'),out=box.querySelector('output'),f={kind:btn.dataset.kind,code:code()};
  box.querySelectorAll('[data-k]').forEach(i=>f[i.dataset.k]=i.value);
  out.className='';out.textContent='Test en cours…';
  try{
    const r=await (await fetch('/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(f)})).json();
    out.className=r.ok?'ok':'ko';out.textContent=r.msg;
    if(r.profiles){const d=document.getElementById('profiles-'+f.kind);d.innerHTML=r.profiles.map(p=>'<option value="'+p.replace(/"/g,'&quot;')+'">').join('')}
    if(r.roots){const d=document.getElementById('roots-'+f.kind);d.innerHTML=r.roots.map(p=>'<option value="'+p.replace(/"/g,'&quot;')+'">').join('')}
  }catch(e){out.className='ko';out.textContent='Pas de réponse du service de configuration.'}
}
async function ts(){
  try{const r=await fetch('/tailscale?code='+encodeURIComponent(code()));if(r.ok)document.getElementById('ts').innerHTML=await r.text()}catch(e){}
}
setInterval(ts,3000);document.addEventListener('input',e=>{if(e.target.name==='setup_code')ts()});
function addRow(){
  const t=document.getElementById('prow'),c=t.content.cloneNode(true);
  document.getElementById('prows').appendChild(c);
}
"""


def tailscale_ip() -> str | None:
    """This host's Tailscale IPv4 (100.64.0.0/10), if Tailscale runs in our network namespace."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("100.100.100.100", 53))  # no packet is sent: this only asks the kernel for the route
            ip = s.getsockname()[0]
    except OSError:
        return None
    return ip if ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10") else None


TS_SOCKET = os.environ.get("AVAILARR_TS_SOCKET", "/var/run/tailscale/tailscaled.sock")


def tailscale_status(sock: str = TS_SOCKET) -> dict | None:
    """Read-only status from the Tailscale sidecar's local API (shared socket); None when there is no sidecar."""
    if not os.path.exists(sock):
        return None
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=sock), timeout=5) as c:
            r = c.get("http://local-tailscaled.sock/localapi/v0/status")
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPError, ValueError):
        return {"BackendState": "Unreachable"}


def netns_lost(st: dict | None, ip: str | None) -> bool:
    """Tailscale runs but this container has no tailnet address: the sidecar restarted and left us on its old network."""
    return bool(st) and st.get("BackendState") == "Running" and ip is None


def watch_tailscale(every: float = 30) -> None:
    """Exit when the Tailscale sidecar's network is lost, so Docker restarts us on the sidecar's current one."""
    def loop():
        misses = 0
        while True:
            time.sleep(every)
            misses = misses + 1 if netns_lost(tailscale_status(), tailscale_ip()) else 0
            if misses >= 2:
                logging.error("Réseau Tailscale perdu (le conteneur tailscale a redémarré) : arrêt pour être relancé")
                os._exit(1)
    threading.Thread(target=loop, daemon=True).start()


def tailscale_view(st: dict | None, kind: str, show_url: bool) -> str:
    """What the setup page says about Tailscale, including the login link once the setup code is typed."""
    if st is None:
        return ("<p class=\"hint\">Tailscale n'est pas utilisé par ce conteneur. Sans lui, l'agent doit joindre le "
                "server autrement (voir le README, « Réseau »).</p>")
    state = st.get("BackendState", "")
    if state == "Running":
        ips = (st.get("Self") or {}).get("TailscaleIPs") or []
        ip = next((i for i in ips if "." in i), "")
        net = (st.get("CurrentTailnet") or {}).get("Name", "")
        extra = (f" Adresse de ce server à donner à l'agent : <b>http://{escape(ip)}:8787</b>."
                 if kind == "server" and ip else "")
        return (f'<p class="ok">Connecté à Tailscale{f" ({escape(net)})" if net else ""}, '
                f"adresse {escape(ip)}.{extra}</p>")
    if state == "NeedsMachineAuth":
        return ("<p class=\"hint\">Connexion faite. La machine attend ton approbation dans l'admin Tailscale "
                "(onglet Machines).</p>")
    url = st.get("AuthURL", "")
    if state == "NeedsLogin" and url:
        if not show_url:
            return '<p class="hint">Saisis le code de configuration ci-dessus pour afficher le lien de connexion.</p>'
        link = f'<a class="btn" href="{escape(url)}" target="_blank" rel="noopener">Connecter à Tailscale</a>'
        if kind == "server":
            return (f'<p class="hint">Connecte-toi avec ton compte Tailscale : cette machine rejoint ton réseau.</p>'
                    f"{link}")
        return ("<p class=\"hint\">Envoie ce lien à A. Il l'ouvre dans son navigateur, se connecte à son compte "
                "Tailscale et accepte : l'agent rejoint son réseau, sans compte de ton côté.</p>"
                f'<input readonly value="{escape(url)}" onclick="this.select()"><p>{link}</p>')
    if state == "Unreachable":
        return '<p class="hint">Tailscale ne répond pas encore, nouvel essai dans quelques secondes.</p>'
    return f'<p class="hint">Tailscale démarre ({escape(state or "état inconnu")}).</p>'


def toml_dump(d: dict) -> str:
    """Just enough TOML for our configs: scalars, string lists, [table] and [table."sub"]."""
    def key(k):
        return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k, ensure_ascii=False)

    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return json.dumps(str(v), ensure_ascii=False)

    out = [f"{key(k)} = {val(v)}" for k, v in d.items() if not isinstance(v, dict)]
    for name, table in d.items():
        if not isinstance(table, dict):
            continue
        if table and all(isinstance(v, dict) for v in table.values()):
            for sub, t in table.items():
                out += ["", f"[{key(name)}.{key(sub)}]"] + [f"{key(k)} = {val(v)}" for k, v in t.items()]
        else:
            out += ["", f"[{key(name)}]"] + [f"{key(k)} = {val(v)}" for k, v in table.items()]
    return "\n".join(out) + "\n"


def check(kind: str, f: dict) -> dict:
    """Connection test behind the "Tester" buttons; also returns profile and root folder names to pick from."""
    url = (f.get("url") or "").rstrip("/")
    if not re.match(r"https?://", url):
        return {"ok": False, "msg": "L'adresse doit commencer par http:// ou https://"}
    try:
        with httpx.Client(timeout=10) as c:
            if kind in ("radarr", "sonarr", "radarr_b", "sonarr_b"):
                h = {"X-Api-Key": f.get("key", "")}
                r = c.get(f"{url}/api/v3/system/status", headers=h)
                if r.status_code == 401:
                    return {"ok": False, "msg": "Clé API refusée"}
                r.raise_for_status()
                st = r.json()
                res = {"ok": True, "msg": f"{st.get('appName', kind.title())} {st.get('version', '')} joint"}
                if kind in ("radarr", "sonarr"):
                    res["profiles"] = [p["name"] for p in c.get(f"{url}/api/v3/qualityprofile", headers=h).json()]
                    res["roots"] = [p["path"] for p in c.get(f"{url}/api/v3/rootfolder", headers=h).json()]
                return res
            if kind == "qbittorrent":
                r = c.post(f"{url}/api/v2/auth/login", data={"username": f.get("user", ""),
                                                             "password": f.get("password", "")})
                if r.text.strip() != "Ok.":
                    return {"ok": False, "msg": "Identifiants refusés par qBittorrent"}
                v = c.get(f"{url}/api/v2/app/version").text
                return {"ok": True, "msg": f"qBittorrent {v} joint"}
            if kind == "server":
                r = c.get(f"{url}/v1/ping", headers={"Authorization": f"Bearer {f.get('token', '')}"})
                if r.status_code == 401:
                    return {"ok": False, "msg": "Le server répond, mais refuse ce jeton"}
                r.raise_for_status()
                return {"ok": True, "msg": "Server joint, jeton accepté"}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        return {"ok": False, "msg": f"Échec : {e}"}
    return {"ok": False, "msg": "Test inconnu"}


def _field(label, name, value="", kind="text", hint="", wide=False, required=True, test_key=None, listid=None):
    attrs = (' required' if required else "") + (f' data-k="{test_key}"' if test_key else "") \
        + (f' list="{listid}"' if listid else "") + (" readonly" if kind == "readonly" else "")
    t = "text" if kind == "readonly" else kind
    return (f'<label{" class=wide" if wide else ""}>{label}{f" <small>{hint}</small>" if hint else ""}'
            f'<input type="{t}" name="{name}" value="{escape(str(value))}"{attrs}></label>')


def _test(kind):
    return (f'<div class="test"><button type="button" data-kind="{kind}" onclick="test(this)">Tester</button>'
            "<output></output></div>")


def _section(title, hint, body, test=None):
    return (f'<fieldset class="card"><legend>{title}</legend><p class="hint">{hint}</p>'
            f'<div class="grid">{body}</div>{_test(test) if test else ""}</fieldset>')


def server_form(v: dict) -> str:
    g = v.get
    rows = v.get("profiles") or [{"name": "default", "a": "", "res": "1080", "lang": "", "announce": "1080p.WEB-DL"}]
    prow = ('<div class="prow">' + _field("Profil chez B", "p_name", "{name}") + _field("Profil chez toi", "p_a", "{a}",
            listid="profiles-radarr") + _field("Résolution min.", "p_res", "{res}", "number")
            + _field("Langues", "p_lang", "{lang}", required=False, hint="ex. French")
            + _field("Qualité annoncée", "p_announce", "{announce}")
            + '<button type="button" onclick="this.parentNode.remove()">Retirer</button></div>')
    rows_html = "".join(prow.format(**{k: escape(str(x)) for k, x in r.items()}) for r in rows)
    template = prow.format(name="", a="", res="1080", lang="", announce="1080p.WEB-DL")
    ts = tailscale_ip()
    where = (f"Adresse de ce server à donner à l'agent : <b>http://{ts}:8787</b> (Tailscale)." if ts else
             "Tailscale n'est pas détecté ici : l'agent devra joindre ce server autrement (voir le README).")
    return (
        _section("Accès", "Le jeton relie l'agent de B à ce server : recopie-le dans la configuration de "
                          f"l'agent. {where}",
                 _field("Jeton partagé", "token", g("token"), "readonly", wide=True)
                 + _field("Mot de passe du panel", "admin_password", g("admin_password", ""), "password",
                          hint="pour ouvrir ce panel")
                 + _field("Limite d'envoi", "bandwidth_mbps", g("bandwidth_mbps", "0"), "number",
                          hint="Mbit/s, 0 = aucune"))
        + _section("Ton Radarr", "Celui qui télécharge chez toi. Clé dans Settings > General.",
                   _field("Adresse", "radarr_url", g("radarr_url", "http://192.168.1.10:7878"), test_key="url",
                          hint="avec l'URL Base, ex. /radarr")
                   + _field("Clé API", "radarr_key", g("radarr_key", ""), test_key="key")
                   + _field("Dossier des films", "radarr_root", g("radarr_root", ""), wide=True, listid="roots-radarr",
                            hint="pour les films ajoutés pour B")
                   + '<datalist id="profiles-radarr"></datalist><datalist id="roots-radarr"></datalist>', "radarr")
        + _section("Ton Sonarr", "Même chose pour les séries.",
                   _field("Adresse", "sonarr_url", g("sonarr_url", "http://192.168.1.10:8989"), test_key="url")
                   + _field("Clé API", "sonarr_key", g("sonarr_key", ""), test_key="key")
                   + _field("Dossier des séries", "sonarr_root", g("sonarr_root", ""), wide=True, listid="roots-sonarr")
                   + '<datalist id="profiles-sonarr"></datalist><datalist id="roots-sonarr"></datalist>', "sonarr")
        + _section("Ton qBittorrent", "Pour lire le temps de seed et la progression en direct.",
                   _field("Adresse", "qbit_url", g("qbit_url", "http://192.168.1.10:8080"), wide=True, test_key="url")
                   + _field("Utilisateur", "qbit_user", g("qbit_user", "admin"), test_key="user")
                   + _field("Mot de passe", "qbit_pass", g("qbit_pass", ""), "password", test_key="password"),
                   "qbittorrent")
        + _section("Rétention", "Quand supprimer ce que tu as téléchargé pour B.",
                   _field("Délai après livraison", "keep_days", g("keep_days", "7"), "number", hint="jours")
                   + _field("Seed minimum", "seed_days", g("seed_days", "3"), "number", hint="jours")
                   + _field("Abandon d'un téléchargement", "fail_days", g("fail_days", "7"), "number", hint="jours"))
        + '<fieldset class="card"><legend>Correspondance des profils</legend><p class="hint">Pour chaque profil '
          "qualité de B, le profil de ton Radarr ou Sonarr à utiliser et ce que B exige au minimum. La ligne "
          "<b>default</b> sert pour tous les profils non listés. Teste ton Radarr pour voir tes profils.</p>"
          f'<div id="prows">{rows_html}</div><template id="prow">{template}</template>'
          '<div class="test"><button type="button" onclick="addRow()">Ajouter une correspondance</button></div>'
          "</fieldset>"
    )


def agent_form(v: dict) -> str:
    g = v.get
    return (
        _section("Le server de A", "L'adresse du server chez A (Tailscale conseillé) et le jeton affiché sur "
                                   "sa page de configuration.",
                 _field("Adresse du server", "server_url", g("server_url", "http://100.x.y.z:8787"),
                        wide=True, test_key="url", hint="affichée par le server de A (page de configuration, logs)")
                 + _field("Jeton partagé", "token", g("token", ""), wide=True, test_key="token"), "server")
        + _section("Côté B", "Ce que le Radarr et le Sonarr de B utilisent pour parler à l'agent.",
                   _field("Clé API de l'agent", "api_key", g("api_key"), "readonly",
                          hint="à mettre dans l'indexeur et le client SABnzbd")
                   + _field("Adresse de l'agent vue par Radarr", "public_url",
                            g("public_url", "http://availarr-agent:8789"))
                   + _field("Dossier de téléchargement", "complete_dir", g("complete_dir", "/downloads/complete"),
                            wide=True, hint="vu par l'agent, partagé avec Radarr et Sonarr"))
        + _section("Radarr de B", "Pour lire le profil qualité demandé.",
                   _field("Adresse", "radarr_url", g("radarr_url", "http://radarr:7878"), test_key="url")
                   + _field("Clé API", "radarr_key", g("radarr_key", ""), test_key="key"), "radarr_b")
        + _section("Sonarr de B", "Même chose pour les séries.",
                   _field("Adresse", "sonarr_url", g("sonarr_url", "http://sonarr:8989"), test_key="url")
                   + _field("Clé API", "sonarr_key", g("sonarr_key", ""), test_key="key"), "sonarr_b")
    )


def _num(v, default):
    try:
        return float(v) if "." in str(v) else int(v)
    except (TypeError, ValueError):
        return default


def server_config(form) -> dict:
    profiles = {}
    for name, a, res, lang, ann in zip(form.getlist("p_name"), form.getlist("p_a"), form.getlist("p_res"),
                                       form.getlist("p_lang"), form.getlist("p_announce")):
        if name.strip():
            profiles[name.strip()] = {"a_profiles": [x.strip() for x in a.split(",") if x.strip()],
                                      "min_resolution": _num(res, 1080),
                                      "languages": [x.strip() for x in lang.split(",") if x.strip()],
                                      "announce": ann.strip()}
    f = form.get
    return {"token": f("token", ""), "admin_password": f("admin_password", ""),
            "bandwidth_mbps": _num(f("bandwidth_mbps"), 0),
            "radarr": {"url": f("radarr_url", "").rstrip("/"), "key": f("radarr_key", ""),
                       "root_folder": f("radarr_root", "")},
            "sonarr": {"url": f("sonarr_url", "").rstrip("/"), "key": f("sonarr_key", ""),
                       "root_folder": f("sonarr_root", "")},
            "qbittorrent": {"url": f("qbit_url", "").rstrip("/"), "user": f("qbit_user", ""),
                            "password": f("qbit_pass", "")},
            "retention": {"keep_days": _num(f("keep_days"), 7), "seed_days": _num(f("seed_days"), 3),
                          "fail_days": _num(f("fail_days"), 7)},
            "profiles": profiles}


def agent_config(form) -> dict:
    f = form.get
    return {"server_url": f("server_url", "").rstrip("/"), "token": f("token", ""), "api_key": f("api_key", ""),
            "public_url": f("public_url", "").rstrip("/"), "complete_dir": f("complete_dir", ""),
            "radarr": {"url": f("radarr_url", "").rstrip("/"), "key": f("radarr_key", "")},
            "sonarr": {"url": f("sonarr_url", "").rstrip("/"), "key": f("sonarr_key", "")}}


def _values(kind: str, form) -> dict:
    """Form back into the page's field values, so an error never loses what was typed."""
    v = {k: form.get(k) for k in form.keys() if not k.startswith("p_")}
    if kind == "server":
        v["profiles"] = [{"name": n, "a": a, "res": r, "lang": l, "announce": an} for n, a, r, l, an in zip(
            form.getlist("p_name"), form.getlist("p_a"), form.getlist("p_res"), form.getlist("p_lang"),
            form.getlist("p_announce"))]
    return v


def create_setup(kind: str, path: str, loader, on_saved=lambda: None, code: str | None = None) -> FastAPI:
    code = code or secrets.token_hex(3).upper()
    log.warning("Configuration initiale : ouvre la page web de %s et saisis le code %s", kind, code)
    first = {"token": secrets.token_urlsafe(32)} if kind == "server" else {"api_key": secrets.token_urlsafe(24)}
    title = "Configuration du server (chez A)" if kind == "server" else "Configuration de l'agent (chez B)"
    app = FastAPI()

    def page(values: dict, error: str = "") -> str:
        form = server_form(values) if kind == "server" else agent_form(values)
        return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{title}, Availarr</title><style>{CSS}{SETUP_CSS}</style></head><body>"
                f'<header class="top"><div class="in"><span class="brand">{LOGO}Availarr</span></div></header>'
                f'<main class="page"><div class="head"><div><h2>{title}</h2><p class="sub">Aucune configuration '
                f"trouvée dans <b>{escape(path)}</b>. Remplis ce formulaire : il écrit ce fichier, puis le service "
                "démarre. Le code de configuration est écrit dans les logs du conteneur.</p></div></div>"
                + (f'<p class="error">{escape(error)}</p>' if error else "")
                + '<form method="post" action="/save">'
                + _section("Code de configuration", "Lis-le avec <code>docker compose logs</code>. Il évite qu'une "
                           "autre personne sur le réseau configure le service à ta place.",
                           _field("Code", "setup_code", values.get("setup_code", ""), wide=True))
                + '<fieldset class="card"><legend>Tailscale</legend><p class="hint">La liaison entre A et B. '
                  "Première fois : ajoute d'abord les règles de <code>examples/tailscale-acl.hujson</code> dans "
                  "l'admin Tailscale (Access controls), sinon la connexion est refusée.</p>"
                  f'<div id="ts">{tailscale_view(tailscale_status(), kind, False)}</div></fieldset>'
                + form
                + '<div class="actions"><button class="primary">Enregistrer et démarrer</button></div></form></main>'
                f"<script>{SETUP_JS}</script></body></html>")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return page(dict(first))

    @app.get("/tailscale", response_class=HTMLResponse)
    def tailscale(code_: str = Query("", alias="code")):
        return tailscale_view(tailscale_status(), kind, secrets.compare_digest(code_.strip().upper(), code))

    @app.post("/test")
    async def test(request: Request):
        f = await request.json()
        if not secrets.compare_digest(str(f.get("code", "")).strip().upper(), code):
            return JSONResponse({"ok": False, "msg": "Code de configuration incorrect"})
        return check(f.get("kind", ""), f)

    @app.post("/save", response_class=HTMLResponse)
    async def save(request: Request):
        form = await request.form()
        values = _values(kind, form)
        if not secrets.compare_digest(str(form.get("setup_code", "")).strip().upper(), code):
            return HTMLResponse(page(values, "Code de configuration incorrect."), 403)
        data = server_config(form) if kind == "server" else agent_config(form)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".toml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(toml_dump(data))
        try:
            loader(tmp)  # the real loader decides what is valid
        except (ValueError, KeyError, TypeError) as e:
            os.remove(tmp)
            return HTMLResponse(page(values, f"Configuration incomplète : {e}"), 400)
        os.replace(tmp, path)
        log.warning("Configuration écrite dans %s, démarrage", path)
        on_saved()
        return HTMLResponse(page_done(kind))

    return app


def page_done(kind: str) -> str:
    nxt = ("Le panel s'ouvre à la même adresse dans quelques secondes. Recopie le jeton dans l'agent de B."
           if kind == "server" else "Ajoute maintenant l'agent dans le Radarr et le Sonarr de B (voir le README).")
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta http-equiv="refresh" content="6;url=/">'
            f"<title>Availarr</title><style>{CSS}</style></head><body>"
            f'<header class="top"><div class="in"><span class="brand">{LOGO}Availarr</span></div></header>'
            f'<main class="page"><h2>Configuration enregistrée</h2><p class="sub">{nxt}</p></main></body></html>')


def run_setup(kind: str, path: str, loader, port: int) -> None:
    """Blocks until the form has written a valid config file."""
    server: uvicorn.Server | None = None

    def done():
        server.should_exit = True
    server = uvicorn.Server(uvicorn.Config(create_setup(kind, path, loader, done), host="0.0.0.0", port=port))
    server.run()
