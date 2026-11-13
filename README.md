# Availarr

Availarr permet à un **receveur** d'utiliser la médiathèque d'un **envoyeur** comme source de téléchargement, directement depuis son propre Radarr/Sonarr, sans rien changer à leur fonctionnement.

Pour le Radarr/Sonarr du receveur, Availarr n'est qu'un indexeur et un client de téléchargement de plus. Il cherche, prend et importe comme d'habitude. Les fichiers, eux, viennent de chez l'envoyeur.

## Fonctionnement

```
          RECEVEUR                                              ENVOYEUR
 ┌──────────────────────────┐                      ┌───────────────────────────────────┐
 │ Radarr / Sonarr          │                      │ availarr-server                   │
 │   │ Newznab + SABnzbd    │   Tailscale (ou      │   API   :8787  (pour l'agent)     │
 │   ▼                      │   HTTPS)             │   panel :8788  (réseau local)     │
 │ availarr-agent :8789  ───┼─────────────────────►│   │                               │
 │   │ écrit les fichiers   │                      │   ▼                               │
 │   ▼                      │                      │ Radarr / Sonarr / qBittorrent     │
 │ dossier de téléchargement│                      │ + médiathèque                     │
 └──────────────────────────┘                      └───────────────────────────────────┘
```

- **L'agent**, chez le receveur, se fait passer pour un indexeur **Newznab** et un client **SABnzbd**. Quand Radarr/Sonarr cherchent un titre, l'agent interroge le server et renvoie une release « synthétique ». Quand ils la prennent, l'agent la demande au server, télécharge les fichiers dans `complete_dir` et signale la fin à Radarr/Sonarr, qui importent.
- **Le server**, chez l'envoyeur, reçoit les demandes :
  - si l'envoyeur a déjà le titre en qualité suffisante (selon `[profiles]`), il le livre tout de suite ;
  - sinon, il l'ajoute dans le Radarr/Sonarr de l'envoyeur (tag `availarr`), attend que qBittorrent l'ait téléchargé, puis le livre.
- Un transfert interrompu (coupure réseau, redémarrage) reprend là où il s'était arrêté.
- **Le panel** (port 8788, réservé à l'envoyeur) liste les demandes, leur état, le débit, l'état de l'agent, et permet de garder ou supprimer un titre.
- Les titres téléchargés **pour** le receveur sont supprimés chez l'envoyeur après un délai (voir [Rétention](#rétention)). Ceux que l'envoyeur possédait déjà ne sont jamais touchés.

## Prérequis

- Docker (Linux, avec `/dev/net/tun` pour Tailscale).
- **Envoyeur** : Radarr, Sonarr et qBittorrent, et un accès en lecture à ses médias.
- **Receveur** : Radarr et/ou Sonarr.
- Un compte Tailscale, **côté envoyeur uniquement** (le receveur n'en a pas besoin).

Images Docker Hub (amd64 et arm64) : `kappacitee/availarr-server` et `kappacitee/availarr-agent`.

## Installation

### 1. Réseau : Tailscale (une fois, chez l'envoyeur)

Chaque compose contient un conteneur `tailscale` qui porte le réseau d'Availarr : rien d'autre à installer. Les deux conteneurs rejoignent le tailnet de l'envoyeur.

Dans l'admin Tailscale (https://login.tailscale.com/admin) > *Access controls*, fusionne [`examples/tailscale-acl.hujson`](examples/tailscale-acl.hujson). Il déclare les tags `tag:availarr-server` et `tag:availarr-agent`, et n'autorise l'agent qu'à joindre le port 8787 du server. Sans ces règles, les conteneurs sont refusés.

### 2. Envoyeur : le server

`compose.yml` :

```yaml
services:
  tailscale:
    image: tailscale/tailscale:stable
    hostname: availarr-server
    restart: unless-stopped
    environment:
      TS_AUTHKEY: ${TS_AUTHKEY:-}              # optionnel, voir plus bas
      TS_HOSTNAME: availarr-server
      TS_STATE_DIR: /var/lib/tailscale
      TS_USERSPACE: "false"
      TS_SOCKET: /var/run/tailscale/tailscaled.sock
      TS_EXTRA_ARGS: --advertise-tags=tag:availarr-server
      TS_TAILSCALED_EXTRA_ARGS: --port=21641   # port UDP fixe, à rediriger sur la box
    volumes:
      - ./tailscale:/var/lib/tailscale         # identité Tailscale : ne pas supprimer ni publier
      - tailscale-run:/var/run/tailscale
    devices:
      - /dev/net/tun:/dev/net/tun
    cap_add: [NET_ADMIN, NET_RAW]
    ports:
      - "8788:8788"         # panel, réseau local uniquement ("127.0.0.1:8788:8788" pour cette machine seule)
      - "21641:21641/udp"   # Tailscale (WireGuard)
      # 8787 n'est volontairement pas publié : l'agent le joint par Tailscale

  availarr-server:
    image: kappacitee/availarr-server:latest
    restart: unless-stopped
    network_mode: service:tailscale
    depends_on: [tailscale]
    volumes:
      - tailscale-run:/var/run/tailscale
      - ./config:/config
      - ./data:/data
      - /media:/media:ro    # médias de l'envoyeur, au MÊME chemin que dans son Radarr/Sonarr

volumes:
  tailscale-run:
```

```sh
mkdir -p config data && sudo chown 1000:1000 config data   # le conteneur tourne en uid 1000
docker compose up -d
```

Puis ouvre `http://<hôte-de-l'envoyeur>:8788` et suis [Configuration du server](#configuration-du-server).

### 3. Receveur : l'agent

`compose.yml` :

```yaml
services:
  tailscale:
    image: tailscale/tailscale:stable
    hostname: availarr-agent
    restart: unless-stopped
    environment:
      TS_AUTHKEY: ${TS_AUTHKEY:-}              # optionnel, clé fournie par l'envoyeur
      TS_HOSTNAME: availarr-agent
      TS_STATE_DIR: /var/lib/tailscale
      TS_USERSPACE: "false"
      TS_SOCKET: /var/run/tailscale/tailscaled.sock
      TS_EXTRA_ARGS: --advertise-tags=tag:availarr-agent
      TS_TAILSCALED_EXTRA_ARGS: --port=21641
    volumes:
      - ./tailscale:/var/lib/tailscale
      - tailscale-run:/var/run/tailscale
    devices:
      - /dev/net/tun:/dev/net/tun
    cap_add: [NET_ADMIN, NET_RAW]
    ports:
      - "8789:8789"         # pour Radarr/Sonarr du receveur et la page de configuration
      - "21641:21641/udp"   # optionnel à rediriger sur la box
    networks:
      default:
        aliases: [availarr-agent]   # Radarr/Sonarr sur ce réseau Docker joignent http://availarr-agent:8789

  availarr-agent:
    image: kappacitee/availarr-agent:latest
    restart: unless-stopped
    network_mode: service:tailscale
    depends_on: [tailscale]
    volumes:
      - tailscale-run:/var/run/tailscale
      - ./config:/config
      - ./data:/data
      - /downloads:/downloads   # dossier de téléchargement du receveur (contient complete_dir)

volumes:
  tailscale-run:
```

```sh
mkdir -p config data && sudo chown 1000:1000 config data /downloads
docker compose up -d
```

Puis ouvre `http://<hôte-du-receveur>:8789` et suis [Configuration de l'agent](#configuration-de-lagent).

Les fichiers [`compose.server.yml`](compose.server.yml) et [`compose.agent.yml`](compose.agent.yml) du dépôt sont ces mêmes exemples, commentés.

**Mise à jour** : `docker compose pull && docker compose up -d`.

## Configuration du server

Au premier démarrage, sans `config/server.toml`, le server ouvre une **page de configuration** sur `http://<hôte>:8788`.

1. Saisis le code affiché par `docker compose logs availarr-server`.
2. **Tailscale** : clique sur **Connecter à Tailscale** et connecte-toi. Le bloc passe à « Connecté » et affiche l'adresse à donner à l'agent, du type `http://100.x.y.z:8787`.
3. Remplis le formulaire. Les boutons **Tester** vérifient Radarr, Sonarr et qBittorrent et proposent les profils qualité et dossiers racines existants.
4. Enregistre : `config/server.toml` est écrit et le server démarre. Note le **jeton partagé** affiché en haut, l'agent en a besoin.

Pour refaire la configuration web : supprime `config/server.toml` et redémarre le conteneur. Tu peux aussi écrire le fichier à la main :

```toml
token = "un-long-secret"          # jeton partagé avec l'agent
admin_password = "..."            # mot de passe du panel
bandwidth_mbps = 200              # limite d'envoi en Mbit/s (0 = illimité)
# api_port = 8787
# panel_port = 8788
# db_path = "/data/availarr.db"

[radarr]                          # instances de l'envoyeur
url = "http://radarr:7878"
key = "..."
root_folder = "/movies"           # dossier racine des films ajoutés pour le receveur

[sonarr]
url = "http://sonarr:8989"
key = "..."
root_folder = "/tv"

[qbittorrent]
url = "http://qbittorrent:8080"
user = "admin"
password = "..."

[retention]
keep_days = 7                     # jours après la clôture avant suppression
seed_days = 3                     # jours de seed réel exigés avant suppression
fail_days = 7                     # purge des demandes en échec

[sizes]                           # taille annoncée quand le titre n'est pas encore chez l'envoyeur
movie_gb = 8
episode_gb = 2

[profiles."HD-1080p FR"]          # voir ci-dessous
a_profiles = ["1080p MULTi"]
min_resolution = 1080
languages = ["French"]
announce = "MULTi.1080p.WEB-DL"
```

Le fichier est lu depuis `/config/server.toml` (modifiable via `AVAILARR_CONFIG`).

### Correspondance des profils (`[profiles]`)

Rien n'est deviné : chaque entrée associe un **profil qualité du receveur** (le nom de la table) à ce que l'envoyeur doit fournir.

| Clé | Rôle |
|---|---|
| `a_profiles` | profil(s) de l'envoyeur. Le premier sert pour un ajout ou une mise à niveau ; un fichier dans n'importe lequel compte comme suffisant. |
| `min_resolution` | résolution minimale (`720`, `1080`, `2160`…) pour qu'un fichier existant soit livré tel quel. |
| `languages` | langues audio exigées (ex. `["French"]`). |
| `announce` | qualité affichée dans le titre de la release synthétique quand le titre doit d'abord être téléchargé. |

Un profil du receveur sans correspondance provoque une erreur visible dans le panel. Définis un profil **`default`**, utilisé pour tous les profils non listés.

## Configuration de l'agent

Au premier démarrage, sans `config/agent.toml`, l'agent ouvre une **page de configuration** sur `http://<hôte-du-receveur>:8789`.

1. Saisis le code affiché par `docker compose logs availarr-agent`.
2. **Tailscale** : le bloc affiche un **lien de connexion**. Le receveur l'envoie à l'envoyeur, qui l'ouvre et l'accepte avec son compte Tailscale. L'agent rejoint alors le tailnet.
3. Remplis le formulaire : adresse du server (celle affichée par le server, `http://100.x.y.z:8787`) et jeton partagé (**Tester** vérifie les deux), dossier de téléchargement, Radarr/Sonarr du receveur.
4. Enregistre. Note la **clé API de l'agent** : c'est elle qu'on met dans Radarr/Sonarr du receveur.

Ou à la main, `config/agent.toml` :

```toml
server_url = "http://100.x.y.z:8787"      # adresse du server (Tailscale ou HTTPS)
token = "un-long-secret"                  # le jeton partagé du server
api_key = "une-autre-cle"                 # clé que Radarr/Sonarr du receveur utilisent pour l'agent
public_url = "http://availarr-agent:8789" # comment Radarr/Sonarr du receveur joignent l'agent
complete_dir = "/downloads/complete"      # où l'agent écrit les fichiers terminés
# port = 8789
# db_path = "/data/agent.db"
# categories = ["radarr", "sonarr", "tv", "movies"]
# poll_seconds = 30

[radarr]                                  # instances du receveur
url = "http://radarr:7878"
key = "..."

[sonarr]
url = "http://sonarr:8989"
key = "..."
```

### Radarr et Sonarr du receveur

**Indexeur** (Settings > Indexers > + > Newznab, *Custom*) :

| Champ | Valeur |
|---|---|
| URL | `http://availarr-agent:8789/newznab` |
| API Path | `/api` |
| API Key | `api_key` de l'agent |
| Catégories | `2000` (Radarr) / `5000` (Sonarr) |

**Client de téléchargement** (Settings > Download Clients > + > SABnzbd) :

| Champ | Valeur |
|---|---|
| Host | `availarr-agent` |
| Port | `8789` |
| URL Base | `sabnzbd` |
| API Key | `api_key` de l'agent |
| Catégorie | `radarr` ou `sonarr` |

- `availarr-agent` ne se résout que si Radarr/Sonarr sont sur le même réseau Docker que l'agent ; sinon, utilise l'IP de l'hôte.
- Si `complete_dir` n'a pas le même chemin pour l'agent et pour Radarr/Sonarr, ajoute un **Remote Path Mapping** (Settings > Download Clients).

## Réseau : détails et variantes

**Débit** : redirige le port **UDP 21641** de la box de l'envoyeur vers le server (et en option chez le receveur). Sans ça, Tailscale passe par ses relais DERP, limités à quelques Mbit/s. Vérification depuis le receveur : `docker compose exec tailscale tailscale ping <ip-du-server>` doit répondre `via <ip>:<port>`, pas `via DERP`.

**Connexion sans clic** : dans l'admin Tailscale, *Settings > Keys > Generate auth key* avec le tag voulu, puis un fichier `.env` à côté du compose : `TS_AUTHKEY=tskey-auth-...`. Le conteneur se connecte seul au premier démarrage.

**Sans Tailscale** : retire le service `tailscale` et la ligne `network_mode`, publie les ports sur le service Availarr, et mets un **reverse proxy HTTPS** (Caddy, Traefik, nginx) devant le port 8787 de l'envoyeur, avec `server_url = "https://..."`. Le `token` est alors la seule protection.

**À éviter** : Cloudflare Tunnel (ou le proxy Cloudflare). Ses conditions interdisent en pratique de servir de gros volumes vidéo.

**Le panel (8788) ne doit jamais être exposé au receveur** ni sur Internet. Seul le port 8787 est destiné à l'agent.

## Rétention

Chaque demande du receveur crée une ligne dans le panel, même pour un titre déjà demandé. La nouvelle ligne reprend l'état de la précédente (protection, torrents, progression) et c'est elle qui décide de la suppression.

Un titre téléchargé par l'envoyeur pour le receveur est supprimé chez l'envoyeur quand **les deux** conditions sont remplies : `keep_days` (7 j) écoulés depuis la clôture (livraison ou annulation), **et** `seed_days` (3 j) de seed réel dans qBittorrent. Le panel affiche le temps restant et le seed.

- 🔒 **Protégé** : l'envoyeur possédait déjà le titre, il n'est jamais supprimé.
- 📌 **Gardé** : l'envoyeur a choisi de le conserver dans le panel.
- Les demandes en échec sont purgées après `fail_days` (7 j).

## Développement

```sh
pip install -e ".[dev]"
pytest
python -m availarr.server    # ou python -m availarr.agent
```

- Images locales : `docker build --target server .` (ou `--target agent`).
- Publication : `docker buildx build --platform linux/amd64,linux/arm64 --target server -t kappacitee/availarr-server --push .` (idem `agent`).
- [`compose.agent-test.yml`](compose.agent-test.yml) : banc d'essai côté receveur (Tailscale + agent + un Radarr vierge) sur une seule machine.

### Vérification de bout en bout

Pas encore exécutée avec de vraies instances. À cocher, et noter tout écart de protocole :

- [ ] Les tests de connexion de l'indexeur et du client SABnzbd passent dans Radarr et Sonarr du receveur.
- [ ] Un film que l'envoyeur possède en qualité suffisante apparaît dans la recherche interactive du receveur, est pris, transféré et importé. Il apparaît avec 🔒 dans le panel.
- [ ] Un film absent chez l'envoyeur est ajouté à son Radarr avec le tag `availarr`, téléchargé, transféré et importé chez le receveur. Le panel affiche ensuite le délai et le seed.
- [ ] Un pack de saison Sonarr fonctionne de la même façon.
- [ ] Couper le server pendant un transfert puis le relancer : le transfert reprend.

Écarts constatés : _(à compléter)_
