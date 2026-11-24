# URL Monitor

Un petit service qui vérifie toutes les 60 secondes une liste d'URL, enregistre pour chaque vérification le code de statut, le temps de réponse et l'horodatage, et expose les résultats récents en JSON sur `GET /status`.

Stack : Python 3.13, FastAPI, httpx, SQLite. Livré sous forme d'image Docker.

## Démarrage rapide

```bash
docker build -t url-monitor .
docker run --rm -p 127.0.0.1:8000:8000 url-monitor
curl http://127.0.0.1:8000/status
```

L'image contient une liste d'URL publiques par défaut (`urls.txt`), elle fonctionne donc sans aucun montage. Pour utiliser sa propre liste et conserver les résultats entre deux redémarrages :

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  -v ./my-urls.txt:/app/urls.txt:ro \
  -v monitor-data:/data \
  url-monitor
```

Sur un système avec SELinux (Fedora, RHEL), ajouter `,z` au montage du fichier : `:ro,z`.

### Sans Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn url_monitor.main:app
```

L'application doit tourner dans **un seul processus** : le worker vit à l'intérieur (pas d'option `--workers`).

## Configuration

Le fichier d'URL contient une URL par ligne. Les lignes vides et les lignes commençant par `#` sont ignorées :

```
# Sites surveillés
https://example.com
https://www.python.org/about/
```

Le service refuse de démarrer si le fichier est invalide : schéma autre que `http`/`https`, hôte manquant, identifiants dans l'URL, port invalide ou liste vide. Toutes les lignes fautives sont signalées en une fois. Les doublons sont ignorés.

| Variable | Défaut (hors Docker) | Défaut (Docker) |
|---|---|---|
| `URLS_FILE` | `urls.txt` | `/app/urls.txt` |
| `DB_PATH` | `data/monitor.db` | `/data/monitor.db` |

Valeurs fixées dans le code : intervalle de 60 s, délai maximal de 5 s par vérification, 10 vérifications simultanées au plus, conservation des résultats pendant 7 jours.

## API

### `GET /status?limit=10`

Les `limit` derniers résultats (10 par défaut, de 1 à 100) de chaque URL du fichier, du plus récent au plus ancien :

```json
{
  "urls": [
    {
      "url": "https://example.com",
      "checks": [
        {"checked_at": "2026-11-24T12:01:00.000000+00:00", "status_code": 200, "error": null, "response_time_ms": 111.3},
        {"checked_at": "2026-11-24T12:00:00.000000+00:00", "status_code": null, "error": "timeout", "response_time_ms": 5000.0}
      ]
    }
  ]
}
```

- Chaque vérification a soit un `status_code`, soit une `error`, jamais les deux.
- Valeurs possibles de `error` : `timeout`, `dns_error`, `connection_error`, `tls_error`, `blocked` (adresse non publique, voir Sécurité), `other_error`, `internal_error` (bug dans le service lui-même).
- `response_time_ms` mesure le temps entre l'envoi de la requête et la réception des en-têtes de réponse, sans la résolution DNS. Il vaut `0.0` pour `dns_error` et `blocked`, car aucune requête n'a été envoyée.
- Tous les horodatages sont en UTC.

### `GET /health`

`200 {"status": "ok"}` si le worker tourne et que la base répond, sinon `503 {"status": "unhealthy"}`. Utilisé par le `HEALTHCHECK` de l'image.

## Tests

```bash
.venv/bin/pytest
```

Les tests couvrent le chargement de la liste d'URL, le modèle de données, le stockage (dont la rétention), le garde-fou SSRF, le checker, le worker (concurrence, cadence, gestion des erreurs) et l'API. Aucun test n'accède au réseau : le DNS et les serveurs HTTP sont simulés (`httpx.MockTransport`).

## Choix de conception et compromis

Le détail des 30 décisions, avec les alternatives écartées, se trouve dans [decision.md](decision.md). Les principales :

- **Le worker tourne dans le processus de l'API**, comme tâche de fond démarrée par le `lifespan` de FastAPI. Il n'y a qu'un seul service à déployer. En contrepartie, il faut un seul processus uvicorn, sinon chaque processus lancerait son propre worker.
- **SQLite via le module `sqlite3` standard**, sans ORM. Une seule table, quelques requêtes SQL lisibles, aucune dépendance. Les résultats de plus de 7 jours sont supprimés à chaque écriture.
- **Les échecs sont des résultats**, pas des absences. Un site en panne apparaît comme tel dans `/status`, avec une catégorie d'erreur fixe plutôt qu'un message d'exception, qui pourrait divulguer des détails internes.
- **Une nouvelle connexion par vérification.** Chaque mesure comprend la même mise en place TCP et TLS, ce qui les rend comparables. Réutiliser les connexions serait plus léger, mais la première mesure serait lente et les suivantes rapides.
- **Le corps de la réponse n'est jamais téléchargé, et les redirections ne sont pas suivies.** Un 3xx prouve déjà que le serveur répond. Suivre la redirection pourrait mener vers une adresse interne.
- **Les tournées démarrent toutes les 60 s sur une horloge monotone**, sans dérive, avec au plus 10 vérifications simultanées. Une tournée qui dépasse l'intervalle n'est jamais superposée à la suivante. Avec un délai de 5 s, cela n'arrive qu'au-delà d'environ 120 URL toutes en échec.
- **Les URL ne viennent que d'un fichier**, jamais d'une variable d'environnement ni de l'API. Une seule source garde le chargement simple.
- **Dépendances verrouillées** : `requirements.in` liste les dépendances directes, et `requirements.txt` fige toutes les versions.

## Sécurité

Le worker envoie des requêtes depuis le réseau où il tourne. La principale menace est donc la **SSRF** : une URL qui pointerait, directement ou via le DNS, vers un service interne (interface d'administration, routeur, endpoint de métadonnées cloud).

- **Seules les adresses publiques sont contactées.** Les adresses privées, de loopback, link-local, multicast, réservées, etc., en IPv4 comme en IPv6, sont refusées (`blocked`). La vérification a lieu à chaque requête, pas seulement au démarrage.
- **Protection contre le DNS rebinding.** Le checker se connecte à l'adresse IP exacte qui a été validée, tout en envoyant le vrai nom d'hôte (en-tête `Host` et SNI). Le certificat TLS reste vérifié pour ce nom.
- **Les variables de proxy de l'environnement sont ignorées**, car un proxy contournerait cette vérification.
- **API en lecture seule**, sans aucun moyen d'ajouter des URL. `/docs` et `/openapi.json` sont désactivés. Les URL contenant des identifiants sont refusées, puisque `/status` les afficherait.
- **Le conteneur tourne avec un utilisateur non privilégié** (UID 10001). Le code lui appartient en lecture seule, et seul `/data` est accessible en écriture. Le `.dockerignore` fonctionne en liste blanche, pour qu'aucun fichier local (secret, base de données) n'entre dans l'image.

**Déploiement prévu** : derrière un Cloudflare Tunnel, sans port ouvert, avec Cloudflare Access pour l'authentification, et le port publié uniquement sur `127.0.0.1`. Au lancement, on peut ajouter `--read-only --cap-drop ALL --security-opt no-new-privileges`. Avec `--read-only`, `/data` doit alors être un volume.

## Limites et améliorations possibles

- L'intervalle, le délai, la concurrence et la rétention ne sont pas configurables sans modifier le code.
- Pas d'authentification dans l'application elle-même : elle est déléguée à la couche de déploiement.
- Pas de configuration de journalisation. Les erreurs et avertissements s'affichent avec le format par défaut de Python.
- Docker seul ne redémarre pas un conteneur `unhealthy` : il faut un orchestrateur pour cela.
- Les adresses privées ne peuvent pas être surveillées, ce qui exclut les services du réseau local. Ce choix est volontaire.
- Le client de test de Starlette signale que `httpx` est déprécié au profit de `httpx2`.
