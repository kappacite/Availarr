# Availarr — Document de conception

Date : 2026-11-11

## Intention

Deux homelabs :
- **Homelab A (toi)** : Radarr, Sonarr, Seerr, Jellyfin, qBittorrent. Télécharge en torrent.
- **Homelab B (ton frère)** : Radarr, Sonarr, Seerr, Jellyfin, utilisés aussi par ses collègues. Ne veut **aucun torrent** chez lui.

Availarr fait le pont : une demande faite dans le Seerr de B est servie par A (bibliothèque existante ou nouveau téléchargement), puis le fichier est transféré vers B, où Radarr/Sonarr l'importent normalement.

## Principe général

Le Seerr de B ne change pas : il envoie ses demandes au Radarr/Sonarr de B, comme d'habitude. Availarr s'insère **à l'intérieur** du Radarr/Sonarr de B en se présentant comme :

1. **un indexeur Usenet** (API compatible Newznab) ;
2. **un client de téléchargement Usenet** (API compatible SABnzbd).

Le côté B ne voit donc que de l'Usenet : aucun client torrent, aucun ratio, aucun seed. Le torrent reste entièrement chez A.

Deux composants, un seul dépôt Python, deux images/services Docker :

| Composant | Où | Rôle |
|---|---|---|
| **agent** | chez B | Faux indexeur Newznab + faux SABnzbd pour le Radarr/Sonarr de B. Connexions **sortantes uniquement** vers le serveur. Télécharge les fichiers vers le dossier de téléchargement de B. |
| **server** | chez A | Seul point d'entrée vers A. Parle au Radarr/Sonarr/qBittorrent de A. Sert les fichiers. Héberge le panel admin et la règle de suppression. |

## Déroulé d'une demande (film ; série identique avec Sonarr)

1. Un collègue demande un film dans le Seerr de B → le Radarr de B lance une recherche.
2. Radarr B interroge l'**agent** (indexeur). L'agent demande au **server** : « tel TMDB id, disponible ? »
   - Disponible chez A → l'agent renvoie une release dont le titre et la taille reflètent le **vrai fichier** de A.
   - Pas disponible → l'agent renvoie quand même une release (voir « Releases synthétiques »), pour que Radarr B la récupère.
3. Radarr B « envoie » la release (un NZB factice qui contient seulement un identifiant de job) à l'**agent** (SABnzbd).
4. L'agent transmet au server → si besoin, le server ajoute le film au Radarr de A (avec le tag `availarr`) et lance la recherche/le téléchargement.
5. Pendant ce temps, l'agent expose dans la file SABnzbd un état « en cours » avec une progression (téléchargement chez A, puis transfert).
6. Une fois que le Radarr de A a importé le fichier, l'agent le télécharge depuis le server vers le dossier de téléchargement de B, **avec reprise** (requêtes HTTP Range).
7. L'agent marque le job « Completed » dans l'historique SABnzbd, avec le chemin du fichier → le Radarr de B l'importe, le renomme et prévient Jellyfin. **Aucune action supplémentaire** n'est nécessaire côté bibliothèque B.

La recherche RSS périodique de Radarr/Sonarr B (sans requête) renvoie une liste vide.

**Toute recherche** de Radarr/Sonarr B (demande Seerr, recherche manuelle, « rechercher tout ce qui manque ») peut déclencher un téléchargement chez A ; pas de filtrage via Seerr. Availarr est le seul indexeur et le seul client de téléchargement de B. *(Décision de A.)* Exception, comme indiqué plus bas : les titres en échec bloqués et les recherches d'upgrade.

## Séries (Sonarr)

- Recherche de saison → l'agent renvoie un pack de saison ; recherche d'épisode → une release d'épisode. *(Mon choix.)*
- Côté A, le server ajoute la série si elle est absente et ne surveille que les saisons/épisodes demandés. *(Mon choix.)*
- Une saison complète est transférée sous la forme d'un job contenant plusieurs fichiers.

## Qualité et langue transmises (exigence de A)

Ce que B demande en qualité et en langue doit être **au moins** satisfait.

- Lors d'une recherche, l'agent lit dans le Radarr/Sonarr de B (API locale, clé de B) le **profil qualité** du titre et ses exigences de langue (langue du profil / custom formats). Il transmet au server une exigence simple : qualité minimale et langue(s) requise(s).
- **Correspondance des profils** *(mon choix)* : une table de correspondance dans la config du server, « profil de B → profil de A ». Exemple : `HD-1080p FR` chez B → `1080p MULTi` chez A. Rien n'est déduit automatiquement : les profils et les custom formats sont trop libres pour être comparés de façon fiable. Un profil de B sans correspondance déclenche une erreur visible dans le panel.
- **Fichier existant chez A** : il n'est proposé que si sa qualité et ses langues réelles (lues dans le `movieFile`/`episodeFile` de A) satisfont l'exigence. Le titre de la release reflète ces valeurs réelles, par exemple `Titre.2024.MULTi.1080p.BluRay-Availarr`, pour que B l'accepte.
- **Release synthétique** : son titre reprend la qualité et la langue du profil A correspondant.
- **Fichier chez A insuffisant** → **mise à niveau en place** *(décision de A)*. Le server bascule le titre de A sur le profil A correspondant et lance une recherche. Le Radarr/Sonarr de A remplace son fichier, puis le nouveau fichier est livré à B. Le titre reste protégé 🔒 s'il l'était, et le nouveau profil est conservé chez A (A y gagne une meilleure version). Une seule instance Radarr/Sonarr chez A.
  - Sonarr : le profil s'applique à toute la série. Le server ne lance la recherche que pour les épisodes demandés, mais Sonarr peut ensuite améliorer d'autres épisodes de A via ses flux RSS. *(Mon choix : accepté.)*
  - Si le profil actuel de A dépasse déjà l'exigence de B (le fichier est seulement en attente d'upgrade), le profil n'est pas modifié. *(Mon choix.)*

## Releases synthétiques (titre absent chez A)

Radarr/Sonarr B n'acceptent une release que si son titre passe leur profil de qualité. Pour un titre absent chez A, l'agent génère un nom parsable (`Titre.Année.1080p.WEB-DL-Availarr`), à partir d'une qualité **configurable** côté agent, et une taille estimée. Côté A, le titre est ajouté avec un profil qualité **configurable** côté server. *(Mon choix : on ne cherche pas à garantir que la qualité finale corresponde exactement au nom annoncé ; Radarr B importe ce qu'il reçoit.)*

## Cas limites *(mes choix)*

- **A n'arrive pas à obtenir le titre** (aucun torrent, téléchargement bloqué, import jamais fait) : passé un délai configurable (7 jours par défaut), le job est signalé « Failed » à B. L'agent mémorise l'identifiant et ne propose **plus aucune release** pour ce titre, ce qui évite la boucle blocklist → nouvelle recherche. Le panel affiche le job « Échec », avec un bouton « Réessayer » qui lève ce blocage.
- **A a le titre dans une qualité ou une langue insuffisante pour B** : voir « Qualité et langue transmises ».
- **Torrent disparu avant 3 jours de seed** (retiré à la main, limites qBittorrent, hash introuvable) : la condition de seed est considérée comme remplie, puisqu'il n'y a plus rien à seeder. La règle des 7 jours s'applique ensuite normalement. Le panel affiche « torrent absent ».
- **« Annuler » dans le panel** annule une **décision** (Garder / Suppr.), pas un job en cours.
- **Job supprimé de la file de B** : l'agent arrête le transfert. Le titre chez A reste soumis à la règle de rétention normale.

## Réseau

- **Recommandé : Tailscale** sur les deux machines. Rien n'est exposé sur Internet et la connexion est généralement directe entre les deux, donc à plein débit.
- **Alternative** : le server exposé derrière le reverse proxy de A, en HTTPS.
- **À ne pas faire** : faire passer les fichiers par un tunnel Cloudflare (conditions d'utilisation sur la vidéo et les gros fichiers, débit réduit).
- Dans tous les cas, l'API entre agent et server exige un **jeton secret partagé**.
- L'API du server n'autorise que : disponibilité, création de demande, état, téléchargement de fichier. Les clés API Radarr/Sonarr/qBittorrent de A **ne quittent jamais** le server.
- **Limite de bande passante** configurable côté server (upload de A : 100-400 Mbps).

## Panel admin (côté A)

Disposition validée : **une seule liste triée par date** (« maquette A »), avec des filtres : Tout / À décider / Gardés / Supprimés.

Colonnes : titre (film/série+saison), état (Téléchargement % / Transfert % / Livré / Déjà chez toi), **seed réel** lu dans qBittorrent (« 2 j 04 h / 3 j » + barre), suppression (compte à rebours / Gardé 📌 / ⏳ dès 3 j de seed / jamais 🔒), actions (Garder / Suppr. / Annuler).

- Le panel est servi sur un **port séparé** de l'API agent ; il n'est **pas** accessible depuis B. Protection par un mot de passe simple. *(Mon choix.)*
- Le nom du collègue demandeur **n'est pas** affiché : le Radarr de B ne le connaît pas.

## Règle de rétention (validée)

- **Protégé 🔒** : tout ce qui existait déjà chez A avant la demande n'est **jamais** supprimé (au niveau du fichier : pour une série, seuls les épisodes ajoutés pour B sont concernés). Cela inclut ce qui était déjà présent **et surveillé (Monitored)** chez A sans fichier : A le voulait aussi. *(Décision de A.)* Pour les séries, j'applique la même règle épisode par épisode : un épisode déjà surveillé chez A est protégé. *(Mon choix.)* Un titre présent mais **non surveillé** chez A est traité comme une demande de B ; à la suppression, l'outil retire le fichier et le torrent mais laisse l'entrée en place, non surveillée. *(Mon choix.)*
- **Gardé 📌** : conservé indéfiniment ; annulable.
- **Sans décision** : suppression automatique quand **7 jours se sont écoulés depuis la livraison chez B** ET que le torrent a **au moins 3 jours de seed réel** (durée de seed qBittorrent). Si le seed n'est pas atteint, on attend.
- **Clic sur « Suppr. »** : si les 3 jours de seed sont atteints, suppression immédiate ; sinon, état « ⏳ dès 3 j de seed », puis suppression automatique dès que le seuil est atteint.
- **Suppression** = retrait du film (ou des épisodes / de la série si elle a été ajoutée uniquement pour B) dans Radarr/Sonarr de A, avec les fichiers, **et** retrait du torrent dans qBittorrent, avec ses données. Le torrent est retrouvé via l'historique Radarr/Sonarr (`downloadId` = hash).
- Le seuil de 3 jours et le délai de 7 jours sont configurables.

## Configuration

- **server** : URL et clé de Radarr/Sonarr de A, URL et identifiants de qBittorrent, profils qualité et dossiers racine pour les ajouts, jeton partagé, mot de passe admin, limite de bande passante, délais de rétention.
- **agent** : URL du server, jeton partagé, dossier de téléchargement (le même que celui vu par Radarr/Sonarr de B, avec le mapping de chemins habituel en Docker), qualité annoncée pour les releases synthétiques.
- Côté B : ajout de l'agent comme indexeur Newznab **et** comme client SABnzbd dans Radarr et dans Sonarr.

## Objectifs

- Une demande dans le Seerr de B aboutit à un fichier importé dans B sans intervention manuelle.
- Rien de torrent n'apparaît chez B.
- A contrôle ce qu'il garde, avec un respect garanti de son seuil de seed.

## Hors périmètre

- Nom du demandeur, notifications, plusieurs homelabs « clients » (un seul : B).
- Synchronisation des bibliothèques existantes ; seules les nouvelles demandes de B passent par l'outil.
- Toute intégration Jellyfin : les Radarr/Sonarr des deux côtés s'en occupent.

## Anti-objectifs (échec même si « ça marche »)

- Supprimer un média que A avait déjà avant la demande.
- Retirer un torrent avant 3 jours de seed réel.
- Exposer le Radarr/Sonarr/qBittorrent de A ou leurs clés à B ou à Internet.
- Faire apparaître du torrent chez B.

## Laissé au constructeur

Choix des bibliothèques Python, schéma SQLite pour l'état des jobs, fréquence de scrutation, format exact du NZB factice, détails d'implémentation des API Newznab/SABnzbd (seulement ce que Radarr/Sonarr utilisent réellement), style visuel du panel.
