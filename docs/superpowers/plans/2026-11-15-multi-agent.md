# Multi-agent, interfaces et corrections — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** plusieurs agents sur un server avec un jeton partagé et un UUID par agent ; panel agents → demandes → détail ;
interface agent ; pages de config ; seed visible ; demandes annulées figées ; Refuser / Retirer.

**Architecture:** l'agent envoie son UUID et son nom en en-têtes ; le server les range dans une table `agents` et
marque chaque job de l'agent qui l'a fait. Le panel reste du HTML rendu côté server (fragments rafraîchis par
`fetch`), même CSS (`availarr/ui.py`). Les pages de config réutilisent les formulaires de `availarr/setup.py`.

**Tech Stack:** Python 3.12, FastAPI, httpx, sqlite3, pytest (TestClient, httpx.MockTransport).

**Spec:** `docs/superpowers/specs/2026-11-15-multi-agent-design.md` — maquettes :
https://claude.ai/artifact/8gs1SKAXLJYsj7c1x7uTqW (la source des maquettes reprend le vrai CSS, à suivre pour le
markup).

## Global Constraints

- Jeton unique `token` inchangé. En-têtes : `X-Availarr-Agent` (UUID), `X-Availarr-Agent-Name` (nom).
- Sans en-tête : agent id `legacy`, nom `Agent`. Colonne `jobs.agent TEXT DEFAULT 'legacy'`.
- En ligne = `last_seen` il y a moins de `AGENT_ONLINE_S = 120` s.
- Erreur d'un refus : exactement `Refusée par l'envoyeur`.
- Agent bloqué : 403 sur toute l'API ; job d'un autre agent : 404.
- Toute chaîne venant d'un agent (nom) passe par `html.escape` avant d'être rendue.
- Migrations sqlite par `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` (motif de `availarr/agent/db.py:43`).
- Les tests existants restent verts ; un agent ou un server non mis à jour continue de fonctionner.
- Textes de l'interface en français, ton des textes actuels (tutoiement).

## Review Focus

1. Mise à jour pendant un transfert : le premier agent mis à jour récupère tous les jobs `legacy`, ses transferts en cours continuent sans 404 (Task 1).
2. Deux agents sur le même film : la livraison à l'un + `keep_days` ne supprime rien tant que l'autre n'est pas éligible (Task 3).
3. Refuser un job dont l'entrée est partagée avec le job actif d'un autre agent : aucun retrait chez l'envoyeur (Task 5).
4. Nom d'agent `<script>` : échappé dans la liste et sur sa page (Task 6).
5. Enregistrer la config avec les champs mot de passe vides : les anciens mots de passe sont gardés (Task 7).

---

### Task 1: Identité des agents côté server

**Files:**
- Modify: `availarr/server/db.py`, `availarr/server/api.py`, `availarr/server/jobs.py`
- Test: `tests/test_server_db.py`, `tests/test_api.py`, `tests/test_jobs.py`

**Interfaces:**
- Produces:
  - `Job.agent: str` (dernier champ de `Job`, défaut côté SQL `'legacy'`) ; `Job.hidden_at: float | None` (ajouté ici aussi pour une seule migration, utilisé en Task 5).
  - `@dataclass Agent(id: str, name: str, first_seen: float, last_seen: float, blocked: bool, renamed: bool)` dans `db.py`.
  - `JobStore.seen(agent_id: str, name: str, now: float) -> Agent` : crée ou met à jour (`name` ignoré si `renamed`).
  - `JobStore.agents() -> list[Agent]`, `JobStore.agent(id: str) -> Agent | None`,
    `JobStore.set_agent(id: str, **cols)` (colonnes `name`, `blocked`, `renamed`).
  - `JobStore.create(item, b_profile, release_title, units, protected_units, added_by_tool, now, agent: str = "legacy")`.
  - `JobStore.active_for(item, agent: str)`, `JobStore.failed_for(item, agent: str)`.
  - `Jobs.lookup(item, b_profile, episodes=0, agent="legacy")`, `Jobs.request(item, b_profile, release_title, agent="legacy")`.
  - API : dépendance `auth` qui renvoie l'`agent_id` ; `jobs.agent_seen` supprimé (remplacé par `store.seen`).

- [ ] **Step 1: tests db** — `test_agents_seen_and_rename`: `seen("a1","Lyon",10)` puis `seen("a1","Lyon2",20)` → `name=="Lyon2"`, `first_seen==10`, `last_seen==20` ; après `set_agent("a1", name="Maison", renamed=1)`, `seen("a1","X",30).name=="Maison"`. `test_migration_adds_agent_column`: une base créée avec l'ancien `_SCHEMA` (copie dans le test) puis `JobStore(path)` → `get(id).agent == "legacy"` et `hidden_at is None`. `test_active_for_is_per_agent`: job `pending` d'`a1` sur M → `active_for(M,"a2") is None`.
- [ ] **Step 2: tests api** — `test_agent_headers_register`: POST `/v1/lookup` avec `H | {"X-Availarr-Agent": "a1", "X-Availarr-Agent-Name": "Lyon"}` → `store.agent("a1").name == "Lyon"`. `test_no_header_is_legacy`: requête sans en-tête → job `agent == "legacy"`. `test_other_agent_gets_404`: job créé par `a1`, GET `/v1/requests/{id}` par `a2` → 404, idem `/files/0` et `/close`. `test_first_agent_adopts_legacy`: deux jobs `legacy` (un livré, un `ready`), premier appel de `a1` → les deux ont `agent == "a1"`, GET `/v1/requests/{id}` par `a1` → 200, par `a2` → 404 ; ensuite un job `legacy` créé par un appel sans en-tête n'est **pas** repris par `a2` (adoption seulement quand aucun agent UUID n'existait). `test_blocked_agent_403`: `set_agent("a1", blocked=1)` → `/v1/ping` 403.
- [ ] **Step 3: tests jobs** — `test_request_per_agent_creates_two_jobs`: `request(M,P,"t",agent="a1")` puis `agent="a2"` → deux jobs distincts, `ensure` appelé une fois par job sans erreur. `test_failure_blocks_only_that_agent`: job `failed` d'`a1` → `lookup(M,P,agent="a1") is None`, `lookup(M,P,agent="a2")` non nul.
- [ ] **Step 4: lancer** `python -m pytest tests/test_server_db.py tests/test_api.py tests/test_jobs.py -q` → FAIL.
- [ ] **Step 5: implémenter.** `auth` lit les en-têtes (`Header(None, alias="X-Availarr-Agent")`), appelle `store.seen`, lève 403 si `blocked`, renvoie l'id. `JobStore.seen` : à la création d'un agent ≠ `legacy`, si aucun autre agent ≠ `legacy` n'existe, `UPDATE jobs SET agent=? WHERE agent='legacy'` dans la même transaction. `job_or_404(id, agent)` : 404 si `job.agent != agent`. `agent` et `hidden_at` dans `set()` (pas dans `_OWNED` pour `agent`, `hidden_at` dans `_OWNED`). Le panel (`panel.py:285`) lit encore `jobs.agent_seen` : le remplacer provisoirement par le max de `store.agents()` last_seen (Task 6 refait cette partie).
- [ ] **Step 6: lancer** `python -m pytest -q` → tout PASS.
- [ ] **Step 7: commit** `feat(server): agents identifiés par UUID, jobs cloisonnés par agent`

### Task 2: L'agent envoie son identité

**Files:**
- Modify: `availarr/agent/config.py`, `availarr/agent/transfer.py`, `availarr/agent/__main__.py`, `availarr/setup.py`
- Test: `tests/test_server_client.py`, `tests/test_setup.py`, `tests/test_entrypoints.py`

**Interfaces:**
- Produces:
  - `AgentConfig.agent_id: str = ""`, `agent_name: str = ""`, `admin_password: str = ""` (TOML : clés de premier niveau).
  - `ensure_identity(path: str, cfg: AgentConfig) -> AgentConfig` dans `config.py` : si `agent_id` vide, génère `str(uuid.uuid4())`, nom par défaut `socket.gethostname()`, `admin_password` par défaut `cfg.api_key`, réécrit le TOML (`setup.toml_dump`) et renvoie la config complétée.
  - `ServerClient(base_url, token, client=None, agent_id="", agent_name="")` : ajoute les deux en-têtes à `self.headers` quand `agent_id` est non vide.
  - `transfer.SERVER: dict = {"ok_at": None, "error": None}` mis à jour par `step_once` dans `__main__.py` (ping réussi → `ok_at=time.time()`, `error=None` ; échec → `error=str(e)`).
  - `setup.agent_form` : bloc « Identité » (champs `agent_name`, `agent_id` readonly, `admin_password` password) ; `setup.agent_config` les écrit.

- [ ] **Step 1: tests** — `test_client_sends_identity`: MockTransport vérifie `X-Availarr-Agent == "u1"` et `X-Availarr-Agent-Name == "Lyon"`. `test_ensure_identity_writes_uuid` (tmp TOML sans `agent_id`) : renvoie un UUID valide (`uuid.UUID(x)`), le fichier relu par `load_agent_config` a le même, un second appel ne le change pas. `test_agent_setup_writes_identity`: POST `/save` du setup agent avec `agent_name=Lyon` → TOML contient `agent_name = "Lyon"` et un `agent_id`.
- [ ] **Step 2: lancer** → FAIL.
- [ ] **Step 3: implémenter** ; `create_setup` agent : `first` contient aussi `agent_id=str(uuid4())`.
- [ ] **Step 4: lancer** `python -m pytest -q` → PASS.
- [ ] **Step 5: commit** `feat(agent): identité (UUID, nom) envoyée au server`

### Task 3: Rétention multi-agent

**Files:**
- Modify: `availarr/server/retention.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: `Job.agent` (Task 1).
- Produces: `remove_entry(jobs: Jobs, qbit, job: Job, all_jobs: list[Job], now: float) -> None` : le corps actuel de suppression (`remove` + `qbit.delete` + `deleted_at` des jobs couverts), extrait de `tick` pour que Task 5 le réutilise.

Règle : les fichiers d'une entrée (`_shares_entry`) ne sont supprimés que si chaque job non supprimé qui la partage
est soit éligible, soit un job plus ancien **du même agent et du même item** qu'un job éligible. « Garder » sur
n'importe lequel bloque tout. Les jobs marqués `deleted_at` sont tous ceux couverts.

- [ ] **Step 1: tests** — `test_two_agents_waits_for_both`: même film, `a1` livré il y a 8 j (éligible), `a2` `ready` sans `closed_at` → `tick` ne supprime rien ; quand `a2` est livré depuis 8 j → supprimé, les deux jobs ont `deleted_at`. `test_keep_by_one_agent_protects_entry`: `a2` `decision="keep"` → rien supprimé. `test_requested_again_newest_job_decides` (existant) reste vert pour un même agent.
- [ ] **Step 2: lancer** `python -m pytest tests/test_retention.py -q` → FAIL.
- [ ] **Step 3: implémenter** la règle dans `tick` et extraire `remove_entry`.
- [ ] **Step 4: lancer** `python -m pytest -q` → PASS.
- [ ] **Step 5: commit** `feat(retention): fichiers partagés gardés tant qu'un autre agent en a besoin`

### Task 4: Seed et torrents en direct

**Files:**
- Modify: `availarr/server/qbit.py`, `availarr/server/library.py`, `availarr/server/radarr.py`, `availarr/server/sonarr.py`, `availarr/server/panel.py`, `tests/fakes.py`
- Test: `tests/test_qbit.py`, `tests/test_radarr.py`, `tests/test_sonarr.py`, `tests/test_panel.py`

**Interfaces:**
- Produces:
  - `@dataclass Torrent(hash: str, name: str, state: str, progress: float, seeding: int | None, upspeed: int, dlspeed: int, ratio: float, peers: int)` dans `qbit.py`.
  - `QBit.info(hashes: list[str]) -> dict[str, Torrent]` : un seul appel `/torrents/info` ; `seeding = seeding_time` ou, s'il manque, `int(time.time()) - completion_on` quand `completion_on > 0`, sinon `None` ; `peers = num_seeds + num_leechs`. Hashes invalides ignorés (comme `live`).
  - `Library.queue_hashes(item) -> list[str]` (Radarr : `/queue/details?movieId`, Sonarr : filtré sur les épisodes de l'item comme `progress`) ; `FakeLibrary.queue_hashes` = attribut `_Attr(list)` défaut `[]`.
  - `panel.torrent_hashes(job) -> list[str]` : `job.hashes` si non vide, sinon `lib.queue_hashes(item)` quand `job.state == "pending"`.
  - `panel.fmt_seedline(ts: list[Torrent], seed_days: float) -> str` : en seed → `reste {fmt_seed restant} · ↑ {fmt_mo(up)} · ratio {r:.2f}` (virgule décimale) ; en téléchargement → `↓ {fmt_mo(dl)} · {peers} pairs`. Somme des débits, ratio et seeding = minimum sur les torrents.

- [ ] **Step 1: tests** — `test_info_fallback_completion_on`: réponse sans `seeding_time`, `completion_on = now-3600` → `seeding` ≈ 3600 (±5). `test_info_ignores_bad_hash`. `test_queue_hashes` (Radarr et Sonarr, `downloadId` en minuscules, dédoublonnés). `test_seed_line_while_downloading`: job `pending`, `job.hashes == []`, `lib.queue_hashes = ["h"]`, qbit fake `info` renvoie `dlspeed=11_744_051, peers=41` → la page contient `↓ 11,2 Mo/s · 41 pairs`. `test_seed_line_seeding`: seeding 7 h, seed_days 3 → contient `reste 2 j 17 h` et `ratio 0,42`.
- [ ] **Step 2: lancer** → FAIL.
- [ ] **Step 3: implémenter** ; `FakeQbit` gagne `info()` construit depuis un dict `{hash: Torrent}` ; `seed()` du panel utilise `info` (un appel) au lieu de `seed_seconds` + `seed_ok`.
- [ ] **Step 4: lancer** `python -m pytest -q` → PASS.
- [ ] **Step 5: commit** `fix(panel): seed, débits et ratio visibles, y compris pendant le téléchargement`

### Task 5: Demandes annulées figées, Refuser, Retirer

**Files:**
- Modify: `availarr/server/jobs.py`, `availarr/server/panel.py`, `availarr/server/stats.py` (si elle filtre les lignes affichées : non, les stats comptent aussi les jobs retirés)
- Test: `tests/test_jobs.py`, `tests/test_panel.py`

**Interfaces:**
- Consumes: `remove_entry` (Task 3), `Job.hidden_at` (Task 1).
- Produces:
  - `Jobs.refuse(job: Job, qbit) -> None` : refusé si `job.outcome == "delivered"` ou `job.deleted_at` (`ValueError`) ; sinon `store.set(id, state="failed", error="Refusée par l'envoyeur")` puis, si `job.added_by_tool` et qu'aucun autre job non supprimé (autre id, tout agent) ne partage l'entrée avec `state in ("pending","ready")` et `outcome is None`, `remove_entry(...)` sur ses unités non protégées.
  - `Jobs.hide(job: Job) -> None` : `ValueError` si le job est actif (`deleted_at is None and outcome is None and state in ("pending","ready")`) ; sinon `store.set(id, hidden_at=clock())`.
  - Panel : actions POST `/jobs/{id}/refuse` et `/jobs/{id}/hide` (409 sur `ValueError`) ; les jobs `hidden_at` n'apparaissent dans aucune liste.
  - Un job `outcome == "cancelled"` : `row()` n'appelle ni `jobs.status`, ni `jobs.transfer`, ni qBittorrent ; trois barres vides libellées `Annulé chez le receveur` (seed : `figé, demande annulée`), badge seul.

- [ ] **Step 1: tests** — `test_cancelled_row_is_frozen`: deux jobs sur M, un `outcome="cancelled"` et `pending`, `lib.progress = 0.5` → la ligne annulée ne contient pas `50 %`, la ligne active si ; `lib.calls["progress"]` n'a qu'un appel par rendu. `test_refuse_fails_job_and_removes_entry`: job `added_by_tool`, seul sur l'entrée → `state=="failed"`, `error=="Refusée par l'envoyeur"`, `lib.removed` non vide. `test_refuse_shared_entry_keeps_files`: autre agent actif sur M → `lib.removed == []`. `test_refuse_delivered_409`. `test_hide_only_when_finished`: job actif → 409 ; job livré → 303 et absent de `/agents/{agent}` (ou `/` avant Task 6). `test_refused_job_blocks_until_retry`: après refus, `lookup(M,P,agent=…) is None` ; après action `retry`, non nul.
- [ ] **Step 2: lancer** → FAIL.
- [ ] **Step 3: implémenter** ; boutons comme la maquette : en cours → `Refuser` ; livré/annulé/échoué/supprimé → `Retirer` ; échoué → `Réessayer` + `Retirer`.
- [ ] **Step 4: lancer** `python -m pytest -q` → PASS.
- [ ] **Step 5: commit** `fix(panel): demande annulée figée ; feat: refuser et retirer une demande`

### Task 6: Panel agents → demandes → détail

**Files:**
- Modify: `availarr/server/panel.py`, `availarr/ui.py` (CSS de la maquette : `.agents`, `a.agent`, `.crumb`, `.detail`, `.steps`, `.seedline`, `.note`, `.mono`, `button.ghost`, `.big-acts`)
- Test: `tests/test_panel.py`

**Interfaces:**
- Consumes: `store.agents()`, `store.agent()`, `store.set_agent()` (Task 1) ; `fmt_seedline`, `torrent_hashes` (Task 4) ; actions de Task 5.
- Produces (routes, toutes sous Basic auth) :
  - `GET /` + `GET /fragment` : cartes d'agents triées en ligne d'abord puis `last_seen` décroissant ; compteurs `En cours` (actif), `À décider` (`show(j,"pending")`), `Livrés` ; dernière demande (titre via `split_title`, `fmt_ago`) ; bannière profils non mappés conservée.
  - `GET /agents/{id}` + `GET /agents/{id}/fragment?f=` : la liste actuelle filtrée sur `job.agent == id`, fil d'Ariane, boutons `Renommer` (formulaire `name`) et `Bloquer`/`Débloquer` → `POST /agents/{id}/rename`, `POST /agents/{id}/block`, `POST /agents/{id}/unblock`, redirection 303 vers la page de l'agent. 404 si agent inconnu.
  - `GET /jobs/{id}` + `GET /jobs/{id}/fragment` : détail comme la maquette (Progression + étapes, Torrent chez toi, Fichiers servis, Demande, Épisodes, Envois). Étapes datées depuis `created_at`, `ready_at`, `closed_at`, `deleted_at` ; envois = `store.transfers()` filtrés sur `job_id`. Les actions de job redirigent vers la page d'où elles viennent (`?back=` limité à `/`, `/agents/<id>`, `/jobs/<id>`).
  - Le titre d'une ligne est un lien vers `/jobs/{id}`.

- [ ] **Step 1: tests** — `test_agents_list`: deux agents (`seen` à `now-10` et `now-500`) → page contient les deux noms, `En ligne` une fois, `Hors ligne` une fois, compteurs exacts. `test_agent_name_escaped`: nom `<script>x</script>` → `&lt;script&gt;` présent, `<script>x` absent. `test_agent_page_filters_by_agent`: jobs de `a1` et `a2` → `/agents/a1` ne montre que ceux d'`a1`. `test_rename_and_block`: POST rename → `agent("a1").name == "Maison"` et `renamed` ; POST block → `blocked`. `test_job_detail`: `/jobs/{id}` contient le nom de l'agent, `b_profile`, le profil chez toi (`cfg.profiles[P].a_profiles[0]`), les fichiers et une ligne par transfert. `test_unknown_agent_404`, `test_unknown_job_404`. `test_back_redirect_is_local`: `back=https://evil` → redirection vers `/`.
- [ ] **Step 2: lancer** → FAIL.
- [ ] **Step 3: implémenter** ; reprendre le markup de la maquette (source : script de maquette, mêmes classes).
- [ ] **Step 4: lancer** `python -m pytest -q` → PASS.
- [ ] **Step 5: vérification à l'œil** : lancer le panel avec une base de test et comparer à la maquette (skill `run`).
- [ ] **Step 6: commit** `feat(panel): liste des agents, demandes par agent, détail d'une demande`

### Task 7: Pages de configuration (server et agent)

**Files:**
- Modify: `availarr/setup.py`, `availarr/server/panel.py`, `availarr/server/__main__.py`
- Test: `tests/test_setup.py`, `tests/test_panel.py`

**Interfaces:**
- Produces:
  - `form_values(kind: str, d: dict) -> dict` dans `setup.py` : TOML chargé (`tomllib`) → valeurs du formulaire (inverse de `server_config` / `agent_config`, profils compris) ; mots de passe (`admin_password`, `qbit_pass`) mis à `""`.
  - `config_router(kind: str, path: str, loader, restart: Callable[[], None]) -> APIRouter` : `GET /config` (formulaire pré-rempli, sans code de configuration ni bloc Tailscale, bouton `Enregistrer et redémarrer`), `POST /config/test` (= `check`), `POST /config` (champ mot de passe vide → valeur actuelle du fichier ; validation par `loader` sur un fichier temporaire comme `create_setup` ; puis `restart()` et page « Configuration enregistrée, redémarrage… » avec `meta refresh` 8 s).
  - `SETUP_JS` : URL de test et code lus sur `document.body.dataset` (`data-test="/test"` au setup, `"/config/test"` en config) ; code absent → non envoyé.
  - `restart` par défaut : `threading.Timer(1.0, os._exit, (0,)).start()` (Docker `restart: unless-stopped` relance).
  - Panel : onglet `Configuration` → `/config` (router inclus avec la dépendance Basic auth du panel). `create_panel(jobs, qbit, cfg, clock=time.time, config_path=None, restart=None)` ; onglet absent si `config_path is None`.

- [ ] **Step 1: tests** — `test_form_values_roundtrip`: `server_config(form)` → TOML → `form_values` → mêmes champs, `admin_password == ""`. `test_config_keeps_blank_passwords`: POST `/config` avec `admin_password=""` → le TOML garde l'ancien mot de passe, `restart` appelé une fois. `test_config_invalid_not_written`: `token` vide → 400, fichier inchangé, `restart` non appelé. `test_config_requires_auth`: `/config` sans Basic → 401. `test_config_tab_in_panel`.
- [ ] **Step 2: lancer** → FAIL.
- [ ] **Step 3: implémenter.**
- [ ] **Step 4: lancer** `python -m pytest -q` → PASS.
- [ ] **Step 5: commit** `feat: page de configuration dans le panel`

### Task 8: Interface de l'agent

**Files:**
- Create: `availarr/agent/ui.py`
- Modify: `availarr/agent/__main__.py`
- Test: `tests/test_agent_ui.py`

**Interfaces:**
- Consumes: `AgentStore.all()`, `transfer.SERVER`, `transfer.ETA`, `AgentConfig.agent_id/agent_name/admin_password` (Task 2), `config_router` (Task 7).
- Produces: `create_ui(cfg: AgentConfig, store: AgentStore, config_path: str, restart, clock=time.time) -> APIRouter` : routes `/`, `/fragment`, `/config*`, dépendance Basic auth (mot de passe `cfg.admin_password`, `secrets.compare_digest`). Les routes `/newznab/*` et `/sabnzbd/*` ne passent pas par cette dépendance.
  Page `/` comme la maquette : chiffres (server connecté si `SERVER["ok_at"]` < 120 s, sinon « Injoignable » + `SERVER["error"]` ; nom et UUID ; en cours ; débit = somme des débits live), puis une ligne par job non `hidden` : release, badge (`queued` → « L'envoyeur télécharge », `transferring` → « Transfert », `completed` → « Terminé », `failed` → `fail_message`, `cancelled` → « Annulé »), barre (`AgentJob.percentage`), ETA depuis `ETA[nzo_id]` (`fmt_eta` de `panel.py`, à déplacer dans `availarr/ui.py` avec `fmt_bytes`/`fmt_mo` pour les partager).

- [ ] **Step 1: tests** — `test_ui_requires_password` (401 sans Basic, 200 avec). `test_sabnzbd_unaffected` (`/sabnzbd/api?mode=version&apikey=…` sans Basic → 200). `test_lists_jobs_with_states`: un job par état → libellés attendus, `Refusée par l'envoyeur` visible pour un `failed` de ce message. `test_server_status`: `SERVER["ok_at"] = now-10` → `Connecté` ; `None` + `error="boom"` → `Injoignable` et `boom`.
- [ ] **Step 2: lancer** → FAIL.
- [ ] **Step 3: implémenter** ; `__main__.build` inclut `create_ui(...)` et passe `restart`.
- [ ] **Step 4: lancer** `python -m pytest -q` → PASS.
- [ ] **Step 5: commit** `feat(agent): interface de suivi et page de configuration`

### Task 9: Documentation

**Files:**
- Modify: `README.md`, `examples/agent.toml`, `compose.agent.yml` (commentaire du port 8789 : interface protégée par `admin_password`)

- [ ] **Step 1:** README : plusieurs receveurs (même jeton, chacun apparaît à sa première connexion, Renommer/Bloquer), interface de l'agent, pages de config, Refuser/Retirer. `examples/agent.toml` : `agent_name`, `admin_password` (`agent_id` généré tout seul).
- [ ] **Step 2:** `python -m pytest -q` → PASS.
- [ ] **Step 3: commit** `docs: multi-agent, interface de l'agent, configuration`
