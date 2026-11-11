# Availarr

Availarr permet à B d'utiliser la médiathèque de A comme source de téléchargement, depuis son propre Radarr/Sonarr, sans rien changer à leur fonctionnement.

- Chez **B**, l'**agent** se fait passer pour un indexeur **Newznab** et un client de téléchargement **SABnzbd**. Radarr/Sonarr de B cherchent, prennent et importent comme d'habitude.
- Chez **A**, le **server** reçoit les demandes. Si A possède déjà le titre en qualité suffisante, il le livre. Sinon, il l'ajoute dans son Radarr/Sonarr (tag `availarr`), attend le téléchargement (qBittorrent), puis le livre.
- Le transfert des fichiers est repris automatiquement après une coupure.

```
 B : Radarr/Sonarr --Newznab/SABnzbd--> agent (8789) ==HTTP via Tailscale, ou HTTPS via reverse proxy==> server (8787) --> Radarr/Sonarr/qBittorrent de A
                                                                                                         panel (8788, réseau de A uniquement)
```

## Installation

### Côté A (server)

1. Copier `examples/server.toml` vers `./config/server.toml` et le remplir (clés Radarr/Sonarr, qBittorrent, `token`, `admin_password`, table `[profiles]`).
2. Dans `compose.server.yml`, monter le dossier des médias de A **au même chemin** que dans le Radarr/Sonarr de A (le server lit les fichiers à ce chemin).
3. Préparer les dossiers (le conteneur tourne en uid 1000) : `mkdir -p config data && sudo chown 1000:1000 data`.
4. Définir `TAILSCALE_IP` (IP `tailscale0` de A) : l'API (HTTP + token) n'est publiée que sur cette IP (par défaut `127.0.0.1`). Lier à l'IP tailscale0 ou filtrer le port 8787 au pare-feu, sauf si un reverse proxy HTTPS le précède. Le panel est lié à `127.0.0.1:8788` ; pour le LAN, voir le commentaire de `compose.server.yml`.
5. `docker compose -f compose.server.yml up -d --build`

Le fichier de config est lu depuis `/config/server.toml` (variable `AVAILARR_CONFIG` pour changer). Ports : **8787** API (pour l'agent), **8788** panel (local).

### Côté B (agent)

1. Copier `examples/agent.toml` vers `./config/agent.toml` et le remplir (`server_url`, `token` identique à celui du server, `api_key` au choix, `public_url`, `complete_dir`, clés du Radarr/Sonarr de B).
2. Monter le dossier de téléchargements de B sur `complete_dir`. Le conteneur tourne en utilisateur non-root (uid 1000) : ce dossier doit lui être accessible en écriture.
3. `mkdir -p config data && sudo chown 1000:1000 data`, et `sudo chown 1000:1000` sur le dossier de téléchargements.
4. `docker compose -f compose.agent.yml up -d --build`

Sans Docker : `pip install .` puis `python -m availarr.server` ou `python -m availarr.agent`.

### Réseau entre A et B

- **Recommandé : Tailscale.** A et B sur le même tailnet, `server_url = "http://<nom-tailscale-de-A>:8787"`. Rien n'est exposé sur Internet.
- **Alternative : reverse proxy HTTPS** (Caddy, Traefik, nginx) devant le port 8787 de A, avec `server_url = "https://..."`. Le `token` est alors la seule protection : le choisir long et aléatoire.
- **À éviter : Cloudflare Tunnel** (ou proxy Cloudflare) pour les fichiers. Il limite la taille des requêtes/le débit et interdit en pratique le transfert de gros fichiers vidéo.

Le **panel (8788) ne doit jamais être exposé à B** : il est protégé par `admin_password` mais réservé à A. Ne publier que le port 8787.

## Configuration côté B (Radarr et Sonarr)

**Indexeur** (Settings > Indexers > Newznab) :

| Champ | Valeur |
|---|---|
| URL | `http://availarr-agent:8789/newznab` |
| API Path | `/api` |
| API Key | la valeur `api_key` de `agent.toml` |
| Catégories | `2000` (Radarr) / `5000` (Sonarr) |

**Client de téléchargement** (Settings > Download Clients > SABnzbd) :

| Champ | Valeur |
|---|---|
| Host | `availarr-agent` |
| Port | `8789` |
| URL Base | `sabnzbd` |
| API Key | la valeur `api_key` de `agent.toml` |
| Catégorie | `radarr` ou `sonarr` |

Le nom `availarr-agent` ne se résout que si Radarr/Sonarr de B partagent le réseau Docker de l'agent ; sinon utiliser l'IP de l'hôte de B.

Si le chemin de `complete_dir` vu par l'agent diffère de celui vu par Radarr/Sonarr, ajouter un **Remote Path Mapping** (Settings > Download Clients).

## Correspondance des profils (`[profiles]`)

Rien n'est déduit automatiquement : la table associe le **profil qualité de B** au **profil de A** et à l'exigence minimale.

```toml
[profiles."HD-1080p FR"]        # nom du profil chez B
a_profiles = ["1080p MULTi"]    # profil(s) chez A utilisés pour un ajout / une mise à niveau
min_resolution = 1080
languages = ["French"]
announce = "MULTi.1080p.WEB-DL" # qualité annoncée dans le titre de la release synthétique
```

Un profil de B sans correspondance provoque une erreur visible dans le panel. Il est conseillé de définir un profil **`default`**, utilisé en repli pour les profils non listés.

## Rétention

Un titre livré à B est supprimé de A (si c'est A qui l'avait téléchargé pour B) **7 jours après la clôture (livraison ou annulation)** (`keep_days`) **et** après **3 jours de seed réel** dans qBittorrent (`seed_days`). Le panel affiche « dans 7 j » et le temps de seed.

- 🔒 **Protégé** : A possédait déjà le titre, il n'est jamais supprimé.
- 📌 **Gardé** : A a demandé dans le panel de conserver le titre.
- Les jobs en échec sont purgés après `fail_days` (7 j).

## Vérification de bout en bout

À faire par le propriétaire avec de vraies instances (idéalement un Radarr et un Sonarr de test chez B). Cette vérification n'a **pas** été exécutée lors du développement. Noter ici tout écart de protocole constaté.

- [ ] Le test de connexion de l'indexeur et celui du client SABnzbd passent dans Radarr B et dans Sonarr B.
- [ ] Un film que A possède en qualité suffisante apparaît dans la recherche interactive de B, est pris, transféré et importé chez B. Il apparaît avec 🔒 dans le panel.
- [ ] Un film absent chez A est ajouté au Radarr de A avec le tag `availarr`, téléchargé, transféré et importé chez B. Le panel affiche ensuite « dans 7 j » et le seed réel.
- [ ] Un pack de saison Sonarr fonctionne de la même façon.
- [ ] Couper le server pendant un transfert, puis le relancer : le transfert reprend.

Écarts constatés : _(à compléter)_
