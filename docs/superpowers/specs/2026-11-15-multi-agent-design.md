# Multi-agent, interfaces et corrections — conception

Maquettes : https://claude.ai/artifact/8gs1SKAXLJYsj7c1x7uTqW

## Intention

Plusieurs receveurs, chacun avec son agent, demandent leurs téléchargements au même server. L'envoyeur voit
ses receveurs, ce que chacun a demandé, et le détail d'une demande. L'agent gagne une interface pour suivre ses
téléchargements. Les deux côtés gagnent une page de configuration pour ne plus éditer le TOML à la main.
Au passage : le seed visible, une demande annulée qui continue de se mettre à jour, et refuser/retirer une demande.

Non-objectifs : comptes ou droits par agent, quotas, limite de débit par agent, statistiques par agent
(les stats restent globales), configuration appliquée à chaud.

Anti-objectifs : un agent qui voit ou télécharge les fichiers d'un autre ; une suppression chez l'envoyeur qui
casse la livraison d'un autre receveur ; une installation existante (un agent, l'ancien `server.toml`) qui
casse à la mise à jour.

## Approches

- **A. Une demande par agent (retenue).** Chaque job porte l'agent qui l'a fait. Deux agents sur le même film =
  deux jobs qui partagent les fichiers chez l'envoyeur. Garde le modèle actuel (un job = un destinataire).
- B. Une demande partagée avec une liste d'abonnés : un seul téléchargement, mais transfert/état/annulation
  multiples dans une ligne, refonte des jobs. Écartée.

Identité (choix de l'utilisateur) : **jeton partagé unique**, comme aujourd'hui. L'agent génère un UUID à sa
configuration et l'envoie à chaque appel ; le server enregistre l'agent à sa première connexion. Pas de jeton
par agent.

## Server

### Agents
- Nouvelle table `agents(id TEXT PRIMARY KEY, name TEXT, first_seen REAL, last_seen REAL, blocked INTEGER)`.
- Chaque appel API porte `X-Availarr-Agent: <uuid>` et `X-Availarr-Agent-Name: <nom>` en plus du jeton.
  Le server crée ou met à jour la ligne (nom envoyé par l'agent, sauf si renommé dans le panel : colonne
  `renamed INTEGER`, le nom du panel l'emporte alors).
- Sans en-tête (agent d'avant la mise à jour) : id `legacy`, nom « Agent ». Les jobs existants reçoivent `legacy`
  par migration (`ALTER TABLE jobs ADD COLUMN agent TEXT DEFAULT 'legacy'`). Avant la mise à jour il n'y avait
  qu'un agent : le **premier agent avec UUID** qui se connecte, s'il n'en existe encore aucun autre, récupère
  tous les jobs `legacy` (historique, demandes en cours, transferts). La carte « Agent » disparaît alors.
  Un agent encore à l'ancienne version qui se connecte plus tard reste sous « Agent ».
- Agent bloqué : toutes ses requêtes API répondent 403. Débloquer depuis sa page.
- Cloisonnement : `GET /v1/requests/{id}`, `/files/{i}`, `/close` répondent 404 si le job n'est pas à cet agent.
- `jobs.agent_seen` disparaît au profit de `agents.last_seen`. En ligne = vu il y a moins de 120 s.

### Jobs
- `active_for(item, agent)` et `failed_for(item, agent)` : la reprise d'une demande et le blocage après échec
  sont par agent. Le panel « Réessayer » ne débloque que cet agent.
- `ensure()` (ajout dans Radarr/Sonarr de l'envoyeur) n'est appelé que si le fichier ne satisfait pas déjà ;
  il est idempotent, donc deux agents sur le même titre ne posent pas de problème.

### Rétention
- Aujourd'hui : « la demande la plus récente d'un item décide ». Devient : les fichiers d'une entrée ne sont
  supprimés que quand **chaque** job non supprimé qui la partage (tous agents confondus) est lui-même éligible.
  Les jobs plus anciens du même item et du même agent suivent le plus récent, comme aujourd'hui.
- Une décision « Garder » sur n'importe quel job de l'entrée protège l'entrée.

### Refuser / Retirer
- **Refuser** (job pas encore livré ni supprimé) : `state=failed`, `error="Refusée par l'envoyeur"`. L'agent voit
  l'échec, SABnzbd le remonte, le Radarr/Sonarr du receveur cherche ailleurs. Bloque une nouvelle demande de cet
  agent pour ce titre jusqu'à « Réessayer ». Si `added_by_tool` et qu'aucun autre job actif ne partage l'entrée :
  retrait de l'entrée et du torrent chez l'envoyeur (même chemin que la rétention).
- **Retirer** (job livré, annulé, échoué, refusé ou supprimé) : colonne `hidden_at`, le job disparaît du panel.
  La rétention continue de le traiter. Pas de bouton sur un job en cours : il faut refuser d'abord.

### Demande annulée qui continue de bouger
Attendu : une demande annulée par le receveur est figée dans le panel.
Cause : la barre de téléchargement lit la progression de l'**item** chez l'envoyeur (`lib.progress(item)`),
pas celle du job. Deux demandes sur le même média (une annulée, une relancée) affichent donc la même barre
qui avance.
Correction : un job avec `outcome == "cancelled"` n'interroge plus rien pour l'affichage (pas de
`jobs.status`, pas de transfert, pas de seed en direct) : barres grisées « Annulé chez le receveur », badge
seul. Le `refresh()` interne continue pour la rétention (sinon un job annulé resterait `pending` et ses
fichiers ne seraient jamais nettoyés). Actions possibles : Supprimer, Retirer. (Maquette : « Anatomie d'une
chute ».)

### Seed (qBittorrent)
Cause : la barre ne s'appuie que sur `job.hashes`, rempli à l'import depuis l'historique. Pendant le
téléchargement (et pour un fichier déjà présent), elle est vide.
Correction :
- Pendant `pending`, les torrents viennent de la file d'attente Radarr/Sonarr (`downloadId`).
- `QBit.info(hashes)` lit en un appel `seeding_time`, `upspeed`, `dlspeed`, `ratio`, `num_seeds/num_leechs`,
  `state`. Si `seeding_time` manque (vieux qBittorrent), repli sur `now - completion_on`.
- Panel : sous la barre, « reste 2 j 17 h · ↑ 1,8 Mo/s · ratio 0,42 » ; pendant le téléchargement,
  « ↓ 11,2 Mo/s · 41 pairs ».

### Panel (voir maquettes)
- `/` : liste des agents (cartes : nom, en ligne, compteurs en cours / à décider / livrés, dernière demande).
- `/agents/{id}` : ses demandes, avec les filtres actuels, Renommer, Bloquer.
- `/jobs/{id}` : détail : progression, étapes datées, torrents (seed, reste, envoi, ratio), fichiers servis et
  leur envoi, fiche de la demande (agent, profils, exigence, release, ajouté par Availarr), épisodes protégés,
  historique des envois, actions.
- `/stats` : inchangé (global).
- `/config` : le formulaire de premier démarrage pré-rempli. « Enregistrer et redémarrer » écrit le TOML puis
  quitte le processus ; `restart: unless-stopped` le relance (déjà dans les compose).

## Agent

- Config : `agent_id` (UUID généré au setup, ou au démarrage s'il manque, puis écrit dans `agent.toml`),
  `agent_name` (défaut : nom d'hôte), `admin_password` (pour l'interface). Envoyés via `ServerClient`.
- Interface sur le port existant (8789), routes `/`, `/fragment`, `/config`, protégées par Basic auth.
  Les routes `/newznab/*` et `/sabnzbd/*` gardent leur clé API.
- `/` : état de la connexion au server (dernier ping réussi, adresse), identité, compteurs, débit, et la liste
  des jobs de `AgentStore` (release, état, progression, ETA). Une demande refusée s'affiche « Refusée par
  l'envoyeur ».
- `/config` : formulaire du setup agent + bloc Identité (nom modifiable, UUID en lecture seule, mot de passe).

## Laissé au constructeur

- Rendu exact des étapes datées (les dates viennent de `created_at`, `ready_at`, `closed_at`, `deleted_at`
  et de la table `transfers`).
- Partage du code de formulaire entre setup et config (`create_setup` réutilisé avec des valeurs initiales).
- Champs secrets de la page de config : ré-afficher ou laisser vides = inchangés (choix par défaut : laisser vides).
