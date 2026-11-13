# Availarr

Availarr permet au receveur d'utiliser la médiathèque de l'envoyeur comme source de téléchargement, depuis son propre Radarr/Sonarr, sans rien changer à leur fonctionnement.

- Chez le **receveur**, l'**agent** se fait passer pour un indexeur **Newznab** et un client de téléchargement **SABnzbd**. Radarr/Sonarr du receveur cherchent, prennent et importent comme d'habitude.
- Chez l'**envoyeur**, le **server** reçoit les demandes. Si L'envoyeur possède déjà le titre en qualité suffisante, il le livre. Sinon, il l'ajoute dans son Radarr/Sonarr (tag `availarr`), attend le téléchargement (qBittorrent), puis le livre.
- Le transfert des fichiers est repris automatiquement après une coupure.

```
 Receveur : Radarr/Sonarr --Newznab/SABnzbd--> agent (8789) ==HTTP via Tailscale, ou HTTPS via reverse proxy==> server (8787) --> Radarr/Sonarr/qBittorrent de l'envoyeur
                                                                                                         panel (8788, réseau local de l'envoyeur)
```

## Installation

Les images sont sur Docker Hub : `kappacitee/availarr-server` et `kappacitee/availarr-agent` (amd64 et arm64). Publication : `docker buildx build --platform linux/amd64,linux/arm64 --target server -t kappacitee/availarr-server --push .` (idem `agent`). Les compose les utilisent directement ; mise à jour : `docker compose pull && docker compose up -d`. Pour construire localement : `docker build --target server .` (ou `--target agent`).

### Côté envoyeur (server)

1. Dans `compose.server.yml`, monter le dossier des médias de l'envoyeur **au même chemin** que dans le Radarr/Sonarr de l'envoyeur (le server lit les fichiers à ce chemin).
2. Préparer les dossiers (le conteneur tourne en uid 1000) : `mkdir -p config data && sudo chown 1000:1000 config data`.
3. Tailscale : créer `.env` avec `TS_AUTHKEY=...` (voir « Réseau entre l'envoyeur et le receveur » ci-dessous). L'API 8787 n'est joignable que par Tailscale ; le panel `8788` est joignable depuis tout le réseau local (pour le limiter à la machine, voir le commentaire de `compose.server.yml`).
4. `docker compose -f compose.server.yml up -d`
5. **Première configuration** : sans `config/server.toml`, le server ouvre une page de configuration sur le port du panel, `http://<hôte-de-l'envoyeur>:8788`. Saisis le code affiché par `docker compose -f compose.server.yml logs`, remplis le formulaire (les boutons « Tester » vérifient chaque service et proposent tes profils et dossiers), puis enregistre : le fichier est écrit et le server démarre. Note le **jeton partagé** affiché en haut, il sert à l'agent.

Tu peux aussi écrire `config/server.toml` à la main à partir de `examples/server.toml`. Pour refaire la configuration web, supprime ce fichier et redémarre le conteneur.

Le fichier de config est lu depuis `/config/server.toml` (variable `AVAILARR_CONFIG` pour changer). Ports : **8787** API (pour l'agent), **8788** panel (réseau local).

### Côté receveur (agent)

1. Monter le dossier de téléchargements du receveur sur `complete_dir`. Le conteneur tourne en utilisateur non-root (uid 1000) : ce dossier doit lui être accessible en écriture.
2. `mkdir -p config data && sudo chown 1000:1000 config data`, et `sudo chown 1000:1000` sur le dossier de téléchargements.
3. `docker compose -f compose.agent.yml up -d`
4. **Première configuration** : ouvre `http://<hôte-du-receveur>:8789`, saisis le code affiché par `docker compose -f compose.agent.yml logs`, puis renseigne l'adresse du server et le jeton partagé (« Tester » vérifie les deux), le dossier de téléchargement et le Radarr/Sonarr du receveur. Note la **clé API de l'agent** : c'est elle qu'on met dans l'indexeur et le client SABnzbd du receveur. Le port 8789 sert ensuite à Radarr et Sonarr.

Ou bien, à la main : copier `examples/agent.toml` vers `config/agent.toml` et le remplir.

Sans Docker : `pip install .` puis `python -m availarr.server` ou `python -m availarr.agent`.

### Réseau entre l'envoyeur et le receveur

**Par défaut : Tailscale dans les conteneurs.** Chaque compose contient un conteneur `tailscale` qui porte le réseau du server ou de l'agent. Rien d'autre à installer, ni chez l'envoyeur ni chez le receveur, et ton frère n'a pas besoin de compte Tailscale : les deux conteneurs rejoignent **ton** tailnet. La connexion est directe entre les deux maisons quand c'est possible (sinon par un relais Tailscale, plus lent). Prérequis : Linux avec `/dev/net/tun` (le cas de tout Docker sous Linux).

1. **Règles d'accès** (une fois, avant tout) : ouvre l'admin Tailscale (https://login.tailscale.com/admin), onglet *Access controls*, et fusionne `examples/tailscale-acl.hujson`. Elles déclarent les tags `tag:availarr-server` et `tag:availarr-agent`, et l'agent ne peut joindre **que** le port 8787 du server. Sans elles, la connexion des conteneurs est refusée.
2. **Chez l'envoyeur** : `docker compose -f compose.server.yml up -d`, puis ouvre la page de configuration (`http://<hôte>:8788`). Dans le bloc Tailscale, saisis le code de configuration puis clique sur **Connecter à Tailscale** et connecte-toi. Le bloc passe à « Connecté » et affiche l'adresse à donner à l'agent : `http://100.x.y.z:8787`.
3. **Chez le receveur** : `docker compose -f compose.agent.yml up -d`, puis la page de configuration de l'agent (`http://<hôte-du-receveur>:8789`). Le bloc Tailscale affiche un **lien de connexion** : ton frère te l'envoie, tu l'ouvres sur ton navigateur et tu acceptes avec ton compte. L'agent rejoint ton réseau, sans compte Tailscale de son côté. Dans le même formulaire, `Adresse du server` = l'adresse affichée par le server.

L'identité Tailscale est ensuite gardée dans `./tailscale` (à ne pas supprimer, ni publier) : rien à refaire aux redémarrages.

**Débit** : redirige le port **UDP 21641** de la box de l'envoyeur vers le server (le compose le publie déjà ; l'agent utilise aussi 21641, à rediriger en option chez le receveur). Sinon Tailscale relaie le trafic par ses serveurs DERP, bridés à quelques Mbit/s. Vérification depuis le receveur : `docker compose exec tailscale tailscale ping <ip-du-server>` doit répondre `via <ip>:<port>`, pas `via DERP`.

**Variante sans clic** : dans l'admin, *Settings > Keys > Generate auth key*, une clé avec le tag voulu, à mettre dans un fichier `.env` à côté du compose (`TS_AUTHKEY=tskey-auth-...`). Le conteneur se connecte alors seul au premier démarrage.

**Sans Tailscale** : retirer le service `tailscale` et la ligne `network_mode` du compose, publier les ports sur le service Availarr, et mettre un **reverse proxy HTTPS** (Caddy, Traefik, nginx) devant le port 8787 de l'envoyeur, avec `server_url = "https://..."`. Le `token` est alors la seule protection.

**À éviter : Cloudflare Tunnel** (ou proxy Cloudflare) pour les fichiers : ses conditions interdisent en pratique de servir de gros volumes vidéo, et le débit n'est pas garanti.

Le **panel (8788) ne doit jamais être exposé au receveur** : il est protégé par `admin_password` mais réservé à l'envoyeur. Ne publier que le port 8787.

## Configuration côté receveur (Radarr et Sonarr)

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

Le nom `availarr-agent` ne se résout que si Radarr/Sonarr du receveur partagent le réseau Docker de l'agent ; sinon utiliser l'IP de l'hôte du receveur.

Si le chemin de `complete_dir` vu par l'agent diffère de celui vu par Radarr/Sonarr, ajouter un **Remote Path Mapping** (Settings > Download Clients).

## Correspondance des profils (`[profiles]`)

Rien n'est déduit automatiquement : la table associe le **profil qualité du receveur** au **profil de l'envoyeur** et à l'exigence minimale.

```toml
[profiles."HD-1080p FR"]        # nom du profil chez le receveur
a_profiles = ["1080p MULTi"]    # profil(s) chez l'envoyeur utilisés pour un ajout / une mise à niveau
min_resolution = 1080
languages = ["French"]
announce = "MULTi.1080p.WEB-DL" # qualité annoncée dans le titre de la release synthétique
```

Un profil du receveur sans correspondance provoque une erreur visible dans le panel. Il est conseillé de définir un profil **`default`**, utilisé en repli pour les profils non listés.

## Rétention

Chaque demande du receveur crée une nouvelle ligne dans le panel, même pour un titre déjà demandé. La nouvelle ligne reprend l'état de la précédente (protection, torrents, progression) et c'est elle qui décide de la suppression.

Un titre livré au receveur est supprimé de l'envoyeur (si c'est l'envoyeur qui l'avait téléchargé pour le receveur) **7 jours après la clôture (livraison ou annulation)** (`keep_days`) **et** après **3 jours de seed réel** dans qBittorrent (`seed_days`). Le panel affiche « dans 7 j » et le temps de seed.

- 🔒 **Protégé** : L'envoyeur possédait déjà le titre, il n'est jamais supprimé.
- 📌 **Gardé** : L'envoyeur a demandé dans le panel de conserver le titre.
- Les jobs en échec sont purgés après `fail_days` (7 j).

## Vérification de bout en bout

À faire par le propriétaire avec de vraies instances (idéalement un Radarr et un Sonarr de test chez le receveur). Cette vérification n'a **pas** été exécutée lors du développement. Noter ici tout écart de protocole constaté.

- [ ] Le test de connexion de l'indexeur et celui du client SABnzbd passent dans Radarr du receveur et dans Sonarr du receveur.
- [ ] Un film que L'envoyeur possède en qualité suffisante apparaît dans la recherche interactive du receveur, est pris, transféré et importé chez le receveur. Il apparaît avec 🔒 dans le panel.
- [ ] Un film absent chez l'envoyeur est ajouté au Radarr de l'envoyeur avec le tag `availarr`, téléchargé, transféré et importé chez le receveur. Le panel affiche ensuite « dans 7 j » et le seed réel.
- [ ] Un pack de saison Sonarr fonctionne de la même façon.
- [ ] Couper le server pendant un transfert, puis le relancer : le transfert reprend.

Écarts constatés : _(à compléter)_
